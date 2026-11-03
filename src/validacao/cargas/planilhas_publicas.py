import csv
from pathlib import Path

import pandas as pd

from validacao.cadastro import UF_POR_CODIGO
from validacao.cargas.comum import codigo_inteiro, numero, texto

SIGLA_PARA_ENTE = {sigla: codigo for codigo, sigla in UF_POR_CODIGO.items()}
URL_CONFAZ = "https://www.confaz.fazenda.gov.br/boletim-arrecadacao/20260623_dados-abertos.xlsx"


def ler_confaz(caminho: Path) -> dict[tuple[int, int, str], float]:
    quadro = pd.read_excel(caminho, sheet_name="arrecadação", header=1)
    coluna = next(nome for nome in quadro.columns if str(nome).startswith("TOTAL DO ICMS"))
    quadro = quadro.dropna(subset=["Descrição da UF", "ano"])
    agrupado = quadro.groupby(["Descrição da UF", "ano"], as_index=False)[coluna].sum()
    mapa: dict[tuple[int, int, str], float] = {}
    for linha in agrupado.itertuples(index=False):
        sigla = str(linha[0]).strip().upper()
        id_ente = SIGLA_PARA_ENTE.get(sigla)
        if id_ente is None:
            continue
        mapa[(id_ente, int(linha[1]), "ICMS_TOTAL")] = float(linha[2])
    return mapa


def ler_sefaz(caminho: Path) -> dict[tuple[int, int, str], float]:
    mapa: dict[tuple[int, int, str], float] = {}
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            id_ente = codigo_inteiro(linha.get("id_ente"))
            ano = codigo_inteiro(linha.get("ano"))
            if id_ente is None or ano is None:
                continue
            repassada = numero(linha.get("cota_repassada"))
            recebida = numero(linha.get("cota_recebida"))
            if repassada is not None:
                mapa[(id_ente, ano, "COTA_REPASSADA")] = repassada
            if recebida is not None:
                mapa[(id_ente, ano, "COTA_RECEBIDA_TOTAL")] = recebida
    return mapa


def ler_tesouro(caminho: Path) -> dict[tuple[int, int, str], float]:
    return _ler_tipos(
        caminho,
        {
            "lc87": "COMPENSACAO_LC87",
            "ipi_exportacao": "COMPENSACAO_IPI_EXP",
            "fex": "COMPENSACAO_FEX",
        },
    )


def ler_anp(caminho: Path) -> dict[tuple[int, int, str], float]:
    return _ler_tipos(caminho, {"royalties": "COMPENSACAO_ROYALTIES"})


def ler_anm(caminho: Path) -> dict[tuple[int, int, str], float]:
    return _ler_tipos(caminho, {"cfem": "COMPENSACAO_CFEM"})


def ler_simples(caminho: Path) -> dict[tuple[int, int, str], float]:
    mapa: dict[tuple[int, int, str], float] = {}
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            id_ente = codigo_inteiro(linha.get("id_ente"))
            ano = codigo_inteiro(linha.get("ano"))
            valor = numero(linha.get("valor"))
            if id_ente is None or ano is None or valor is None:
                continue
            origem = (texto(linha.get("origem")) or "").upper()
            if origem == "RFB" and ano >= 2019:
                continue
            if origem == "BB" and ano < 2019:
                continue
            if not origem and ano < 2019:
                continue
            mapa[(id_ente, ano, "ISS_SIMPLES")] = valor
    return mapa


def ler_tribunal(caminho: Path) -> dict[tuple[int, int, str], tuple[float, str | None]]:
    mapa: dict[tuple[int, int, str], tuple[float, str | None]] = {}
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            id_ente = codigo_inteiro(linha.get("id_ente"))
            ano = codigo_inteiro(linha.get("ano"))
            componente = texto(linha.get("componente_id"))
            valor = numero(linha.get("valor"))
            if id_ente is None or ano is None or componente is None or valor is None:
                continue
            mapa[(id_ente, ano, componente)] = (valor, texto(linha.get("natureza")))
    return mapa


def _ler_tipos(caminho: Path, colunas: dict[str, str]) -> dict[tuple[int, int, str], float]:
    mapa: dict[tuple[int, int, str], float] = {}
    with caminho.open(encoding="utf-8", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            id_ente = codigo_inteiro(linha.get("id_ente"))
            ano = codigo_inteiro(linha.get("ano"))
            if id_ente is None or ano is None:
                continue
            for coluna, componente in colunas.items():
                valor = numero(linha.get(coluna))
                if valor is None:
                    continue
                mapa[(id_ente, ano, componente)] = valor
            total = sum(
                numero(linha.get(coluna)) or 0.0
                for coluna in colunas
                if numero(linha.get(coluna)) is not None
            )
            if total:
                mapa[(id_ente, ano, "COMPENSACAO")] = total
    return mapa
