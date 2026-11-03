"""Execução da validação: monta os casos, aplica as regras e grava o resultado.

Cada execução registra o conjunto de regras usado, os lotes considerados e a
data de corte. Os casos e alertas ficam ligados à execução; as decisões ficam
ligadas ao caso (ente × exercício × componente) e sobrevivem a novas execuções.
"""

from __future__ import annotations

import hashlib
import re
import json
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import (
    COMPONENTES,
    ESCOPO_COMPONENTE,
    EXERCICIOS,
    PRAZO_DECLARACAO,
    SELECAO_INICIAL,
)
from validacao.conferencia.confianca import classificar as classificar_confianca, tolerancia_da_configuracao
from validacao.conferencia.comparacao import (
    RegistroV,
    comparar,
    montar_fonte,
    sigla,
    valor_exibicao,
    valor_serie,
)
from validacao.conferencia.migracao import garantir_conjunto
from validacao.conferencia.regras import Contexto, avaliar, diagnostico, prioridade
from validacao.modelos import Ente

LOTE_INSERCAO = 2000
EXECUCOES_GUARDADAS = 2
REGRAS_DE_PAR = ("DAX", "DSI", "ZER", "ESC")


_PREFIXO = re.compile(
    r"^(prefeitura municipal|munic[ií]pio|governo do estado|estado)\s+(de|do|da|dos|das)\s+|^governo do\s+(?=distrito federal)",
    re.IGNORECASE,
)


def nome_exibicao(nome: str, uf: str) -> str:
    """'Prefeitura Municipal de Recife - PE' -> 'Recife'."""
    limpo = _PREFIXO.sub("", (nome or "").strip())
    limpo = re.sub(rf"\s*[-–]\s*{re.escape(uf)}$", "", limpo, flags=re.IGNORECASE)
    return limpo or nome


def exigibilidade(exercicio: int, data_corte: datetime) -> tuple[bool, str]:
    prazo = date(exercicio + 1, PRAZO_DECLARACAO["mes"], PRAZO_DECLARACAO["dia"])
    if data_corte.date() > prazo:
        return True, ""
    if data_corte.date() <= date(exercicio, 12, 31):
        return False, (
            f"Exercício {exercicio} em curso na data de corte ({data_corte:%d/%m/%Y}). "
            f"A DCA só será exigível em {prazo:%d/%m/%Y} (LRF, art. 51). Ausência não é tratada como falha."
        )
    return False, (
        f"Declaração de {exercicio} ainda não exigível na data de corte ({data_corte:%d/%m/%Y}); prazo em {prazo:%d/%m/%Y} (LRF, art. 51)."
    )


# A cota-parte municipal é processada antes do ICMS estadual (conciliação na UF).
ORDEM_COMPONENTES = ["ISS_MUN", "COTA_ICMS_MUN", "ICMS_UF", "FUNDOS_UF", "ICMS_DF", "ISS_DF"]


def componentes_ativos(session: Session) -> list[str]:
    ativos = {c.id for c in session.scalars(select(cf.Componente).where(cf.Componente.ativo.is_(True)))}
    if not ativos:
        ativos = {c["id"] for c in COMPONENTES if c["ativo"]}
    return [c for c in ORDEM_COMPONENTES if c in ativos]


