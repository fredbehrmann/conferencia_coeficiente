import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.modelos import RegraVersao

PARAMETROS_INICIAIS = {
    "anos": [2019, 2020, 2021, 2022, 2023, 2024, 2025],
    "igualdade_absoluta": 1.0,
    "igualdade_relativa": 0.0001,
    "aceite_sugerido": 0.01,
    "fila_obrigatoria": 0.05,
    "fundeb_liquido_min": 0.79,
    "fundeb_liquido_max": 0.81,
    "salto_min": 0.10,
    "salto_max": 10.0,
    "fundeb_permanece": True,
    "compensacao_soma": False,
    "iss_simples_soma": False,
    "revisao_corte": None,
}


def garantir_regra(session: Session) -> RegraVersao:
    existente = session.scalar(
        select(RegraVersao)
        .where(RegraVersao.vigente_desde.is_not(None))
        .order_by(RegraVersao.id.desc())
        .limit(1)
    )
    if existente is not None:
        return existente
    regra = RegraVersao(
        vigente_desde=datetime.now().replace(microsecond=0),
        parametros=json.dumps(PARAMETROS_INICIAIS),
    )
    session.add(regra)
    session.flush()
    return regra


def parametros_de(regra: RegraVersao) -> dict:
    dados = json.loads(regra.parametros)
    dados.setdefault("revisao_corte", None)
    return dados
