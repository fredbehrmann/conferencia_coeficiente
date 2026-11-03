import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.cadastro import garantir_municipio, mapa_de_codigos
from validacao.catalogo import permitidos
from validacao.cargas.comum import (
    abrir_carga,
    acrescentar,
    carga_concluida,
    codigo_inteiro,
    gravar_valores,
    hash_arquivo,
    ler_planilha,
    numero,
    texto,
)
from validacao.modelos import Ente

ARQUIVO = "TCE Arrecadacao Municipios - 2019_2025.xlsx"

ISS = (
    (5, "ISS_PRINCIPAL"),
    (6, "ISS_MULTAS_JUROS"),
    (7, "ISS_DA_PRINCIPAL"),
    (8, "ISS_DA_MULTAS"),
    (9, "ISS_OUTROS"),
    (11, "ISS_FUNDEB"),
    (12, "ISS_OUTRAS_DEDUCOES"),
)
COTA = (
    (15, "COTA_RECEBIDA_BRUTO"),
    (16, "COTA_RECEBIDA_FUNDEB"),
    (17, "COTA_RECEBIDA_OUTRAS_DEDUCOES"),
    (18, "COTA_RECEBIDA_LIQUIDO"),
)
COLUNA_ISS_TOTAL = 13
COLUNA_ISS_PUBLICADO = 10
COLUNA_NATUREZA_ISS = 14
COLUNA_COTA_TOTAL = 20
COLUNA_COTA_UNICO = 19
COLUNA_NATUREZA_COTA = 21
NOMES = (
    "Principal",
    "Multas e juros",
    "Dívida ativa",
    "Multas e juros da dívida ativa",
    "Outros itens / total sem abertura",
    "Total publicado",
    "Dedução FUNDEB",
    "Outras deduções",
    "ISS — valor de referência",
    "Natureza do valor ISS",
    "Bruto",
    "Dedução FUNDEB",
    "Outras deduções",
    "Líquido",
    "Valor único (sem classificação)",
    "Cota-parte — valor de referência",
    "Natureza do valor cota-parte",
)


def carregar(session: Session, caminho: Path) -> dict:
    hash_hex = hash_arquivo(caminho)
    if carga_concluida(session, hash_hex) is not None:
        return {"fonte": "TCE", "situacao": "JA_CARREGADA", "valores": 0}

    print("Lendo TCE...", flush=True)
    _incluir_universo(session, caminho)
    codigos = mapa_de_codigos(session, "TCE")
    esferas = dict(session.execute(select(Ente.id_ente, Ente.esfera)).all())
    detalhe = ler_planilha(caminho, sheet_name="Base detalhada", header=4)
    mapa = {}
    exercicios = set()
    matriz = detalhe.to_numpy()
    for indice, linha in enumerate(matriz):
        id_bruto = codigo_inteiro(linha[2])
        ano = codigo_inteiro(linha[4])
        if id_bruto is None or ano is None:
            continue
        chave = str(id_bruto)
        if chave not in codigos:
            raise KeyError(f"Código TCE sem ente no cadastro: {chave}")
        id_ente = codigos[chave]
        liberados = permitidos(esferas[id_ente])
        exercicios.add(ano)
        excel = indice + 6
        natureza_iss = texto(linha[COLUNA_NATUREZA_ISS])
        natureza_cota = texto(linha[COLUNA_NATUREZA_COTA])
        for coluna, componente in ISS:
            acrescentar(
                mapa,
                id_ente,
                ano,
                componente,
                numero(linha[coluna]),
                natureza_iss,
                NOMES[coluna - 5] if coluna >= 5 else None,
                excel,
                liberados,
            )
        iss_total = numero(linha[COLUNA_ISS_TOTAL])
        if iss_total is None:
            iss_total = numero(linha[COLUNA_ISS_PUBLICADO])
        acrescentar(
            mapa,
            id_ente,
            ano,
            "ISS_TOTAL",
            iss_total,
            natureza_iss,
            "ISS — valor de referência",
            excel,
            liberados,
        )
        for coluna, componente in COTA:
            acrescentar(
                mapa,
                id_ente,
                ano,
                componente,
                numero(linha[coluna]),
                natureza_cota,
                None,
                excel,
                liberados,
            )
        cota_total = numero(linha[COLUNA_COTA_TOTAL])
        if cota_total is None:
            cota_total = numero(linha[COLUNA_COTA_UNICO])
        acrescentar(
            mapa,
            id_ente,
            ano,
            "COTA_RECEBIDA_TOTAL",
            cota_total,
            natureza_cota,
            "Cota-parte — valor de referência",
            excel,
            liberados,
        )
    carga = abrir_carga(session, "TCE", caminho, hash_hex, _data_extracao(caminho), exercicios)
    quantidade = gravar_valores(session, carga.id, mapa)
    return {"fonte": "TCE", "situacao": "CARREGADA", "valores": quantidade, "municipios": len(codigos)}


def _incluir_universo(session: Session, caminho: Path) -> None:
    quadro = ler_planilha(caminho, sheet_name="ISS", header=3)
    conhecidos = set(session.scalars(select(Ente.id_ente)))
    for registro in quadro.to_dict("records"):
        id_ente = codigo_inteiro(registro.get("Cód. IBGE"))
        if id_ente is None:
            continue
        nome = texto(registro.get("Município")) or f"Município {id_ente}"
        garantir_municipio(session, id_ente, nome, conhecidos)


def _data_extracao(caminho: Path) -> datetime:
    leia = ler_planilha(caminho, sheet_name="Leia-me", header=None, nrows=5)
    for valor in leia.to_numpy().ravel():
        if valor is None or (isinstance(valor, float) and pd.isna(valor)):
            continue
        encontrado = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(valor))
        if encontrado:
            dia, mes, ano = (int(parte) for parte in encontrado.groups())
            return datetime(ano, mes, dia)
    return datetime.fromtimestamp(caminho.stat().st_mtime).replace(microsecond=0)
