import json
from datetime import datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from validacao.modelos import Carga, Decisao, RegraVersao, ValorObservado, Verificacao
from validacao.motor import TOTAL_DO_BLOCO, VERMELHAS, reprocessar
from validacao.referencia import iguais
from validacao.regras import garantir_regra, parametros_de

ACOES_DE_FONTE = ("USAR_SUGERIDA", "TROCAR_FONTE")
TAMANHO_PAGINA = 50
_BLOCO_DO_TOTAL = {componente: bloco for bloco, componente in TOTAL_DO_BLOCO.items()}


class DecisaoRecusada(ValueError):
    pass


def situacoes_de(texto: str) -> set[str]:
    return set(json.loads(texto))


def fecha_limpo(situacoes: set[str]) -> bool:
    return "FECHA" in situacoes and not (situacoes & VERMELHAS)


def siconfi_bloqueado(situacoes: set[str]) -> bool:
    return "SEM_ABERTURA" in situacoes


def consultar_fila(
    session: Session,
    regra_id: int,
    esfera: str | None = None,
    uf: str | None = None,
    ano: int | None = None,
    situacao: str | None = None,
    pagina: int = 1,
):
    from validacao.modelos import Ente

    consulta = _sem_decisao(regra_id)
    if esfera:
        consulta = consulta.where(Ente.esfera == esfera)
    if uf:
        consulta = consulta.where(Ente.uf == uf)
    if ano:
        consulta = consulta.where(Verificacao.ano == ano)
    if situacao:
        consulta = consulta.where(Verificacao.situacoes.contains(f'"{situacao}"'))
    if situacao != "FECHA":
        consulta = consulta.where(~_expressao_limpa())
    total = session.scalar(select(func.count()).select_from(consulta.subquery()))
    ordenada = consulta.order_by(Ente.uf, Ente.nome, Verificacao.ano, Verificacao.bloco)
    inicio = max(pagina - 1, 0) * TAMANHO_PAGINA
    linhas = session.execute(ordenada.offset(inicio).limit(TAMANHO_PAGINA)).all()
    return linhas, total or 0


def contar_lote(
    session: Session,
    regra_id: int,
    esfera: str | None = None,
    uf: str | None = None,
    ano: int | None = None,
    situacao: str | None = None,
) -> int:
    from validacao.modelos import Ente

    consulta = _candidatos_lote(regra_id, esfera, uf, ano, situacao)
    return session.scalar(select(func.count()).select_from(consulta.subquery())) or 0


def aceitar_lote(
    session: Session,
    regra: RegraVersao,
    usuario_id: int,
    esfera: str | None = None,
    uf: str | None = None,
    ano: int | None = None,
    situacao: str | None = None,
) -> int:
    linhas = session.execute(_candidatos_lote(regra.id, esfera, uf, ano, situacao)).all()
    cargas = json.dumps(_cargas_vigentes(session))
    agora = datetime.now().replace(microsecond=0)
    registros = []
    for verificacao, _ente in linhas:
        situacoes = situacoes_de(verificacao.situacoes)
        if not fecha_limpo(situacoes) or siconfi_bloqueado(situacoes):
            continue
        if verificacao.fonte_sugerida != "SICONFI" or verificacao.valor_sugerido is None:
            continue
        registros.append(
            {
                "id_ente": verificacao.id_ente,
                "ano": verificacao.ano,
                "bloco": verificacao.bloco,
                "acao": "USAR_SUGERIDA",
                "fonte": "SICONFI",
                "valor": verificacao.valor_sugerido,
                "valor_anterior": None,
                "motivo": None,
                "usuario_id": usuario_id,
                "data": agora,
                "cargas_usadas": cargas,
                "estado": "VIGENTE",
                "revisor_id": None,
                "data_revisao": None,
            }
        )
    for inicio in range(0, len(registros), 400):
        session.execute(insert(Decisao), registros[inicio : inicio + 400])
    return len(registros)


def registrar(
    session: Session,
    id_ente: int,
    ano: int,
    bloco: str,
    acao: str,
    usuario_id: int,
    fonte: str | None = None,
    valor: float | None = None,
    motivo: str | None = None,
) -> Decisao:
    regra = garantir_regra(session)
    verificacao = session.scalar(
        select(Verificacao).where(
            Verificacao.regra_versao_id == regra.id,
            Verificacao.id_ente == id_ente,
            Verificacao.ano == ano,
            Verificacao.bloco == bloco,
        )
    )
    if verificacao is None:
        raise DecisaoRecusada("Este bloco não está na verificação vigente.")
    situacoes = situacoes_de(verificacao.situacoes)
    if "NAO_SE_APLICA" in situacoes:
        raise DecisaoRecusada("Este bloco não se aplica.")
    motivo = (motivo or "").strip() or None
    fonte_escolhida, valor_escolhido = _resolver_acao(
        acao, situacoes, verificacao, fonte, valor, motivo
    )
    anterior = _vigente(session, id_ente, ano, bloco)
    if anterior is not None:
        anterior.estado = "SUBSTITUIDA"
        session.flush()
    decisao = Decisao(
        id_ente=id_ente,
        ano=ano,
        bloco=bloco,
        acao=acao,
        fonte=fonte_escolhida,
        valor=valor_escolhido,
        valor_anterior=None if anterior is None else anterior.valor,
        motivo=motivo,
        usuario_id=usuario_id,
        data=datetime.now().replace(microsecond=0),
        cargas_usadas=json.dumps(_cargas_vigentes(session)),
        estado="VIGENTE",
    )
    session.add(decisao)
    session.flush()
    return decisao