def executar(engine: Engine, usuario: str = "sistema", componente: str | None = None, progresso=None) -> int:
    with Session(engine) as session:
        conjunto = garantir_conjunto(session, usuario)
        configuracao = json.loads(conjunto.parametros)
        data_corte = configuracao.get("data_corte")
        data_corte = datetime.fromisoformat(data_corte) if data_corte else datetime.now().replace(microsecond=0)
        componentes = [componente] if componente else componentes_ativos(session)
        lotes = [
            {"id": lote.id, "declaracao": lote.declaracao_id, "hash": lote.hash_conteudo}
            for lote in session.scalars(select(cf.Lote).where(cf.Lote.situacao == "CONCLUIDO").order_by(cf.Lote.id))
        ]
        execucao = cf.Execucao(
            conjunto_id=conjunto.id,
            iniciada_em=datetime.now().replace(microsecond=0),
            data_corte=data_corte,
            componentes=json.dumps(componentes),
            lotes=json.dumps(lotes),
            usuario=usuario,
            situacao="EM_ANDAMENTO",
        )
        session.add(execucao)
        session.commit()
        execucao_id = execucao.id
    try:
        casos = alertas = 0
        extras_uf: dict = {}
        estatisticas: dict = {}
        for item in componentes:
            if progresso:
                progresso(f"{item}: lendo registros...")
            n_casos, n_alertas, somas = _processar(engine, execucao_id, configuracao, data_corte, item, progresso, extras_uf,
                                                   estatisticas)
            casos += n_casos
            alertas += n_alertas
            if item == "COTA_ICMS_MUN":
                extras_uf = somas
    except Exception as erro:  # registra a falha e propaga
        with Session(engine) as session:
            item = session.get(cf.Execucao, execucao_id)
            item.situacao = "ERRO"
            item.mensagem = str(erro)[:2000]
            item.concluida_em = datetime.now().replace(microsecond=0)
            session.commit()
        raise
    with Session(engine) as session:
        item = session.get(cf.Execucao, execucao_id)
        item.situacao = "CONCLUIDA"
        item.casos = casos
        item.alertas = alertas
        item.concluida_em = datetime.now().replace(microsecond=0)
        _registrar_impressao_migradas(session, execucao_id)
        from validacao.conferencia.migracao import definir_configuracao

        definir_configuracao(session, f"resumo:{execucao_id}", [
            {"componente": c, "declaracao": d, **v} for (c, d), v in sorted(estatisticas.items())
        ])
        _podar(session)
        session.commit()
    return execucao_id


def ultima_execucao(session: Session) -> cf.Execucao | None:
    return session.scalar(
        select(cf.Execucao).where(cf.Execucao.situacao == "CONCLUIDA").order_by(cf.Execucao.id.desc()).limit(1)
    )


def _carregar(session: Session, componente: str):
    consulta = select(
        cf.Registro.id, cf.Registro.declaracao_id, cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.campo,
        cf.Registro.parcela, cf.Registro.conceito, cf.Registro.valor_centavos, cf.Registro.estado, cf.Registro.periodo,
        cf.Registro.versao, cf.Registro.vigente, cf.Registro.duplicado, cf.Registro.lote_id, cf.Registro.data_extracao,
    ).where(cf.Registro.componente_id == componente)
    agrupado: dict[tuple[int, int], dict[str, list[RegistroV]]] = defaultdict(lambda: defaultdict(list))
    for linha in session.execute(consulta):
        registro = RegistroV(
            id=linha[0], declaracao=linha[1], campo=linha[4], parcela=linha[5], conceito=linha[6], valor=linha[7],
            estado=linha[8], periodo=linha[9], versao=linha[10], vigente=bool(linha[11]), duplicado=linha[12],
            lote_id=linha[13], data_extracao=linha[14],
        )
        agrupado[(linha[2], linha[3])][linha[1]].append(registro)
    return agrupado


def _impressao(exigivel: bool, fontes: dict) -> str:
    partes = [f"exigivel={exigivel}"]
    for declaracao in sorted(fontes):
        for r in sorted(fontes[declaracao].registros, key=lambda r: (r.campo, r.versao, r.id)):
            if r.vigente:
                partes.append(f"{declaracao}|{r.campo}|{r.id}|{r.versao}|{r.valor}|{r.estado}|{r.periodo}|{r.duplicado}")
    return hashlib.sha256("\n".join(partes).encode()).hexdigest()


