"""Registro das conclusões do analista, com histórico e sinal de reanálise.

O estado do diagnóstico (o que as regras encontraram) é separado do estado da
análise (o que o analista concluiu). Um caso pode continuar divergente e estar
esclarecido. Novas decisões não apagam as anteriores.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.comparacao import sigla
from validacao.conferencia.execucao import expandir_valores, ultima_execucao
from validacao.conferencia.regras import MEDIA

ACOES = {
    "MANTER_PRINCIPAL": {
        "rotulo": "Manter o valor principal",
        "ajuda": "Após conferência, o valor da fonte principal é adotado.",
        "situacao": "ESCLARECIDO",
    },
    "ADOTAR_FONTE": {
        "rotulo": "Adotar valor de outra fonte",
        "ajuda": "Escolha o registro de outra declaração. Exige justificativa.",
        "situacao": "AJUSTADO",
    },
    "CORRIGIR_VALOR": {
        "rotulo": "Corrigir o valor de trabalho",
        "ajuda": "Informe o valor e o documento que o fundamenta. Exige justificativa.",
        "situacao": "AJUSTADO",
    },
    "DIFERENCA_CONCEITO": {
        "rotulo": "Reconhecer diferença de conceito",
        "ajuda": "A divergência decorre de conceito ou abrangência; o valor principal é mantido.",
        "situacao": "ESCLARECIDO",
    },
    "SOLICITAR_ESCLARECIMENTO": {
        "rotulo": "Solicitar esclarecimento",
        "ajuda": "Registra o pedido ao ente ou à fonte. O valor adotado não muda.",
        "situacao": "AGUARDANDO_ESCLARECIMENTO",
    },
    "MANTER_PENDENTE": {
        "rotulo": "Manter pendente",
        "ajuda": "Registra uma anotação sem concluir.",
        "situacao": "PENDENTE",
    },
    "DESCONSIDERAR": {
        "rotulo": "Desconsiderar para a apuração",
        "ajuda": "O valor fica inelegível; o registro original é preservado. Exige justificativa.",
        "situacao": "DESCONSIDERADO",
    },
}

SITUACOES = {
    "PENDENTE": ("Pendente de análise", "pendente"),
    "REANALISE": ("Reanálise necessária", "reanalise"),
    "AGUARDANDO_ESCLARECIMENTO": ("Aguardando esclarecimento", "aguardando"),
    "ESCLARECIDO": ("Esclarecido", "esclarecido"),
    "AJUSTADO": ("Valor ajustado", "ajustado"),
    "DESCONSIDERADO": ("Desconsiderado", "desconsiderado"),
    "SEM_PENDENCIA": ("Sem alerta, sem decisão", "neutro"),
    "NAO_EXIGIVEL": ("Não exigível", "neutro"),
    "EXCLUIDO": ("Fora do universo", "neutro"),
}
SITUACOES_ABERTAS = ("PENDENTE", "REANALISE", "AGUARDANDO_ESCLARECIMENTO")

ACOES_COM_JUSTIFICATIVA = ("ADOTAR_FONTE", "CORRIGIR_VALOR", "DESCONSIDERAR", "DIFERENCA_CONCEITO", "SOLICITAR_ESCLARECIMENTO")


class DecisaoRecusada(ValueError):
    pass


def situacao_do_caso(caso: cf.Caso, decisao: cf.DecisaoCaso | None) -> str:
    if decisao is None:
        if caso.diagnostico in ("ALERTA", "SEM_VALIDACAO"):
            return "PENDENTE"
        if caso.diagnostico == "CONFERIDO":
            return "SEM_PENDENCIA"
        return caso.diagnostico
    if decisao.impressao and decisao.impressao != caso.impressao:
        return "REANALISE"
    return decisao.situacao_analise


def decisao_vigente(session: Session, id_ente: int, exercicio: int, componente: str) -> cf.DecisaoCaso | None:
    return session.scalar(
        select(cf.DecisaoCaso).where(
            cf.DecisaoCaso.id_ente == id_ente,
            cf.DecisaoCaso.exercicio == exercicio,
            cf.DecisaoCaso.componente_id == componente,
            cf.DecisaoCaso.vigente.is_(True),
        )
    )


def historico(session: Session, id_ente: int, exercicio: int, componente: str) -> list[cf.DecisaoCaso]:
    return list(
        session.scalars(
            select(cf.DecisaoCaso)
            .where(
                cf.DecisaoCaso.id_ente == id_ente,
                cf.DecisaoCaso.exercicio == exercicio,
                cf.DecisaoCaso.componente_id == componente,
            )
            .order_by(cf.DecisaoCaso.id.desc())
        )
    )


def registrar(
    session: Session,
    caso: cf.Caso,
    acao: str,
    usuario: str,
    usuario_id: int | None = None,
    registro_id: int | None = None,
    valor: int | None = None,
    justificativa: str = "",
    evidencias: str = "",
) -> cf.DecisaoCaso:
    if acao not in ACOES:
        raise DecisaoRecusada("Ação desconhecida.")
    justificativa = (justificativa or "").strip()
    evidencias = (evidencias or "").strip()
    exige = acao in ACOES_COM_JUSTIFICATIVA or (acao == "MANTER_PRINCIPAL" and caso.gravidade_max >= MEDIA)
    if exige and len(justificativa) < 10:
        raise DecisaoRecusada("Esta decisão exige justificativa (mínimo de 10 caracteres).")
    anterior = decisao_vigente(session, caso.id_ente, caso.exercicio, caso.componente_id)
    adotado_anterior = None if anterior is None else anterior.valor_adotado
    valores = expandir_valores(caso.valores)
    declaracao = None
    registro = None
    elegivel = True
    if acao in ("MANTER_PRINCIPAL", "DIFERENCA_CONCEITO"):
        if caso.valor_principal is None:
            raise DecisaoRecusada("Não há valor na fonte principal para manter. Use outra ação.")
        adotado = caso.valor_principal
        declaracao = caso.declaracao_principal
        registro = caso.registro_principal_id
    elif acao == "ADOTAR_FONTE":
        if registro_id is None:
            raise DecisaoRecusada("Escolha o registro da fonte a adotar.")
        item = session.get(cf.Registro, registro_id)
        if (
            item is None or item.id_ente != caso.id_ente or item.exercicio != caso.exercicio
            or item.componente_id != caso.componente_id or not item.vigente
        ):
            raise DecisaoRecusada("O registro escolhido não pertence a este caso ou não é a versão vigente.")
        if item.valor_centavos is None:
            raise DecisaoRecusada("O registro escolhido não tem valor.")
        adotado = item.valor_centavos
        declaracao = item.declaracao_id
        registro = item.id
    elif acao == "CORRIGIR_VALOR":
        if valor is None:
            raise DecisaoRecusada("Informe o valor corrigido.")
        if not evidencias:
            raise DecisaoRecusada("Informe o documento ou a evidência que fundamenta a correção.")
        adotado = valor
        declaracao = "CORRECAO"
    elif acao == "DESCONSIDERAR":
        adotado = None
        elegivel = False
    else:  # SOLICITAR_ESCLARECIMENTO, MANTER_PENDENTE
        adotado = adotado_anterior
        declaracao = None if anterior is None else anterior.declaracao_adotada
        registro = None if anterior is None else anterior.registro_adotado_id
        elegivel = True if anterior is None else anterior.elegivel
    if anterior is not None:
        anterior.vigente = False
        session.flush()
    analisados = {
        d: {"registro": v.get("registro"), "versao": v.get("versao"), "original": v.get("original"), "estado": v.get("estado")}
        for d, v in valores.items()
    }
    decisao = cf.DecisaoCaso(
        id_ente=caso.id_ente,
        exercicio=caso.exercicio,
        componente_id=caso.componente_id,
        acao=acao,
        situacao_analise=ACOES[acao]["situacao"],
        valor_anterior=adotado_anterior,
        valor_adotado=adotado,
        declaracao_adotada=declaracao,
        registro_adotado_id=registro,
        elegivel=elegivel,
        justificativa=justificativa,
        evidencias=evidencias,
        usuario=usuario,
        usuario_id=usuario_id,
        criado_em=datetime.now().replace(microsecond=0),
        execucao_id=caso.execucao_id,
        conjunto_id=_conjunto_da_execucao(session, caso.execucao_id),
        impressao=caso.impressao,
        registros_analisados=json.dumps(analisados, ensure_ascii=False),
        vigente=True,
        substitui_id=None if anterior is None else anterior.id,
        origem="ANALISTA",
    )
    session.add(decisao)
    session.flush()
    return decisao


def _conjunto_da_execucao(session: Session, execucao_id: int) -> int | None:
    execucao = session.get(cf.Execucao, execucao_id)
    return None if execucao is None else execucao.conjunto_id


def aceitar_sem_alerta(session: Session, usuario: str, usuario_id: int | None, filtros_ids: list[int]) -> int:
    """Registra 'manter valor principal' para casos conferidos sem alerta e sem decisão.

    É uma decisão humana em lote: cada caso recebe sua própria decisão, com o
    responsável e a execução de validação que a fundamenta.
    """
    execucao = ultima_execucao(session)
    if execucao is None:
        return 0
    quantidade = 0
    for caso in session.scalars(select(cf.Caso).where(cf.Caso.id.in_(filtros_ids))):
        if caso.diagnostico != "CONFERIDO" or caso.valor_principal is None:
            continue
        if decisao_vigente(session, caso.id_ente, caso.exercicio, caso.componente_id) is not None:
            continue
        registrar(
            session, caso, "MANTER_PRINCIPAL", usuario, usuario_id,
            justificativa=(
                f"Conferido sem alertas na execução {caso.execucao_id}. Valor da {sigla(caso.declaracao_principal)} "
                "mantido em lote pelo analista."
            ),
        )
        quantidade += 1
    return quantidade


def descricao_decisao(decisao: cf.DecisaoCaso) -> str:
    rotulo = ACOES.get(decisao.acao, {}).get("rotulo", decisao.acao)
    return rotulo


def junta_decisao():
    """Condição de junção entre caso e decisão vigente (para consultas)."""
    return and_(
        cf.DecisaoCaso.id_ente == cf.Caso.id_ente,
        cf.DecisaoCaso.exercicio == cf.Caso.exercicio,
        cf.DecisaoCaso.componente_id == cf.Caso.componente_id,
        cf.DecisaoCaso.vigente.is_(True),
    )
