"""Migração da versão 1 para a conferência, preservando dados e histórico.

- Cada carga antiga vira um lote (origem MIGRACAO), com o mesmo hash e arquivo.
- Cada valor observado dos componentes do art. 115 (ISS, cota-parte do ICMS e
  ICMS) vira um registro original, com aba, linha e coluna; o arquivo fica no lote.
  Compensações financeiras (fora do art. 115) permanecem apenas nas tabelas da v1.
- As decisões antigas são copiadas com referência à decisão de origem.
Nenhuma tabela da versão 1 é alterada. A migração é idempotente.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import (
    COMPONENTE_DF,
    COMPONENTE_DO_LEGADO,
    COMPONENTES_MIGRADOS,
    DECLARACAO_DO_LEGADO,
    EXCLUSOES_UNIVERSO,
    MAPEAMENTOS_LEGADO,
    NATUREZA_TCE,
    SELECAO_INICIAL,
    semear_catalogo,
)
from validacao.conferencia.regras import parametros_iniciais
from validacao.catalogo import POR_ID
from validacao.modelos import Base, Carga, Decisao

VERSAO_ESQUEMA = "3"

ABA_LEGADO = {
    "SICONFI": {"ISS_TOTAL": "SICONFI", "COTA_RECEBIDA_TOTAL": "SICONFI", "ICMS_TOTAL": "SICONFI",
                "COTA_REPASSADA": "SICONFI", "COMPENSACAO": "SICONFI", "COMPENSACAO_REPASSADA": "SICONFI"},
}


COLUNAS_NOVAS = {
    "cf_caso": {"confianca": "INTEGER", "confianca_motivo": "TEXT NOT NULL DEFAULT ''"},
}


# Índice de cobertura medido no diagnóstico de performance: Recuperação 653 → 183 ms, Cobertura 468 → 131 ms.
INDICES_NOVOS = (
    "CREATE INDEX IF NOT EXISTS ix_cf_registro_cobertura ON cf_registro "
    "(componente_id, vigente, declaracao_id, exercicio, estado, parcela, id_ente)",
    # Dados brutos: página ordenada por código IBGE direto do índice (548 → 9 ms na página 500).
    "CREATE INDEX IF NOT EXISTS ix_cf_registro_ordem ON cf_registro "
    "(componente_id, vigente, id_ente, exercicio, declaracao_id, campo, versao)",
)


def criar_estrutura(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conexao:
        for tabela, colunas in COLUNAS_NOVAS.items():
            existentes = {linha[1] for linha in conexao.execute(text(f"PRAGMA table_info({tabela})"))}
            for nome, tipo in colunas.items():
                if existentes and nome not in existentes:
                    conexao.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))
        criados = 0
        for comando in INDICES_NOVOS:
            nome = comando.split(" ON ")[0].split()[-1]
            if not conexao.execute(text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": nome}).first():
                conexao.execute(text(comando))
                criados += 1
        sem_estatisticas = not conexao.execute(text("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'")).first()
        if criados or sem_estatisticas:
            conexao.execute(text("ANALYZE"))


def classificar_confianca_existente(engine: Engine) -> int:
    """Calcula o nível de confiança dos casos já gravados que ainda não o têm (sem revalidar)."""
    from validacao.conferencia.confianca import classificar, tolerancia_da_configuracao
    from validacao.conferencia.execucao import expandir_valores

    atualizados = 0
    with Session(engine) as session:
        execucoes = session.execute(
            select(cf.Execucao.id, cf.Execucao.conjunto_id).where(cf.Execucao.situacao == "CONCLUIDA")
        ).all()
        for execucao_id, conjunto_id in execucoes:
            conjunto = session.get(cf.ConjuntoRegras, conjunto_id) if conjunto_id else None
            tol = tolerancia_da_configuracao(json.loads(conjunto.parametros) if conjunto else {})
            linhas = session.execute(
                select(cf.Caso.id, cf.Caso.componente_id, cf.Caso.declaracao_principal, cf.Caso.valores,
                       cf.Caso.situacao_exercicio, cf.Caso.diagnostico)
                .where(cf.Caso.execucao_id == execucao_id, cf.Caso.confianca.is_(None), cf.Caso.confianca_motivo == "")
            ).all()
            lote = []
            for caso_id, componente, principal, valores, situacao, diag in linhas:
                nivel, motivo = classificar(componente, principal, expandir_valores(valores), situacao, diag, tol)
                lote.append({"i": caso_id, "n": nivel, "m": motivo})
            for inicio in range(0, len(lote), 5000):
                session.execute(text("UPDATE cf_caso SET confianca = :n, confianca_motivo = :m WHERE id = :i"),
                                lote[inicio:inicio + 5000])
            atualizados += len(lote)
        session.commit()
    return atualizados


def configuracao(session: Session, chave: str, padrao=None):
    item = session.get(cf.Configuracao, chave)
    return padrao if item is None else json.loads(item.valor)


def definir_configuracao(session: Session, chave: str, valor) -> None:
    item = session.get(cf.Configuracao, chave)
    if item is None:
        session.add(cf.Configuracao(chave=chave, valor=json.dumps(valor, ensure_ascii=False)))
    else:
        item.valor = json.dumps(valor, ensure_ascii=False)


def conjunto_inicial() -> dict:
    return {
        "regras": parametros_iniciais(),
        "selecao": SELECAO_INICIAL,
        "exclusoes": EXCLUSOES_UNIVERSO,
        "data_corte": None,
    }


def garantir_conjunto(session: Session, usuario: str = "sistema") -> cf.ConjuntoRegras:
    vigente = session.scalar(select(cf.ConjuntoRegras).where(cf.ConjuntoRegras.vigente.is_(True)).order_by(cf.ConjuntoRegras.id.desc()))
    if vigente is not None:
        return vigente
    conjunto = cf.ConjuntoRegras(
        criado_em=datetime.now().replace(microsecond=0),
        criado_por=usuario,
        parametros=json.dumps(conjunto_inicial(), ensure_ascii=False),
        observacao="Configuração inicial. Todos os parâmetros são hipóteses de trabalho.",
        vigente=True,
    )
    session.add(conjunto)
    session.flush()
    return conjunto


def preparar(engine: Engine, usuario: str = "sistema") -> None:
    """Cria tabelas, catálogo e configuração (sem migrar dados)."""
    criar_estrutura(engine)
    with Session(engine) as session:
        semear_catalogo(session)
        garantir_conjunto(session, usuario)
        if configuracao(session, "versao_esquema") is None:
            definir_configuracao(session, "versao_esquema", VERSAO_ESQUEMA)
        session.commit()
        from validacao.conferencia.acesso import garantir_admin

        garantir_admin(session)


def migrar(engine: Engine, usuario: str = "migracao") -> dict:
    preparar(engine, usuario)
    resumo = {"lotes": 0, "registros": 0, "decisoes": 0}
    with Session(engine) as session:
        migradas = set(session.scalars(select(cf.Lote.carga_legada_id).where(cf.Lote.carga_legada_id.is_not(None))))
        cargas = session.scalars(select(Carga).where(Carga.situacao == "CONCLUIDA").order_by(Carga.id)).all()
        for carga in cargas:
            if carga.id in migradas:
                continue
            declaracao = DECLARACAO_DO_LEGADO.get(carga.fonte_id)
            if declaracao is None:
                continue
            lote = cf.Lote(
                declaracao_id=declaracao,
                origem_tipo="MIGRACAO",
                arquivo=carga.arquivo_ou_endpoint,
                parametros=json.dumps({"carga_legada": carga.id, "exercicios": carga.exercicios}, ensure_ascii=False),
                hash_conteudo=carga.hash,
                data_extracao=carga.data_extracao,
                data_importacao=datetime.now().replace(microsecond=0),
                usuario=usuario,
                situacao="CONCLUIDO",
                carga_legada_id=carga.id,
                observacao=f"Migrado da carga {carga.id} da versão 1 (carregada em {carga.data_carga:%d/%m/%Y %H:%M} por {carga.usuario}).",
            )
            session.add(lote)
            session.flush()
            quantidade = _copiar_valores(session, carga, lote)
            lote.lidos = lote.novos = quantidade
            resumo["lotes"] += 1
            resumo["registros"] += quantidade
        resumo["decisoes"] = _migrar_decisoes(session)
        definir_configuracao(session, "migrado_em", datetime.now().strftime("%Y-%m-%d %H:%M"))
        session.commit()
    resumo["atualizacao"] = atualizar_estrutura(engine)
    return resumo


def atualizar_estrutura(engine: Engine) -> dict:
    """Atualização idempotente para a versão 2 do esquema (todos os componentes).

    - copia dos lotes migrados os campos da v1 que ainda faltam (ex.: cota repassada);
    - separa ISS e ICMS do DF em componentes próprios;
    - aplica parcela, conceito e mapeamento aos registros ainda sem classificação.
    """
    preparar(engine)
    resumo = {"registros_copiados": 0, "registros_df": 0, "registros_classificados": 0}
    with Session(engine) as session:
        for lote in session.scalars(select(cf.Lote).where(cf.Lote.carga_legada_id.is_not(None))).all():
            carga = session.get(Carga, lote.carga_legada_id)
            if carga is None:
                continue
            existentes = set(session.scalars(select(cf.Registro.campo).where(cf.Registro.lote_id == lote.id).distinct()))
            quantidade = _copiar_valores(session, carga, lote, existentes)
            if quantidade:
                lote.lidos += quantidade
                lote.novos += quantidade
                resumo["registros_copiados"] += quantidade
        for origem, destino in COMPONENTE_DF.items():
            resultado = session.execute(
                text("UPDATE cf_registro SET componente_id = :d WHERE componente_id = :o AND id_ente IN (SELECT id_ente FROM ente WHERE esfera = 'DF')"),
                {"d": destino, "o": origem},
            )
            resumo["registros_df"] += resultado.rowcount or 0
            session.execute(
                text("UPDATE cf_decisao SET componente_id = :d WHERE componente_id = :o AND id_ente IN (SELECT id_ente FROM ente WHERE esfera = 'DF')"),
                {"d": destino, "o": origem},
            )
        natureza_case = " ".join(f"WHEN '{nome}' THEN '{conceito}'" for nome, conceito in NATUREZA_TCE.items())
        for m in session.scalars(select(cf.Mapeamento)).all():
            if m.conceito == "POR_REGISTRO":
                conceito_sql = f"COALESCE(CASE json_extract(procedencia, '$.natureza_informada') {natureza_case} END, 'INDEFINIDO')"
                conhecidas = ", ".join(f"'{nome}'" for nome, conceito in NATUREZA_TCE.items() if conceito != "INDEFINIDO")
                condicao = (
                    f"(mapeamento_id IS NULL OR (conceito = 'INDEFINIDO' AND "
                    f"json_extract(procedencia, '$.natureza_informada') IN ({conhecidas})))"
                )
            else:
                conceito_sql = ":conceito"
                condicao = "mapeamento_id IS NULL"
            resultado = session.execute(
                text(
                    f"UPDATE cf_registro SET parcela = :parcela, conceito = {conceito_sql}, mapeamento_id = :mid "
                    f"WHERE declaracao_id = :decl AND campo = :campo AND {condicao}"
                ),
                {"parcela": m.parcela, "conceito": m.conceito, "mid": m.id, "decl": m.declaracao_id, "campo": m.campo},
            )
            resumo["registros_classificados"] += resultado.rowcount or 0
        session.commit()
    resumo["casos_com_confianca"] = classificar_confianca_existente(engine)
    with Session(engine) as session:
        definir_configuracao(session, "versao_esquema", VERSAO_ESQUEMA)
        session.commit()
    return resumo


def precisa_atualizar(engine: Engine) -> bool:
    with Session(engine) as session:
        return configuracao(session, "versao_esquema") != VERSAO_ESQUEMA


def _copiar_valores(session: Session, carga: Carga, lote: cf.Lote, existentes: set[str] | None = None) -> int:
    existentes = existentes or set()
    session.execute(text("DROP TABLE IF EXISTS temp._cf_mapa"))
    session.execute(text(
        "CREATE TEMP TABLE _cf_mapa (componente_legado TEXT PRIMARY KEY, componente TEXT, parcela TEXT, conceito TEXT, mapeamento_id INTEGER, aba TEXT)"
    ))
    mapeamentos = {
        (m.declaracao_id, m.campo): m.id for m in session.scalars(select(cf.Mapeamento))
    }
    linhas = []
    for componente_legado, definicao in POR_ID.items():
        if componente_legado in existentes:
            continue
        chave = (carga.fonte_id, componente_legado)
        componente = COMPONENTE_DO_LEGADO.get(definicao.bloco, definicao.bloco)
        parcela = conceito = mapeamento_id = None
        if chave in MAPEAMENTOS_LEGADO:
            declaracao, parcela, conceito, _detalhe, _evid, _status, _componente = MAPEAMENTOS_LEGADO[chave]
            mapeamento_id = mapeamentos.get((declaracao, componente_legado))
        aba = _aba(carga.fonte_id, componente_legado)
        linhas.append({"c": componente_legado, "comp": componente, "p": parcela, "k": conceito, "m": mapeamento_id, "a": aba})
    if not linhas:
        session.execute(text("DROP TABLE IF EXISTS temp._cf_mapa"))
        return 0
    session.execute(text("INSERT INTO _cf_mapa VALUES (:c, :comp, :p, :k, :m, :a)"), linhas)
    natureza_case = " ".join(f"WHEN '{nome}' THEN '{conceito}'" for nome, conceito in NATUREZA_TCE.items())
    lista_componentes = ", ".join(f"'{c}'" for c in COMPONENTES_MIGRADOS)
    resultado = session.execute(
        text(
            f"""
            INSERT INTO cf_registro (lote_id, declaracao_id, id_ente, exercicio, campo, componente_id, parcela, conceito,
                mapeamento_id, valor_texto, valor_centavos, estado, periodo, versao, vigente, procedencia, data_extracao, valor_legado_id)
            SELECT :lote, :declaracao, v.id_ente, v.ano, v.componente_id, m.componente, m.parcela,
                CASE WHEN m.conceito = 'POR_REGISTRO' THEN COALESCE(CASE v.natureza {natureza_case} END, 'INDEFINIDO') ELSE m.conceito END,
                m.mapeamento_id, CAST(v.valor AS TEXT), CAST(ROUND(v.valor * 100) AS INTEGER),
                CASE WHEN v.valor = 0 THEN 'ZERO' ELSE 'VALOR' END, 'ANUAL', 1, 1,
                CASE WHEN v.natureza IS NULL THEN
                    json_object('aba', CASE WHEN m.aba = 'POR_ESFERA' THEN (CASE e.esfera WHEN 'MUNICIPIO' THEN 'Municipios' ELSE 'Estados' END) ELSE m.aba END,
                                'linha', v.linha_origem, 'coluna', v.conta_origem)
                ELSE
                    json_object('aba', m.aba, 'linha', v.linha_origem, 'coluna', v.conta_origem, 'natureza_informada', v.natureza)
                END,
                :extracao, v.id
            FROM valor_observado v
            JOIN _cf_mapa m ON m.componente_legado = v.componente_id
            JOIN ente e ON e.id_ente = v.id_ente
            WHERE v.carga_id = :carga AND m.componente IN ({lista_componentes})
            """
        ),
        {
            "lote": lote.id,
            "declaracao": lote.declaracao_id,
            "carga": carga.id,
            "extracao": carga.data_extracao,
        },
    )
    session.execute(text("DROP TABLE IF EXISTS temp._cf_mapa"))
    return resultado.rowcount or 0


def _nome_arquivo(caminho: str) -> str:
    from pathlib import PureWindowsPath

    return PureWindowsPath(caminho).name if "\\" in caminho else Path(caminho).name


def _aba(fonte: str, componente: str) -> str:
    if fonte == "SICONFI":
        return ABA_LEGADO["SICONFI"].get(componente, "SICONFI_BRUTO")
    if fonte in ("SIOPE", "SIOPS"):
        return "POR_ESFERA"
    if fonte == "TCE":
        return "Base detalhada"
    return ""


def _migrar_decisoes(session: Session) -> int:
    ja = set(session.scalars(select(cf.DecisaoCaso.decisao_legada_id).where(cf.DecisaoCaso.decisao_legada_id.is_not(None))))
    quantidade = 0
    for antiga in session.scalars(select(Decisao).order_by(Decisao.id)):
        if antiga.id in ja:
            continue
        componente = COMPONENTE_DO_LEGADO.get(antiga.bloco, antiga.bloco)
        acao = {
            "USAR_SUGERIDA": "MANTER_PRINCIPAL" if antiga.fonte == "SICONFI" else "ADOTAR_FONTE",
            "TROCAR_FONTE": "MANTER_PRINCIPAL" if antiga.fonte == "SICONFI" else "ADOTAR_FONTE",
            "TIRAR_ANO": "DESCONSIDERAR",
            "VALOR_DIGITADO": "CORRIGIR_VALOR",
        }.get(antiga.acao, "MANTER_PENDENTE")
        situacao = {
            "MANTER_PRINCIPAL": "ESCLARECIDO",
            "ADOTAR_FONTE": "AJUSTADO",
            "CORRIGIR_VALOR": "AJUSTADO",
            "DESCONSIDERAR": "DESCONSIDERADO",
        }.get(acao, "PENDENTE")
        valor = None if antiga.valor is None else int(round(antiga.valor * 100))
        declaracao = DECLARACAO_DO_LEGADO.get(antiga.fonte or "", antiga.fonte)
        cargas = json.loads(antiga.cargas_usadas or "{}")
        session.add(
            cf.DecisaoCaso(
                id_ente=antiga.id_ente,
                exercicio=antiga.ano,
                componente_id=componente,
                acao=acao,
                situacao_analise=situacao,
                valor_anterior=None if antiga.valor_anterior is None else int(round(antiga.valor_anterior * 100)),
                valor_adotado=valor,
                declaracao_adotada=declaracao,
                elegivel=acao != "DESCONSIDERAR",
                justificativa=(
                    f"Decisão migrada da versão 1 (decisão nº {antiga.id}, ação {antiga.acao}, fonte {antiga.fonte}). "
                    + (f"Motivo original: {antiga.motivo}" if antiga.motivo else "Sem motivo registrado na versão 1 (aceite da fonte sugerida).")
                ),
                usuario=_nome_usuario(session, antiga.usuario_id),
                usuario_id=antiga.usuario_id,
                criado_em=antiga.data,
                impressao="",
                registros_analisados=json.dumps({"cargas_legadas": cargas}, ensure_ascii=False),
                vigente=antiga.estado == "VIGENTE",
                origem="MIGRADA_V1",
                decisao_legada_id=antiga.id,
            )
        )
        quantidade += 1
    session.flush()
    return quantidade


def _nome_usuario(session: Session, usuario_id: int | None) -> str:
    from validacao.modelos import Usuario

    usuario = session.get(Usuario, usuario_id) if usuario_id else None
    return usuario.nome if usuario is not None else "desconhecido"


def esta_migrado(engine: Engine) -> bool:
    from sqlalchemy import inspect

    if "cf_lote" not in inspect(engine).get_table_names():
        return False
    with Session(engine) as session:
        return configuracao(session, "migrado_em") is not None