def _processar(engine, execucao_id, configuracao, data_corte, componente, progresso, extras_uf=None, estatisticas=None):
    extras_uf = extras_uf or {}
    estatisticas = estatisticas if estatisticas is not None else {}
    selecao = configuracao.get("selecao", {}).get(componente) or SELECAO_INICIAL.get(componente, {})
    escopo = ESCOPO_COMPONENTE.get(componente, {"esfera": "MUNICIPIO", "exercicios": EXERCICIOS, "exigibilidade": "LRF"})
    exercicios = escopo["exercicios"]
    precedencia = selecao.get("precedencia", ["SICONFI_DCA"])
    conceito_ref = selecao.get("conceito_referencia", "LIQUIDO")
    regras_cfg = configuracao.get("regras", {})
    tolerancia = tolerancia_da_configuracao(configuracao)
    exclusoes = {int(k): v for k, v in configuracao.get("exclusoes", {}).items()}
    with Session(engine) as session:
        esfera = escopo["esfera"]
        grupos = dict(session.execute(select(cf.Declaracao.id, cf.Declaracao.grupo_independencia)).all())
        entes = session.execute(
            select(Ente.id_ente, Ente.nome, Ente.uf, Ente.ano_instalacao, Ente.ano_extincao).where(Ente.esfera == esfera)
        ).all()
        agrupado = _carregar(session, componente)

    if escopo["exigibilidade"] == "CGIBS":
        motivo_cgibs = (
            "Aguardando a informação dos Estados ao CGIBS sobre as contribuições a fundos, na forma e no prazo fixados "
            "pelo Comitê (LC 227/2026, art. 116, §§ 5º e 6º). Ausência não é tratada como falha."
        )
        exigiveis = {ano: (False, motivo_cgibs) for ano in exercicios}
    else:
        exigiveis = {ano: exigibilidade(ano, data_corte) for ano in exercicios}
    fontes_por_caso: dict[tuple[int, int], dict] = {}
    series: dict[int, dict[str, dict[int, int | None]]] = defaultdict(lambda: defaultdict(dict))
    for (id_ente, ano), por_declaracao in agrupado.items():
        fontes = {d: montar_fonte(d, regs) for d, regs in por_declaracao.items()}
        fontes_por_caso[(id_ente, ano)] = fontes
        for d, fonte in fontes.items():
            series[id_ente][d][ano] = valor_serie(fonte, conceito_ref)

    repetidos: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    principal_fixa = precedencia[0] if precedencia else None
    for id_ente, por_decl in series.items():
        for ano, valor in por_decl.get(principal_fixa, {}).items():
            if valor:
                repetidos[(principal_fixa, ano, valor)].append(id_ente)

    if progresso:
        progresso(f"{componente}: aplicando regras...")
    casos_linhas = []
    alertas_por_caso = []
    somas_uf: dict[tuple[str, int], dict] = {}
    for id_ente, nome, uf, instalacao, extincao in entes:
        for ano in exercicios:
            fontes = fontes_por_caso.get((id_ente, ano), {})
            exigivel, motivo = exigiveis[ano]
            excluido = exclusoes.get(id_ente)
            if not excluido and ((instalacao and ano < instalacao) or (extincao and ano > extincao)):
                excluido = "Ente não existia no exercício (cadastro do IBGE)."
            principal = next((d for d in precedencia if d in fontes and fontes[d].medidas), None)
            comparacoes = {}
            if principal is not None:
                for d, fonte in fontes.items():
                    if d != principal:
                        comparacoes[d] = comparar(fontes[principal], fonte, conceito_ref)
            valor_ref = None if principal is None else valor_serie(fontes[principal], conceito_ref)
            rep_entes = []
            if principal == principal_fixa and valor_ref:
                rep_entes = [e for e in repetidos.get((principal, ano, valor_ref), []) if e != id_ente]
            ctx = Contexto(
                id_ente=id_ente, nome_ente=nome, uf=uf, exercicio=ano, exigivel=exigivel, motivo_exigibilidade=motivo,
                principal=principal, precedencia=precedencia, conceito_ref=conceito_ref, fontes=fontes,
                comparacoes=comparacoes, grupos=grupos, series=series.get(id_ente, {}),
                repeticoes_entes={principal: rep_entes} if principal else {}, data_corte=configuracao.get("data_corte") and data_corte,
                excluido=excluido, componente=componente,
                extras=(
                    {"cota_municipal_uf": extras_uf[(uf, ano)]["soma"], "municipios_uf": extras_uf[(uf, ano)]["municipios"]}
                    if (uf, ano) in extras_uf else {}
                ),
            )
            alertas = avaliar(ctx, regras_cfg)
            diag = diagnostico(ctx, alertas)
            envolvidas = defaultdict(dict)
            for alerta in alertas:
                if alerta.gravidade < 2:
                    continue
                alvos = []
                if alerta.regra in REGRAS_DE_PAR and alerta.declaracao_b:
                    alvos.append(alerta.declaracao_b)
                if alerta.regra in ("AUS", "NEG", "REP", "REE", "VAR", "TOT", "RET", "DUP", "ZER", "ESC") and alerta.declaracao_a:
                    if alerta.regra not in ("ZER", "ESC") or alerta.declaracao_b is None or alerta.declaracao_a == alerta.declaracao_b:
                        alvos.append(alerta.declaracao_a)
                    elif alerta.regra == "ZER" and alerta.valor_a == 0:
                        alvos.append(alerta.declaracao_a)
                for alvo in alvos:
                    envolvidas[alvo][alerta.regra] = max(envolvidas[alvo].get(alerta.regra, 0), alerta.gravidade)
            valores = {}
            for d, fonte in fontes.items():
                original, estado = valor_exibicao(fonte)
                comp = comparacoes.get(d)
                valores[d] = {
                    "registro": fonte.total.id if fonte.total is not None else None,
                    "original": original,
                    "estado": estado,
                    "conceito": fonte.total.conceito if fonte.total is not None else ("BRUTO" if "BRUTO" in fonte.medidas else None),
                    "versao": fonte.total.versao if fonte.total is not None else None,
                    "normalizado": comp.valor_fonte if comp else (valor_ref if d == principal else None),
                    "conceito_comparado": comp.conceito if comp else (conceito_ref if d == principal else None),
                    "derivacao": comp.derivacao_fonte if comp else "",
                    "comparabilidade": "REFERENCIA" if d == principal else (comp.situacao if comp else "SEM_REFERENCIA"),
                    "motivo": comp.motivo if comp else "",
                    "parcela": comp.parcela if comp else "",
                    "assinada": comp.assinada if comp else None,
                    "percentual": comp.percentual if comp else None,
                    "alertas": sorted(envolvidas.get(d, {})),
                    "gravidade": max(envolvidas.get(d, {}).values(), default=0),
                }
            if not excluido:
                for d, item in valores.items():
                    est = estatisticas.setdefault((componente, d), {
                        "presentes": 0, "referencia": 0, "comparaveis": 0, "concordantes": 0, "ressalva": 0,
                        "nao_comparaveis": 0, "fundeb": 0, "vazios": 0,
                    })
                    if item["estado"] in ("VALOR", "ZERO"):
                        est["presentes"] += 1
                    else:
                        est["vazios"] += 1
                    situacao_comp = item["comparabilidade"]
                    if situacao_comp == "REFERENCIA":
                        est["referencia"] += 1
                    elif situacao_comp in ("SIM", "RESSALVA"):
                        est["comparaveis"] += 1
                        est["ressalva"] += situacao_comp == "RESSALVA"
                        if not set(item["alertas"]) & {"DAX", "DSI", "ESC", "ZER"}:
                            est["concordantes"] += 1
                    elif situacao_comp == "NAO":
                        est["nao_comparaveis"] += 1
                    comp_obj = comparacoes.get(d)
                    if comp_obj is not None and comp_obj.inferencia == "FUNDEB":
                        est["fundeb"] += 1
            pares = [a for a in alertas if a.regra in ("DAX", "DSI", "ZER") and a.dif_abs is not None]
            maior = max(pares, key=lambda a: a.dif_abs, default=None)
            if maior is None:
                candidatos = [c for c in comparacoes.values() if c.absoluta is not None and c.situacao in ("SIM", "RESSALVA")]
                melhor = max(candidatos, key=lambda c: c.absoluta, default=None)
                maior_info = (None, None, None, None) if melhor is None else (
                    f"{melhor.declaracao}×{melhor.referencia}", melhor.assinada, melhor.absoluta, melhor.percentual)
            else:
                maior_info = (f"{maior.declaracao_b}×{maior.declaracao_a}", maior.dif_assinada, maior.dif_abs, maior.dif_pct)
            gravidade_max = max((a.gravidade for a in alertas), default=0)
            tipos = sorted({a.regra for a in alertas})
            comparadores = sum(1 for c in comparacoes.values() if c.situacao in ("SIM", "RESSALVA") and grupos.get(c.declaracao) != grupos.get(principal))
            proposto, proposto_decl, proposto_reg, motivo_prop = _proposta(ctx, fontes, principal, valor_ref, exigivel, alertas, selecao)
            estado_principal = "AUSENTE"
            primeira = precedencia[0] if precedencia else None
            if principal is not None:
                estado_principal = valores[principal]["estado"]
            elif primeira in fontes and fontes[primeira].total is not None:
                estado_principal = fontes[primeira].total.estado
            if not exigivel and principal is None:
                estado_principal = "NAO_EXIGIVEL"
            if excluido:
                estado_principal = "NAO_APLICAVEL"
            nivel, motivo_nivel = classificar_confianca(
                componente, principal, valores, "EXIGIVEL" if exigivel else "NAO_EXIGIVEL",
                "EXCLUIDO" if excluido else diag, tolerancia,
            )
            casos_linhas.append({
                "execucao_id": execucao_id, "id_ente": id_ente, "exercicio": ano, "componente_id": componente,
                "esfera": esfera, "uf": uf, "nome_ente": nome_exibicao(nome, uf),
                "situacao_exercicio": "EXIGIVEL" if exigivel else "NAO_EXIGIVEL",
                "declaracao_principal": principal, "registro_principal_id": None if principal is None else valores[principal]["registro"],
                "valor_principal": valor_ref if principal else None, "estado_principal": estado_principal,
                "valores": json.dumps(_compacto(valores), ensure_ascii=False, default=str), "comparadores": comparadores,
                "registros": sum(1 for f in fontes.values() for r in f.registros if r.vigente),
                "par_maior": maior_info[0], "maior_dif_assinada": maior_info[1], "maior_dif_abs": maior_info[2],
                "maior_dif_pct": maior_info[3], "alertas": sum(1 for a in alertas if a.gravidade >= 2),
                "gravidade_max": gravidade_max, "tipos": "|" + "|".join(tipos) + "|" if tipos else "",
                "alertas_resumo": json.dumps(_resumo(alertas)),
                "diagnostico": diag, "prioridade": prioridade(alertas, maior_info[2]) if diag in ("ALERTA", "SEM_VALIDACAO") else 0.0,
                "impressao": _impressao(exigivel, fontes),
                "valor_proposto": proposto, "declaracao_proposta": proposto_decl, "registro_proposto_id": proposto_reg,
                "motivo_proposta": motivo_prop, "confianca": nivel, "confianca_motivo": motivo_nivel,
            })
            alertas_por_caso.append(alertas)
            if valor_ref is not None and not excluido:
                item = somas_uf.setdefault((uf, ano), {"soma": 0, "municipios": 0})
                item["soma"] += valor_ref
                item["municipios"] += 1

    if progresso:
        progresso(f"{componente}: gravando resultado...")
    total_alertas = 0
    with Session(engine) as session:
        conexao = session.connection()  # inserção pelo Core (executemany), sem o custo do ORM por linha
        for inicio in range(0, len(casos_linhas), LOTE_INSERCAO):
            conexao.execute(cf.Caso.__table__.insert(), casos_linhas[inicio:inicio + LOTE_INSERCAO])
        ids = {
            (id_ente, ano): caso_id
            for caso_id, id_ente, ano in session.execute(
                select(cf.Caso.id, cf.Caso.id_ente, cf.Caso.exercicio).where(
                    cf.Caso.execucao_id == execucao_id, cf.Caso.componente_id == componente
                )
            )
        }
        linhas_alerta = []
        for caso, alertas in zip(casos_linhas, alertas_por_caso):
            caso_id = ids[(caso["id_ente"], caso["exercicio"])]
            for a in alertas:
                linhas_alerta.append({
                    "execucao_id": execucao_id, "caso_id": caso_id, "regra": a.regra, "gravidade": a.gravidade,
                    "declaracao_a": a.declaracao_a, "declaracao_b": a.declaracao_b, "valor_a": a.valor_a,
                    "valor_b": a.valor_b, "dif_assinada": a.dif_assinada, "dif_abs": a.dif_abs, "dif_pct": a.dif_pct,
                    "mensagem": a.mensagem, "evidencias": json.dumps(a.evidencias, ensure_ascii=False, default=str),
                })
        total_alertas = sum(1 for linha in linhas_alerta if linha["gravidade"] >= 2)
        for inicio in range(0, len(linhas_alerta), LOTE_INSERCAO):
            conexao.execute(cf.Alerta.__table__.insert(), linhas_alerta[inicio:inicio + LOTE_INSERCAO])
        session.commit()
    return len(casos_linhas), total_alertas, somas_uf


