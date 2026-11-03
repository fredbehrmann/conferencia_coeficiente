"""Consultas da interface: central, caso, resultado, cobertura, dados brutos e regras."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import String, and_, case, cast, distinct, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from validacao.db import sem_acento

from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import EXERCICIOS
from validacao.conferencia.decisao import SITUACOES_ABERTAS, junta_decisao
from validacao.conferencia.execucao import expandir_valores, ultima_execucao
from validacao.modelos import Ente

TAMANHO_PAGINA = 50
ORDENACOES = {
    "prioridade": "Prioridade",
    "dif_abs": "Maior diferença em R$",
    "dif_pct": "Maior diferença em %",
    "ente": "Ente e exercício",
    "confianca": "Menor confiança primeiro",
}


@dataclass
class Filtros:
    q: str = ""
    uf: str = ""
    esfera: str = ""
    exercicio: str = ""
    componente: str = ""
    fonte: str = ""
    tipo: str = ""
    gravidade: str = ""
    situacao: str = ""
    confianca: str = ""
    somente_alerta: bool = True
    somente_pendentes: bool = False
    ordem: str = "prioridade"
    pagina: int = 1
    colunas: list[str] = field(default_factory=list)

    @classmethod
    def de_query(cls, params) -> "Filtros":
        def texto(nome, padrao=""):
            return (params.get(nome) or padrao).strip()

        enviado = params.get("enviado") == "1"
        filtros = cls(
            q=texto("q"), uf=texto("uf"), esfera=texto("esfera"), exercicio=texto("exercicio"),
            componente=texto("componente"), fonte=texto("fonte"), tipo=texto("tipo"),
            gravidade=texto("gravidade"), situacao=texto("situacao"), confianca=texto("confianca"),
            somente_alerta=(params.get("somente_alerta") == "1") if enviado else True,
            somente_pendentes=params.get("somente_pendentes") == "1",
            ordem=texto("ordem", "prioridade") if texto("ordem", "prioridade") in ORDENACOES else "prioridade",
        )
        try:
            filtros.pagina = max(1, int(params.get("pagina") or 1))
        except ValueError:
            filtros.pagina = 1
        if hasattr(params, "getlist"):
            filtros.colunas = [c for c in params.getlist("col") if c]
        return filtros

    def query(self, **troca) -> str:
        from urllib.parse import urlencode

        dados = {
            "q": self.q, "uf": self.uf, "esfera": self.esfera, "exercicio": self.exercicio, "componente": self.componente,
            "fonte": self.fonte, "tipo": self.tipo, "gravidade": self.gravidade, "situacao": self.situacao, "confianca": self.confianca,
            "somente_alerta": "1" if self.somente_alerta else "", "somente_pendentes": "1" if self.somente_pendentes else "",
            "ordem": self.ordem, "pagina": str(self.pagina), "enviado": "1",
        }
        dados.update({k: str(v) for k, v in troca.items()})
        pares = [(k, v) for k, v in dados.items() if v not in ("", None)]
        pares += [("col", c) for c in self.colunas]
        return urlencode(pares)


def expressao_situacao():
    return case(
        (cf.DecisaoCaso.id.is_(None),
         case(
             (cf.Caso.diagnostico.in_(("ALERTA", "SEM_VALIDACAO")), literal("PENDENTE")),
             (cf.Caso.diagnostico == "CONFERIDO", literal("SEM_PENDENCIA")),
             else_=cf.Caso.diagnostico,
         )),
        (and_(cf.DecisaoCaso.impressao != "", cf.DecisaoCaso.impressao != cf.Caso.impressao), literal("REANALISE")),
        else_=cf.DecisaoCaso.situacao_analise,
    )


def consulta_base(execucao_id: int, filtros: Filtros):
    situacao = expressao_situacao().label("situacao")
    consulta = (
        select(cf.Caso, cf.DecisaoCaso, situacao)
        .outerjoin(cf.DecisaoCaso, junta_decisao())
        .where(cf.Caso.execucao_id == execucao_id)
    )
    condicoes = _condicoes(filtros, situacao)
    if condicoes:
        consulta = consulta.where(*condicoes)
    return consulta, situacao


def _condicoes(filtros: Filtros, situacao):
    condicoes = []
    if filtros.componente:
        condicoes.append(cf.Caso.componente_id == filtros.componente)
    if filtros.q:
        texto = filtros.q.strip()
        if texto.isdigit():
            if len(texto) in (2, 7):  # código de estado/DF ou de município: busca exata
                condicoes.append(cf.Caso.id_ente == int(texto))
            else:
                condicoes.append(cast(cf.Caso.id_ente, String).like(f"{texto}%"))
        else:
            condicoes.append(func.sem_acento(cf.Caso.nome_ente).like(f"%{sem_acento(texto)}%"))
    if filtros.uf:
        condicoes.append(cf.Caso.uf == filtros.uf.upper())
    if filtros.esfera:
        condicoes.append(cf.Caso.esfera == filtros.esfera)
    if filtros.exercicio:
        condicoes.append(cf.Caso.exercicio == int(filtros.exercicio))
    if filtros.fonte and filtros.tipo:
        # com tipo de alerta escolhido, a fonte precisa estar envolvida naquele alerta (não só ter valor)
        condicoes.append(exists().where(
            cf.Alerta.caso_id == cf.Caso.id, cf.Alerta.regra == filtros.tipo,
            or_(cf.Alerta.declaracao_b == filtros.fonte, and_(cf.Alerta.declaracao_b.is_(None), cf.Alerta.declaracao_a == filtros.fonte)),
        ))
    elif filtros.fonte:
        condicoes.append(cf.Caso.valores.like(f'%"{filtros.fonte}":%'))
    if filtros.tipo:
        condicoes.append(cf.Caso.tipos.like(f"%|{filtros.tipo}|%"))
    if filtros.gravidade:
        condicoes.append(cf.Caso.gravidade_max == int(filtros.gravidade))
    if filtros.somente_alerta:
        condicoes.append(cf.Caso.diagnostico.in_(("ALERTA", "SEM_VALIDACAO")))
    elif filtros.situacao != "EXCLUIDO":
        condicoes.append(cf.Caso.diagnostico != "EXCLUIDO")
    if filtros.situacao:
        condicoes.append(situacao == filtros.situacao)
    if filtros.confianca == "na":
        condicoes.append(cf.Caso.confianca.is_(None))
    elif filtros.confianca.startswith("ate") and filtros.confianca[3:].isdigit():
        condicoes.append(cf.Caso.confianca <= int(filtros.confianca[3:]))
    elif filtros.confianca.isdigit():
        condicoes.append(cf.Caso.confianca == int(filtros.confianca))
    if filtros.somente_pendentes:
        condicoes.append(situacao.in_(SITUACOES_ABERTAS))
    return [c for c in condicoes if c is not None]


def ordenar(consulta, ordem: str):
    if ordem == "dif_abs":
        return consulta.order_by(cf.Caso.maior_dif_abs.desc().nulls_last(), cf.Caso.prioridade.desc(), cf.Caso.id)
    if ordem == "dif_pct":
        return consulta.order_by(func.abs(cf.Caso.maior_dif_pct).desc().nulls_last(), cf.Caso.prioridade.desc(), cf.Caso.id)
    if ordem == "confianca":
        return consulta.order_by(cf.Caso.confianca.asc().nulls_last(), cf.Caso.prioridade.desc(), cf.Caso.maior_dif_abs.desc().nulls_last(), cf.Caso.id)
    if ordem == "ente":
        return consulta.order_by(cf.Caso.uf, cf.Caso.nome_ente, cf.Caso.exercicio)
    return consulta.order_by(cf.Caso.prioridade.desc(), cf.Caso.maior_dif_abs.desc().nulls_last(), cf.Caso.id)


def chave_filtros(filtros: Filtros) -> tuple:
    """Filtros que mudam o conjunto de casos (sem página, ordem e colunas exibidas)."""
    return (filtros.q, filtros.uf, filtros.esfera, filtros.exercicio, filtros.componente, filtros.fonte, filtros.tipo,
            filtros.gravidade, filtros.situacao, filtros.confianca, filtros.somente_alerta, filtros.somente_pendentes)


def central(session: Session, filtros: Filtros, tamanho: int = TAMANHO_PAGINA, cache=None) -> dict:
    execucao = ultima_execucao(session)
    if execucao is None:
        return {"execucao": None, "linhas": [], "total": 0, "indicadores": {}}
    consulta, situacao = consulta_base(execucao.id, filtros)
    if cache is not None:
        indicadores_linha = cache.obter(session, ("central", execucao.id, chave_filtros(filtros)),
                                        lambda: _indicadores(session, consulta))
    else:
        indicadores_linha = _indicadores(session, consulta)
    total = indicadores_linha[0]
    paginas = max(1, -(-total // tamanho))
    filtros.pagina = min(filtros.pagina, paginas)
    linhas = session.execute(ordenar(consulta, filtros.ordem).offset((filtros.pagina - 1) * tamanho).limit(tamanho)).all()
    return {
        "execucao": execucao,
        "linhas": [_linha(caso, decisao, sit) for caso, decisao, sit in linhas],
        "total": total,
        "paginas": paginas,
        "indicadores": {
            "casos": total, "entes": indicadores_linha[1], "alertas": indicadores_linha[2],
            "registros": indicadores_linha[3], "abertos": indicadores_linha[4], "reanalise": indicadores_linha[5],
            "sem_validacao": indicadores_linha[6], "decididos": indicadores_linha[7],
            "entes_com_alerta": indicadores_linha[8], "casos_com_alerta": indicadores_linha[9],
        },
    }


def _indicadores(session: Session, consulta) -> tuple:
    sub = consulta.subquery()
    return tuple(session.execute(
        select(
            func.count(),
            func.count(distinct(sub.c.id_ente)),
            func.coalesce(func.sum(sub.c.alertas), 0),
            func.coalesce(func.sum(sub.c.registros), 0),
            func.coalesce(func.sum(case((sub.c.situacao.in_(SITUACOES_ABERTAS), 1), else_=0)), 0),
            func.coalesce(func.sum(case((sub.c.situacao == "REANALISE", 1), else_=0)), 0),
            func.coalesce(func.sum(case((sub.c.diagnostico == "SEM_VALIDACAO", 1), else_=0)), 0),
            func.coalesce(func.sum(case((sub.c.situacao.in_(("ESCLARECIDO", "AJUSTADO", "DESCONSIDERADO")), 1), else_=0)), 0),
            func.count(distinct(case((sub.c.diagnostico.in_(("ALERTA", "SEM_VALIDACAO")), sub.c.id_ente)))),
            func.coalesce(func.sum(case((sub.c.diagnostico.in_(("ALERTA", "SEM_VALIDACAO")), 1), else_=0)), 0),
        )
    ).one())


def _linha(caso: cf.Caso, decisao: cf.DecisaoCaso | None, situacao: str) -> dict:
    return {"caso": caso, "decisao": decisao, "situacao": situacao, "valores": expandir_valores(caso.valores)}


def ids_filtrados(session: Session, filtros: Filtros, limite: int = 100000) -> list[int]:
    execucao = ultima_execucao(session)
    if execucao is None:
        return []
    consulta, _ = consulta_base(execucao.id, filtros)
    sub = ordenar(consulta, filtros.ordem).subquery()
    return list(session.scalars(select(sub.c.id).limit(limite)))


def linhas_filtradas(session: Session, filtros: Filtros, limite: int = 200000):
    execucao = ultima_execucao(session)
    if execucao is None:
        return []
    consulta, _ = consulta_base(execucao.id, filtros)
    return [_linha(c, d, s) for c, d, s in session.execute(ordenar(consulta, filtros.ordem).limit(limite)).all()]


def caso_por_chave(session: Session, id_ente: int, exercicio: int, componente: str) -> cf.Caso | None:
    execucao = ultima_execucao(session)
    if execucao is None:
        return None
    return session.scalar(
        select(cf.Caso).where(
            cf.Caso.execucao_id == execucao.id, cf.Caso.id_ente == id_ente,
            cf.Caso.exercicio == exercicio, cf.Caso.componente_id == componente,
        )
    )


def detalhe(session: Session, caso: cf.Caso) -> dict:
    alertas = list(session.scalars(select(cf.Alerta).where(cf.Alerta.caso_id == caso.id).order_by(cf.Alerta.gravidade.desc(), cf.Alerta.id)))
    registros = session.execute(
        select(cf.Registro, cf.Lote)
        .join(cf.Lote, cf.Lote.id == cf.Registro.lote_id)
        .where(
            cf.Registro.id_ente == caso.id_ente, cf.Registro.exercicio == caso.exercicio,
            cf.Registro.componente_id == caso.componente_id,
        )
        .order_by(cf.Registro.declaracao_id, cf.Registro.campo, cf.Registro.versao.desc())
    ).all()
    serie_casos = session.scalars(
        select(cf.Caso).where(
            cf.Caso.execucao_id == caso.execucao_id, cf.Caso.id_ente == caso.id_ente,
            cf.Caso.componente_id == caso.componente_id,
        ).order_by(cf.Caso.exercicio)
    ).all()
    matriz: dict[str, dict[int, dict]] = {}
    for item in serie_casos:
        for declaracao, valor in expandir_valores(item.valores).items():
            matriz.setdefault(declaracao, {})[item.exercicio] = valor
    decisoes_serie = {
        d.exercicio: d for d in session.scalars(
            select(cf.DecisaoCaso).where(
                cf.DecisaoCaso.id_ente == caso.id_ente, cf.DecisaoCaso.componente_id == caso.componente_id,
                cf.DecisaoCaso.vigente.is_(True),
            )
        )
    }
    ente = session.get(Ente, caso.id_ente)
    return {
        "alertas": alertas,
        "registros": registros,
        "matriz": matriz,
        "serie": {item.exercicio: item for item in serie_casos},
        "decisoes_serie": decisoes_serie,
        "ente": ente,
        "valores": expandir_valores(caso.valores),
    }


def cobertura(session: Session, componente: str = "ISS_MUN") -> dict:
    execucao = ultima_execucao(session)
    declaracoes = list(session.scalars(select(cf.Declaracao).order_by(cf.Declaracao.ordem)))
    esperados = {}
    if execucao is not None:
        for ano, quantidade in session.execute(
            select(cf.Caso.exercicio, func.count()).where(
                cf.Caso.execucao_id == execucao.id, cf.Caso.componente_id == componente, cf.Caso.diagnostico != "EXCLUIDO"
            ).group_by(cf.Caso.exercicio)
        ):
            esperados[ano] = quantidade
    contagem = {}
    for declaracao, ano, estado, quantidade in session.execute(
        select(cf.Registro.declaracao_id, cf.Registro.exercicio, cf.Registro.estado, func.count(distinct(cf.Registro.id_ente)))
        .where(cf.Registro.componente_id == componente, cf.Registro.vigente.is_(True), cf.Registro.parcela.in_(("TOTAL", "BRUTO")))
        .group_by(cf.Registro.declaracao_id, cf.Registro.exercicio, cf.Registro.estado)
    ):
        contagem.setdefault(declaracao, {}).setdefault(ano, {})[estado] = quantidade
    por_uf = {}
    if execucao is not None:
        for uf, ano, total, sem_principal in session.execute(
            select(
                cf.Caso.uf, cf.Caso.exercicio, func.count(),
                func.sum(case((and_(cf.Caso.declaracao_principal.is_(None), cf.Caso.situacao_exercicio == "EXIGIVEL"), 1), else_=0)),
            )
            .where(cf.Caso.execucao_id == execucao.id, cf.Caso.componente_id == componente, cf.Caso.diagnostico != "EXCLUIDO")
            .group_by(cf.Caso.uf, cf.Caso.exercicio)
        ):
            por_uf.setdefault(uf, {})[ano] = {"total": total, "sem_principal": sem_principal}
    return {"declaracoes": declaracoes, "esperados": esperados, "contagem": contagem, "por_uf": por_uf, "exercicios": EXERCICIOS}


def dados_brutos(session: Session, params: dict, pagina: int, tamanho: int = 100, cache=None) -> dict:
    condicoes = []
    precisa_ente = bool(params.get("uf") or (params.get("q") and not params["q"].strip().isdigit()))
    if params.get("componente"):
        condicoes.append(cf.Registro.componente_id == params["componente"])
    if params.get("declaracao"):
        condicoes.append(cf.Registro.declaracao_id == params["declaracao"])
    if params.get("exercicio"):
        condicoes.append(cf.Registro.exercicio == int(params["exercicio"]))
    if params.get("uf"):
        condicoes.append(Ente.uf == params["uf"].upper())
    if params.get("q"):
        texto = params["q"].strip()
        condicoes.append(cf.Registro.id_ente == int(texto) if texto.isdigit() else func.sem_acento(Ente.nome).like(f"%{sem_acento(texto)}%"))
    if params.get("parcela"):
        condicoes.append(cf.Registro.parcela == params["parcela"])
    if params.get("estado"):
        condicoes.append(cf.Registro.estado == params["estado"])
    if params.get("lote"):
        condicoes.append(cf.Registro.lote_id == int(params["lote"]))
    if params.get("versoes") != "todas":
        condicoes.append(cf.Registro.vigente.is_(True))

    def contar():
        consulta = select(func.count()).select_from(cf.Registro)
        if precisa_ente:
            consulta = consulta.join(Ente, Ente.id_ente == cf.Registro.id_ente)
        return session.scalar(consulta.where(*condicoes)) or 0

    chave = ("dados", tuple(sorted((k, v) for k, v in params.items() if k != "pagina")))
    total = cache.obter(session, chave, contar) if cache is not None else contar()
    # 1º passo: só os ids da página (ordenação leve); 2º passo: as linhas completas desses ids
    ids = list(session.scalars(
        (select(cf.Registro.id).join(Ente, Ente.id_ente == cf.Registro.id_ente) if precisa_ente else select(cf.Registro.id))
        .where(*condicoes)
        # código IBGE agrupa por UF; com o índice ix_cf_registro_ordem a página sai sem ordenar a tabela (548 → 9 ms)
        .order_by(cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.declaracao_id, cf.Registro.campo, cf.Registro.versao)
        .offset((pagina - 1) * tamanho).limit(tamanho)
    ))
    linhas = []
    if ids:
        por_id = {
            linha[0].id: linha for linha in session.execute(
                select(cf.Registro, Ente.nome, Ente.uf, cf.Lote)
                .join(Ente, Ente.id_ente == cf.Registro.id_ente)
                .join(cf.Lote, cf.Lote.id == cf.Registro.lote_id)
                .where(cf.Registro.id.in_(ids))
            ).all()
        }
        linhas = [por_id[i] for i in ids if i in por_id]
    return {"linhas": linhas, "total": total, "paginas": max(1, -(-total // tamanho))}


def contagem_regras(session: Session) -> dict:
    execucao = ultima_execucao(session)
    if execucao is None:
        return {}
    resultado = {}
    for regra, gravidade, alertas, casos in session.execute(
        select(cf.Alerta.regra, func.max(cf.Alerta.gravidade), func.count(), func.count(distinct(cf.Alerta.caso_id)))
        .where(cf.Alerta.execucao_id == execucao.id)
        .group_by(cf.Alerta.regra)
    ):
        exemplos = session.execute(
            select(cf.Caso.id_ente, cf.Caso.exercicio, cf.Caso.nome_ente, cf.Caso.uf, cf.Alerta.mensagem, cf.Caso.componente_id)
            .join(cf.Caso, cf.Caso.id == cf.Alerta.caso_id)
            .where(cf.Alerta.execucao_id == execucao.id, cf.Alerta.regra == regra)
            .order_by(cf.Alerta.dif_abs.desc().nulls_last(), cf.Alerta.id)
            .limit(3)
        ).all()
        resultado[regra] = {"alertas": alertas, "casos": casos, "gravidade": gravidade, "exemplos": exemplos}
    return resultado