def confirmar(session: Session, decisao_id: int, revisor_id: int) -> Decisao:
    decisao = session.get(Decisao, decisao_id)
    if decisao is None or decisao.estado != "VIGENTE":
        raise DecisaoRecusada("Não há decisão vigente para confirmar.")
    if decisao.usuario_id == revisor_id:
        raise DecisaoRecusada("Quem decidiu não confirma a própria decisão.")
    decisao.revisor_id = revisor_id
    decisao.data_revisao = datetime.now().replace(microsecond=0)
    return decisao


def exige_revisao(ente, valor, corte) -> bool:
    if ente.esfera in ("ESTADO", "DF") or ente.capital:
        return True
    if corte is None or valor is None:
        return False
    return abs(valor) >= float(corte)


def consultar_revisao(session: Session, pagina: int = 1):
    from validacao.modelos import Ente, Usuario

    regra = garantir_regra(session)
    corte = parametros_de(regra).get("revisao_corte")
    condicao = (Ente.esfera.in_(("ESTADO", "DF"))) | (Ente.capital.is_(True))
    if corte is not None:
        condicao = condicao | (func.abs(Decisao.valor) >= float(corte))
    consulta = (
        select(Decisao, Ente, Usuario)
        .join(Ente, Ente.id_ente == Decisao.id_ente)
        .join(Usuario, Usuario.id == Decisao.usuario_id)
        .where(Decisao.estado == "VIGENTE", Decisao.revisor_id.is_(None), condicao)
    )
    total = session.scalar(select(func.count()).select_from(consulta.subquery())) or 0
    inicio = max(pagina - 1, 0) * TAMANHO_PAGINA
    linhas = session.execute(
        consulta.order_by(Ente.uf, Ente.nome, Decisao.ano, Decisao.bloco).offset(inicio).limit(TAMANHO_PAGINA)
    ).all()
    return linhas, total


def reabrir_alterados(session: Session) -> int:
    decisoes = session.scalars(
        select(Decisao).where(
            Decisao.estado == "VIGENTE",
            Decisao.acao.in_(ACOES_DE_FONTE),
            Decisao.fonte.is_not(None),
        )
    ).all()
    if not decisoes:
        return 0
    cargas = _cargas_vigentes(session)
    atuais = _totais_das_cargas(session, cargas)
    reabertos = 0
    for decisao in decisoes:
        atual = atuais.get((decisao.fonte, decisao.id_ente, decisao.ano, decisao.bloco))
        if atual is None or decisao.valor is None or not iguais(atual, decisao.valor):
            decisao.estado = "REABERTA"
            reabertos += 1
    return reabertos


def prever_regra(session: Session, parametros: dict) -> tuple[RegraVersao, dict]:
    vigente = garantir_regra(session)
    regra = RegraVersao(vigente_desde=None, parametros=json.dumps(parametros))
    session.add(regra)
    session.flush()
    reprocessar(session, regra)
    antes = _individuais(session, vigente.id)
    depois = _individuais(session, regra.id)
    return regra, {"entram": len(depois - antes), "saem": len(antes - depois)}


def confirmar_regra(session: Session, regra_id: int) -> RegraVersao:
    regra = session.get(RegraVersao, regra_id)
    if regra is None or regra.vigente_desde is not None:
        raise DecisaoRecusada("Esta versão já está vigente.")
    regra.vigente_desde = datetime.now().replace(microsecond=0)
    return regra


def cancelar_regra(session: Session, regra_id: int) -> None:
    regra = session.get(RegraVersao, regra_id)
    if regra is None or regra.vigente_desde is not None:
        raise DecisaoRecusada("A versão vigente não pode ser apagada.")
    session.execute(delete(Verificacao).where(Verificacao.regra_versao_id == regra_id))
    session.delete(regra)