CHAVES_CURTAS = {
    "registro": "r", "original": "o", "estado": "e", "conceito": "k", "versao": "v", "normalizado": "n",
    "conceito_comparado": "kc", "derivacao": "dv", "comparabilidade": "c", "motivo": "m", "parcela": "p",
    "assinada": "a", "percentual": "pc", "alertas": "al", "gravidade": "g",
}
CHAVES_LONGAS = {curta: longa for longa, curta in CHAVES_CURTAS.items()}
_PADROES = {"derivacao": "", "motivo": "", "parcela": "", "alertas": [], "gravidade": 0}


def _compacto(valores: dict) -> dict:
    """Grava os valores do caso com chaves curtas e sem campos vazios (reduz o arquivo)."""
    return {
        d: {CHAVES_CURTAS[k]: v for k, v in item.items() if not (k in _PADROES and v == _PADROES[k]) and v is not None}
        for d, item in valores.items()
    }


def expandir_valores(texto: str) -> dict:
    """Lê os valores gravados (curtos ou longos) e devolve todas as chaves, com padrões."""
    bruto = json.loads(texto or "{}")
    resultado = {}
    for d, item in bruto.items():
        completo = {longa: None for longa in CHAVES_CURTAS}
        completo.update(_PADROES)
        completo["alertas"] = []
        for k, v in item.items():
            completo[CHAVES_LONGAS.get(k, k)] = v
        resultado[d] = completo
    return resultado


