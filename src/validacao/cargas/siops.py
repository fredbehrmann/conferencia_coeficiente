from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.cadastro import mapa_de_codigos
from validacao.catalogo import permitidos
from validacao.cargas.comum import (
    abrir_carga,
    acrescentar,
    carga_concluida,
    codigo_inteiro,
    data_do_arquivo,
    gravar_valores,
    hash_arquivo,
    numero,
)
from validacao.modelos import Ente

ARQUIVO = "SIOPS - ICMS e ISS.xlsx"

MUNICIPIOS = {
    "ISS": "ISS_TOTAL",
    "Cota-Parte": "COTA_RECEBIDA_TOTAL",
    "Comp. Fin": "COMPENSACAO",
}
ESTADOS = {
    "ICMS": "ICMS_TOTAL",
    "Cota-Parte": "COTA_REPASSADA",
    "Comp. Fin": "COMPENSACAO",
}


def carregar(session: Session, caminho: Path) -> dict:
    hash_hex = hash_arquivo(caminho)
    if carga_concluida(session, hash_hex) is not None:
        return {"fonte": "SIOPS", "situacao": "JA_CARREGADA", "valores": 0}

    print("Lendo SIOPS...", flush=True)
    codigos = mapa_de_codigos(session, "SIOPS")
    esferas = dict(session.execute(select(Ente.id_ente, Ente.esfera)).all())
    mapa = {}
    exercicios = set()
    municipios = pd.read_excel(caminho, sheet_name="Municipios")
    estados = pd.read_excel(caminho, sheet_name="Estados")
    _quadro(mapa, exercicios, municipios, MUNICIPIOS, codigos, esferas)
    _quadro(mapa, exercicios, estados, ESTADOS, codigos, esferas)
    carga = abrir_carga(session, "SIOPS", caminho, hash_hex, data_do_arquivo(caminho), exercicios)
    quantidade = gravar_valores(session, carga.id, mapa)
    return {"fonte": "SIOPS", "situacao": "CARREGADA", "valores": quantidade}


def _quadro(mapa, exercicios, quadro: pd.DataFrame, colunas, codigos, esferas):
    for deslocamento, registro in enumerate(quadro.to_dict("records")):
        codigo = codigo_inteiro(registro.get("COD_IBGE"))
        ano = codigo_inteiro(registro.get("ANO"))
        if codigo is None or ano is None:
            continue
        chave = str(codigo)
        if chave not in codigos:
            raise KeyError(f"Código SIOPS sem ente no cadastro: {chave}")
        id_ente = codigos[chave]
        liberados = permitidos(esferas[id_ente])
        exercicios.add(ano)
        linha = deslocamento + 2
        for origem, componente in colunas.items():
            acrescentar(
                mapa,
                id_ente,
                ano,
                componente,
                numero(registro.get(origem)),
                None,
                origem,
                linha,
                liberados,
            )
