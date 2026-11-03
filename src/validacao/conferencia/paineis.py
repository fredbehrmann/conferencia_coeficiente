"""Consultas dos painéis gerenciais: visão geral e recuperação de dados por ente."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import ESCOPO_COMPONENTE, EXERCICIOS
from validacao.conferencia.consultas import expressao_situacao
from validacao.conferencia.decisao import SITUACOES_ABERTAS, junta_decisao
from validacao.conferencia.execucao import ORDEM_COMPONENTES, ultima_execucao
from validacao.conferencia.migracao import configuracao
from validacao.db import sem_acento
from validacao.modelos import Ente

DIAGNOSTICOS_ORDEM = ("CONFERIDO", "ALERTA", "SEM_VALIDACAO", "NAO_EXIGIVEL")


def visao_geral(session: Session) -> dict:
    execucao = ultima_execucao(session)
    if execucao is None:
        return {"execucao": None}
    situacao = expressao_situacao()
    valor_final = case(
        (and_(cf.DecisaoCaso.id.is_not(None), cf.DecisaoCaso.elegivel.is_(False)), None),
        (cf.DecisaoCaso.valor_adotado.is_not(None), cf.DecisaoCaso.valor_adotado),
        else_=cf.Caso.valor_proposto,
    )
    base = (
        select(
            cf.Caso.componente_id, cf.Caso.exercicio, cf.Caso.diagnostico, situacao.label("situacao"),
            func.count().label("casos"), func.count(distinct(cf.Caso.id_ente)).label("entes"),
            func.sum(valor_final).label("valor"), func.sum(case((valor_final.is_(None), 1), else_=0)).label("sem_valor"),
            func.sum(cf.Caso.alertas).label("alertas"),
            func.sum(case((cf.DecisaoCaso.valor_adotado.is_not(None), 1), else_=0)).label("adotados"),
        )
        .select_from(cf.Caso)
        .outerjoin(cf.DecisaoCaso, junta_decisao())
        .where(cf.Caso.execucao_id == execucao.id, cf.Caso.diagnostico != "EXCLUIDO")
        .group_by(cf.Caso.componente_id, cf.Caso.exercicio, cf.Caso.diagnostico, situacao)
    )
    componentes: dict[str, dict] = {}
    valores: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {"valor": 0, "casos": 0, "sem_valor": 0, "adotados": 0}))
    for linha in session.execute(base):
        comp = componentes.setdefault(linha.componente_id, {
            "casos": 0, "diagnosticos": defaultdict(int), "situacoes": defaultdict(int), "alertas": 0, "exigiveis": 0,
        })
        comp["casos"] += linha.casos
        comp["diagnosticos"][linha.diagnostico] += linha.casos
        comp["situacoes"][linha.situacao] += linha.casos
        comp["alertas"] += linha.alertas or 0
        if linha.diagnostico != "NAO_EXIGIVEL":
            comp["exigiveis"] += linha.casos
        celula = valores[linha.componente_id][linha.exercicio]
        celula["valor"] += linha.valor or 0
        celula["casos"] += linha.casos
        if linha.diagnostico != "NAO_EXIGIVEL":
            celula["sem_valor"] += linha.sem_valor or 0
        celula["adotados"] += linha.adotados or 0
    entes = dict(session.execute(
        select(cf.Caso.componente_id, func.count(distinct(cf.Caso.id_ente)))
        .where(cf.Caso.execucao_id == execucao.id, cf.Caso.diagnostico != "EXCLUIDO")
        .group_by(cf.Caso.componente_id)
    ).all())
    entes_alerta = dict(session.execute(
        select(cf.Caso.componente_id, func.count(distinct(cf.Caso.id_ente)))
        .where(cf.Caso.execucao_id == execucao.id, cf.Caso.diagnostico.in_(("ALERTA", "SEM_VALIDACAO")))
        .group_by(cf.Caso.componente_id)
    ).all())
    for codigo, comp in componentes.items():
        comp["entes"] = entes.get(codigo, 0)
        comp["entes_alerta"] = entes_alerta.get(codigo, 0)
        comp["com_alerta"] = comp["diagnosticos"].get("ALERTA", 0) + comp["diagnosticos"].get("SEM_VALIDACAO", 0)
        comp["abertos"] = sum(comp["situacoes"].get(s, 0) for s in SITUACOES_ABERTAS)
        comp["concluidos"] = sum(comp["situacoes"].get(s, 0) for s in ("ESCLARECIDO", "AJUSTADO", "DESCONSIDERADO"))
        comp["reanalise"] = comp["situacoes"].get("REANALISE", 0)

    alertas_regra = defaultdict(dict)
    for regra, componente, gravidade, casos in session.execute(
        select(cf.Alerta.regra, cf.Caso.componente_id, func.max(cf.Alerta.gravidade), func.count(distinct(cf.Alerta.caso_id)))
        .join(cf.Caso, cf.Caso.id == cf.Alerta.caso_id)
        .where(cf.Alerta.execucao_id == execucao.id)
        .group_by(cf.Alerta.regra, cf.Caso.componente_id)
    ):
        alertas_regra[regra][componente] = {"casos": casos, "gravidade": gravidade}

    por_uf = defaultdict(dict)
    for uf, componente, total, alerta, abertos in session.execute(
        select(
            cf.Caso.uf, cf.Caso.componente_id,
            func.sum(case((cf.Caso.diagnostico != "NAO_EXIGIVEL", 1), else_=0)),
            func.sum(case((cf.Caso.diagnostico.in_(("ALERTA", "SEM_VALIDACAO")), 1), else_=0)),
            func.sum(case((situacao.in_(SITUACOES_ABERTAS), 1), else_=0)),
        )
        .select_from(cf.Caso)
        .outerjoin(cf.DecisaoCaso, junta_decisao())
        .where(cf.Caso.execucao_id == execucao.id, cf.Caso.diagnostico != "EXCLUIDO")
        .group_by(cf.Caso.uf, cf.Caso.componente_id)
    ):
        por_uf[uf][componente] = {"total": total or 0, "alerta": alerta or 0, "abertos": abertos or 0,
                                  "taxa": (alerta or 0) / total if total else None}

    maiores = session.execute(
        select(cf.Caso, situacao.label("situacao"))
        .outerjoin(cf.DecisaoCaso, junta_decisao())
        .where(cf.Caso.execucao_id == execucao.id, cf.Caso.diagnostico == "ALERTA", cf.Caso.maior_dif_abs.is_not(None),
               situacao.in_(SITUACOES_ABERTAS))
        .order_by(cf.Caso.maior_dif_abs.desc())
        .limit(10)
    ).all()

    ordem_decl = {d: i for i, (d,) in enumerate(session.execute(select(cf.Declaracao.id).order_by(cf.Declaracao.ordem)))}
    concordancia = sorted(
        (item for item in (configuracao(session, f"resumo:{execucao.id}", []) or []) if item.get("comparaveis")),
        key=lambda item: (ORDEM_COMPONENTES.index(item["componente"]) if item["componente"] in ORDEM_COMPONENTES else 99,
                          ordem_decl.get(item["declaracao"], 99)),
    )
    lotes = session.execute(
        select(cf.Lote.declaracao_id, func.count(), func.max(cf.Lote.data_importacao), func.max(cf.Lote.data_extracao))
        .where(cf.Lote.situacao == "CONCLUIDO").group_by(cf.Lote.declaracao_id)
    ).all()
    registros = dict(session.execute(
        select(cf.Registro.declaracao_id, func.count()).where(cf.Registro.vigente.is_(True)).group_by(cf.Registro.declaracao_id)
    ).all())
    consultas = session.execute(
        select(cf.Consulta.fonte, func.count(), func.sum(case((cf.Consulta.ok.is_(True), 1), else_=0)))
        .group_by(cf.Consulta.fonte)
    ).all()
    ordem = [c for c in ORDEM_COMPONENTES if c in componentes]
    return {
        "execucao": execucao,
        "ordem": ordem,
        "componentes": componentes,
        "valores": valores,
        "alertas_regra": alertas_regra,
        "por_uf": dict(sorted(por_uf.items())),
        "maiores": maiores,
        "concordancia": concordancia,
        "lotes": lotes,
        "registros": registros,
        "consultas": consultas,
        "pacote": configuracao(session, "pacote_siconfi"),
        "exercicios": EXERCICIOS,
    }


# ------------------------------------------------------------ recuperação

ESTADOS_CELULA = {
    "V": ("Valor recuperado", "rv"),
    "Z": ("Zero declarado", "rz"),
    "A": ("Registro sem valor (célula vazia ou fonte sem dado)", "ra"),
    "I": ("Formato inválido", "ri"),
    "E": ("Consulta com erro", "re"),
    "N": ("Sem registro", "rn"),
    "X": ("Declaração ainda não exigível", "rx"),
}
FILTROS_RECUPERACAO = {
    "": "Todos os entes",
    "lacuna_principal": "Com lacuna na fonte principal (exercício exigível)",
    "falha_consulta": "Com consulta do pacote SICONFI que falhou",
    "sem_rreo": "Sem RREO em algum exercício exigível",
    "sem_msc": "Sem MSC em algum exercício exigível",
}


def recuperacao(session: Session, componente: str, uf: str = "", q: str = "", filtro: str = "", pagina: int = 1,
                tamanho: int = 60) -> dict:
    escopo = ESCOPO_COMPONENTE.get(componente, ESCOPO_COMPONENTE["ISS_MUN"])
    exercicios = escopo["exercicios"]
    execucao = ultima_execucao(session)
    exigiveis = set()
    if execucao is not None:
        exigiveis = {
            ano for (ano,) in session.execute(
                select(cf.Caso.exercicio).where(
                    cf.Caso.execucao_id == execucao.id, cf.Caso.componente_id == componente,
                    cf.Caso.situacao_exercicio == "EXIGIVEL",
                ).distinct()
            )
        }
    consulta = select(Ente).where(Ente.esfera == escopo["esfera"])
    if uf:
        consulta = consulta.where(Ente.uf == uf.upper())
    if q:
        texto = q.strip()
        consulta = consulta.where(Ente.id_ente == int(texto)) if texto.isdigit() else consulta.where(func.sem_acento(Ente.nome).like(f"%{sem_acento(texto)}%"))
    presentes_registro = (
        select(cf.Registro.id_ente, cf.Registro.declaracao_id, cf.Registro.exercicio)
        .where(cf.Registro.componente_id == componente, cf.Registro.vigente.is_(True),
               cf.Registro.parcela.in_(("TOTAL", "BRUTO")), cf.Registro.estado.in_(("VALOR", "ZERO")))
    )
    if filtro == "lacuna_principal" and execucao is not None:
        consulta = consulta.where(Ente.id_ente.in_(
            select(cf.Caso.id_ente).where(
                cf.Caso.execucao_id == execucao.id, cf.Caso.componente_id == componente,
                cf.Caso.situacao_exercicio == "EXIGIVEL", cf.Caso.declaracao_principal.is_(None),
                cf.Caso.diagnostico != "EXCLUIDO",
            )
        ))
    elif filtro == "falha_consulta":
        consulta = consulta.where(Ente.id_ente.in_(select(cf.Consulta.id_ente).where(cf.Consulta.ok.is_(False))))
    elif filtro in ("sem_rreo", "sem_msc") and exigiveis:
        declaracao = "SICONFI_RREO" if filtro == "sem_rreo" else "SICONFI_MSC"
        sub = presentes_registro.where(cf.Registro.declaracao_id == declaracao, cf.Registro.exercicio.in_(exigiveis)).subquery()
        completos = select(sub.c.id_ente).group_by(sub.c.id_ente).having(func.count(distinct(sub.c.exercicio)) >= len(exigiveis))
        consulta = consulta.where(Ente.id_ente.not_in(completos))
    total = session.scalar(select(func.count()).select_from(consulta.subquery())) or 0
    paginas = max(1, -(-total // tamanho))
    pagina = min(max(1, pagina), paginas)
    entes = list(session.scalars(consulta.order_by(Ente.uf, Ente.nome).offset((pagina - 1) * tamanho).limit(tamanho)))
    ids = [e.id_ente for e in entes]

    estados: dict[tuple[int, str, int], str] = {}
    if ids:
        for id_ente, declaracao, ano, estado in session.execute(
            select(cf.Registro.id_ente, cf.Registro.declaracao_id, cf.Registro.exercicio, cf.Registro.estado)
            .where(cf.Registro.componente_id == componente, cf.Registro.vigente.is_(True),
                   cf.Registro.parcela.in_(("TOTAL", "BRUTO")), cf.Registro.id_ente.in_(ids))
        ):
            codigo = {"VALOR": "V", "ZERO": "Z", "AUSENTE": "A", "INVALIDO": "I"}.get(estado, "A")
            atual = estados.get((id_ente, declaracao, ano))
            if atual is None or atual in ("A", "I"):
                estados[(id_ente, declaracao, ano)] = codigo
    falhas: dict[tuple[int, str, int], str] = {}
    if ids:
        for id_ente, fonte, ano, ok, erro, detalhe in session.execute(
            select(cf.Consulta.id_ente, cf.Consulta.fonte, cf.Consulta.ano, cf.Consulta.ok, cf.Consulta.erro, cf.Consulta.detalhe)
            .where(cf.Consulta.id_ente.in_(ids))
        ):
            declaracao = {"RREO": "SICONFI_RREO", "MSC": "SICONFI_MSC"}.get(fonte, fonte)
            if ok is False:
                falhas[(id_ente, declaracao, ano)] = erro or detalhe or "falha"
    declaracoes_com_dados = {
        d for (d,) in session.execute(select(cf.Registro.declaracao_id).where(cf.Registro.componente_id == componente).distinct())
    }
    todas = [d for (d,) in session.execute(select(cf.Declaracao.id).order_by(cf.Declaracao.ordem))]
    colunas = [d for d in todas if d in declaracoes_com_dados or d in ("SICONFI_DCA", "SICONFI_RREO", "SICONFI_MSC")]
    pacote = {(c.id_ente, c.ano): c for c in session.scalars(select(cf.CoberturaPacote).where(cf.CoberturaPacote.id_ente.in_(ids)))} if ids else {}

    linhas = []
    for ente in entes:
        celulas = {}
        faltas = 0
        for declaracao in colunas:
            fila = []
            for ano in exercicios:
                codigo = estados.get((ente.id_ente, declaracao, ano))
                titulo = ""
                if codigo in (None, "A") and (ente.id_ente, declaracao, ano) in falhas:
                    codigo, titulo = "E", falhas[(ente.id_ente, declaracao, ano)]
                elif codigo is None:
                    codigo = "N" if ano in exigiveis else "X"
                if declaracao == "SICONFI_DCA" and ano in exigiveis and codigo not in ("V", "Z"):
                    faltas += 1
                fila.append({"ano": ano, "codigo": codigo, "titulo": titulo})
            celulas[declaracao] = fila
        situacoes_pacote = defaultdict(int)
        for ano in exercicios:
            item = pacote.get((ente.id_ente, ano))
            if item is not None:
                situacoes_pacote[item.situacao] += 1
        linhas.append({"ente": ente, "celulas": celulas, "faltas": faltas, "pacote": dict(situacoes_pacote)})

    # resumo por fonte e exercício (todos os entes do universo, não só a página)
    universo = session.scalar(select(func.count()).select_from(Ente).where(Ente.esfera == escopo["esfera"])) or 0
    contagem = defaultdict(dict)
    sub = presentes_registro.subquery()
    for declaracao, ano, quantidade in session.execute(
        select(sub.c.declaracao_id, sub.c.exercicio, func.count(distinct(sub.c.id_ente))).group_by(sub.c.declaracao_id, sub.c.exercicio)
    ):
        contagem[declaracao][ano] = quantidade
    consultas = defaultdict(dict)
    for fonte, ano, total_c, ok_c in session.execute(
        select(cf.Consulta.fonte, cf.Consulta.ano, func.count(), func.sum(case((cf.Consulta.ok.is_(True), 1), else_=0)))
        .join(Ente, Ente.id_ente == cf.Consulta.id_ente)
        .where(Ente.esfera == escopo["esfera"])
        .group_by(cf.Consulta.fonte, cf.Consulta.ano)
    ):
        consultas[{"RREO": "SICONFI_RREO", "MSC": "SICONFI_MSC"}.get(fonte, fonte)][ano] = {"total": total_c, "ok": ok_c or 0}
    return {
        "componente": componente, "exercicios": exercicios, "exigiveis": exigiveis, "colunas": colunas,
        "linhas": linhas, "total": total, "paginas": paginas, "pagina": pagina, "universo": universo,
        "contagem": contagem, "consultas": consultas,
    }


# ---------------------------------------------------------------- situação da importação do pacote SICONFI

def situacao_pacote(session: Session, arquivos: list[dict]) -> dict:
    """Etapas (arquivos, importação, validação), o que entrou por componente e a concordância com a DCA."""
    from datetime import datetime as _dt

    info = configuracao(session, "pacote_siconfi") or {}
    diagnostico = configuracao(session, "pacote_siconfi_diagnostico") or {}
    importado_em = None
    if info.get("importado_em"):
        for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                importado_em = _dt.strptime(info["importado_em"], formato)
                break
            except ValueError:
                continue
    presentes = [a for a in arquivos if a.get("presente")]
    mais_recente = max((a["modificado"] for a in presentes), default=None)
    desatualizado = bool(importado_em and mais_recente and mais_recente > importado_em)
    lotes = list(session.scalars(
        select(cf.Lote).where(cf.Lote.declaracao_id.in_(("SICONFI_RREO", "SICONFI_MSC")), cf.Lote.situacao == "CONCLUIDO")
        .order_by(cf.Lote.id)
    ))
    execucao = ultima_execucao(session)
    validado = bool(execucao and importado_em and execucao.concluida_em
                    and execucao.concluida_em >= importado_em.replace(second=0))

    registros: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for declaracao, componente, estado, n in session.execute(
        select(cf.Registro.declaracao_id, cf.Registro.componente_id, cf.Registro.estado, func.count())
        .where(cf.Registro.declaracao_id.in_(("SICONFI_RREO", "SICONFI_MSC")), cf.Registro.vigente.is_(True))
        .group_by(cf.Registro.declaracao_id, cf.Registro.componente_id, cf.Registro.estado)
    ):
        registros[componente][declaracao][estado] = n

    concordancia: dict[str, dict[str, dict]] = defaultdict(dict)
    divergencias: dict[tuple[str, str], int] = {}
    if execucao is not None:
        for item in configuracao(session, f"resumo:{execucao.id}") or []:
            if item.get("declaracao") in ("SICONFI_RREO", "SICONFI_MSC"):
                comparaveis = item.get("comparaveis") or 0
                item = dict(item)
                item["taxa"] = (item.get("concordantes", 0) / comparaveis) if comparaveis else None
                concordancia[item["componente"]][item["declaracao"]] = item
        for componente, declaracao, n in session.execute(
            select(cf.Caso.componente_id, cf.Alerta.declaracao_b, func.count(distinct(cf.Alerta.caso_id)))
            .join(cf.Caso, cf.Caso.id == cf.Alerta.caso_id)
            .where(cf.Alerta.execucao_id == execucao.id, cf.Alerta.regra == "DSI",
                   cf.Alerta.declaracao_b.in_(("SICONFI_RREO", "SICONFI_MSC")))
            .group_by(cf.Caso.componente_id, cf.Alerta.declaracao_b)
        ):
            divergencias[(componente, declaracao)] = n
    componentes = [c for c in ORDEM_COMPONENTES if c in registros]
    if not presentes:
        etapa = "sem_arquivos"
    elif not lotes:
        etapa = "importar"
    elif desatualizado:
        etapa = "atualizar"
    elif not validado:
        etapa = "validar"
    else:
        etapa = "pronto"
    return {
        "info": info, "diagnostico": diagnostico, "importado_em": importado_em, "arquivos_em": mais_recente,
        "desatualizado": desatualizado, "lotes": lotes, "execucao": execucao, "validado": validado,
        "registros": registros, "concordancia": concordancia, "divergencias": divergencias,
        "componentes": componentes, "etapa": etapa, "presentes": len(presentes), "esperados": len(arquivos),
        "resultado": configuracao(session, "pacote_siconfi_resultado") or {},
    }