def _resumo(alertas) -> list:
    maiores: dict[str, int] = {}
    for alerta in alertas:
        maiores[alerta.regra] = max(maiores.get(alerta.regra, 0), alerta.gravidade)
    return sorted(([regra, g] for regra, g in maiores.items()), key=lambda item: (-item[1], item[0]))


def _proposta(ctx, fontes, principal, valor_ref, exigivel, alertas, selecao):
    if ctx.excluido:
        return None, None, None, f"Fora do universo: {ctx.excluido}"
    if principal is None:
        if not exigivel:
            return None, None, None, "Declaração ainda não exigível. Sem proposta."
        return None, None, None, (
            "Sem valor na fonte principal. A adoção de outra fonte ou de estimativa depende de metodologia aprovada "
            "(LC 227/2026, art. 116, §§ 3º e 4º)."
        )
    status = selecao.get("status", "HIPOTESE")
    motivo = f"Valor da {sigla(principal)} pela precedência {'/'.join(sigla(d) for d in ctx.precedencia)}"
    motivo += " (aprovada)." if status == "APROVADA" else " (hipótese, pendência P1)."
    if principal != (ctx.precedencia[0] if ctx.precedencia else principal):
        motivo += f" A {sigla(ctx.precedencia[0])} não tem valor neste exercício."
    if any(a.regra == "DSI" for a in alertas):
        motivo += " As declarações do SICONFI divergem entre si."
    registro = fontes[principal].total.id if fontes[principal].total is not None else None
    return valor_ref, principal, registro, motivo