def _resolver_acao(acao, situacoes, verificacao, fonte, valor, motivo):
    if acao == "USAR_SUGERIDA":
        if not verificacao.fonte_sugerida or verificacao.valor_sugerido is None:
            raise DecisaoRecusada("Não há fonte sugerida para este bloco.")
        _recusar_siconfi(situacoes, verificacao.fonte_sugerida)
        return verificacao.fonte_sugerida, verificacao.valor_sugerido
    if acao == "TROCAR_FONTE":
        if not fonte:
            raise DecisaoRecusada("Escolha a fonte publicada.")
        _recusar_siconfi(situacoes, fonte)
        publicado = json.loads(verificacao.diferencas).get(fonte)
        if not publicado or publicado.get("valor") is None:
            raise DecisaoRecusada("Essa fonte não publicou valor neste bloco.")
        return fonte, publicado["valor"]
    if acao == "TIRAR_ANO":
        if not motivo:
            raise DecisaoRecusada("Tirar o ano exige um motivo.")
        return None, None
    if acao == "VALOR_DIGITADO":
        if not motivo:
            raise DecisaoRecusada("O valor digitado exige um motivo.")
        if valor is None:
            raise DecisaoRecusada("Informe o valor.")
        return None, valor
    raise DecisaoRecusada("Ação desconhecida.")


def _recusar_siconfi(situacoes, fonte):
    if fonte == "SICONFI" and siconfi_bloqueado(situacoes):
        raise DecisaoRecusada("O SICONFI está sem abertura e não pode ser usado.")


def _vigente(session, id_ente, ano, bloco) -> Decisao | None:
    return session.scalar(
        select(Decisao).where(
            Decisao.id_ente == id_ente,
            Decisao.ano == ano,
            Decisao.bloco == bloco,
            Decisao.estado == "VIGENTE",
        )
    )


def _sem_decisao(regra_id: int):
    from validacao.modelos import Ente

    decisao_vigente = (
        select(Decisao.id)
        .where(
            Decisao.id_ente == Verificacao.id_ente,
            Decisao.ano == Verificacao.ano,
            Decisao.bloco == Verificacao.bloco,
            Decisao.estado == "VIGENTE",
        )
        .exists()
    )
    return (
        select(Verificacao, Ente)
        .join(Ente, Ente.id_ente == Verificacao.id_ente)
        .where(
            Verificacao.regra_versao_id == regra_id,
            ~decisao_vigente,
            ~Verificacao.situacoes.contains('"NAO_SE_APLICA"'),
        )
    )


def _expressao_limpa():
    expressao = Verificacao.situacoes.contains('"FECHA"')
    for codigo in VERMELHAS:
        expressao = expressao & ~Verificacao.situacoes.contains(f'"{codigo}"')
    return expressao


def _candidatos_lote(regra_id, esfera, uf, ano, situacao):
    from validacao.modelos import Ente

    consulta = _sem_decisao(regra_id).where(_expressao_limpa())
    if esfera:
        consulta = consulta.where(Ente.esfera == esfera)
    if uf:
        consulta = consulta.where(Ente.uf == uf)
    if ano:
        consulta = consulta.where(Verificacao.ano == ano)
    if situacao:
        consulta = consulta.where(Verificacao.situacoes.contains(f'"{situacao}"'))
    return consulta


def _cargas_vigentes(session: Session) -> dict[str, int]:
    linhas = session.execute(
        select(Carga.fonte_id, func.max(Carga.id))
        .where(Carga.situacao == "CONCLUIDA")
        .group_by(Carga.fonte_id)
    ).all()
    return {fonte: int(carga_id) for fonte, carga_id in linhas}


def _totais_das_cargas(session: Session, cargas: dict[str, int]) -> dict:
    if not cargas:
        return {}
    linhas = session.execute(
        select(
            Carga.fonte_id,
            ValorObservado.id_ente,
            ValorObservado.ano,
            ValorObservado.componente_id,
            ValorObservado.valor,
        )
        .join(Carga, Carga.id == ValorObservado.carga_id)
        .where(
            Carga.id.in_(cargas.values()),
            ValorObservado.componente_id.in_(_BLOCO_DO_TOTAL),
        )
    )
    return {
        (fonte, id_ente, ano, _BLOCO_DO_TOTAL[componente]): valor
        for fonte, id_ente, ano, componente, valor in linhas
        if cargas.get(fonte) is not None
    }


def _individuais(session: Session, regra_id: int) -> set[tuple]:
    vigentes = {
        (id_ente, ano, bloco)
        for id_ente, ano, bloco in session.execute(
            select(Decisao.id_ente, Decisao.ano, Decisao.bloco).where(Decisao.estado == "VIGENTE")
        )
    }
    linhas = session.execute(
        select(Verificacao.id_ente, Verificacao.ano, Verificacao.bloco, Verificacao.situacoes).where(
            Verificacao.regra_versao_id == regra_id
        )
    )
    resultado = set()
    for id_ente, ano, bloco, texto in linhas:
        if (id_ente, ano, bloco) in vigentes:
            continue
        situacoes = situacoes_de(texto)
        if "NAO_SE_APLICA" in situacoes or fecha_limpo(situacoes):
            continue
        resultado.add((id_ente, ano, bloco))
    return resultado