def _registrar_impressao_migradas(session: Session, execucao_id: int) -> None:
    """Decisões migradas da versão 1 analisaram as mesmas cargas que viraram os lotes migrados.

    Na primeira execução após a migração, a impressão das evidências é registrada
    para que retificações posteriores sinalizem reanálise.
    """
    pendentes = session.scalars(
        select(cf.DecisaoCaso).where(cf.DecisaoCaso.origem == "MIGRADA_V1", cf.DecisaoCaso.impressao == "")
    ).all()
    if not pendentes:
        return
    migrados = {
        lote.carga_legada_id for lote in session.scalars(select(cf.Lote).where(cf.Lote.carga_legada_id.is_not(None)))
    }
    novos = session.scalar(select(cf.Lote.id).where(cf.Lote.carga_legada_id.is_(None)).limit(1))
    for decisao in pendentes:
        cargas = set(json.loads(decisao.registros_analisados or "{}").get("cargas_legadas", {}).values())
        if novos is not None or not cargas <= migrados:
            continue
        caso = session.scalar(
            select(cf.Caso).where(
                cf.Caso.execucao_id == execucao_id, cf.Caso.id_ente == decisao.id_ente,
                cf.Caso.exercicio == decisao.exercicio, cf.Caso.componente_id == decisao.componente_id,
            )
        )
        if caso is not None:
            decisao.impressao = caso.impressao
            decisao.execucao_id = execucao_id


def _podar(session: Session) -> None:
    ids = list(session.scalars(select(cf.Execucao.id).where(cf.Execucao.situacao == "CONCLUIDA").order_by(cf.Execucao.id.desc())))
    antigas = ids[EXECUCOES_GUARDADAS:]
    if not antigas:
        return
    session.execute(delete(cf.Alerta).where(cf.Alerta.execucao_id.in_(antigas)))
    session.execute(delete(cf.Caso).where(cf.Caso.execucao_id.in_(antigas)))
    session.execute(update(cf.Execucao).where(cf.Execucao.id.in_(antigas)).values(mensagem="Casos e alertas removidos (mantidas as 2 execuções mais recentes)."))
    session.execute(delete(cf.Configuracao).where(cf.Configuracao.chave.in_([f"resumo:{i}" for i in antigas])))
