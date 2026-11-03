"""De-para da natureza da receita para os itens do catálogo.

O oitavo dígito separa o item: 1 principal, 2 multas e juros, 3 dívida ativa,
4 multas e juros da dívida ativa, 7 ou 9 dedução. Há um regime até 2021 e outro
a partir de 2022; as famílias abaixo valem nos dois.
"""

from __future__ import annotations

ITENS = {
    "1": "PRINCIPAL",
    "2": "MULTAS",
    "3": "DIVIDA",
    "4": "MULTAS_DA",
    "7": "DEDUCAO",
    "9": "DEDUCAO",
}

FAMILIAS = {
    "ATE_2021": {
        "111451": "ISS",
        "111452": "ISS",
        "111250": "ICMS",
        "111251": "ICMS",
        "172150": "COTA_RECEBIDA",
    },
    "DESDE_2022": {
        "111451": "ISS",
        "111452": "ISS",
        "111250": "ICMS",
        "111251": "ICMS",
        "111253": "ICMS",
        "172150": "COTA_RECEBIDA",
    },
}

COMPONENTES = {
    ("ISS", "PRINCIPAL"): "ISS_PRINCIPAL",
    ("ISS", "MULTAS"): "ISS_MULTAS_JUROS",
    ("ISS", "DIVIDA"): "ISS_DA_PRINCIPAL",
    ("ISS", "MULTAS_DA"): "ISS_DA_MULTAS",
    ("ISS", "DEDUCAO"): "ISS_OUTRAS_DEDUCOES",
    ("ISS", "OUTROS"): "ISS_OUTROS",
    ("ICMS", "PRINCIPAL"): "ICMS_PRINCIPAL",
    ("ICMS", "MULTAS"): "ICMS_MULTAS_JUROS",
    ("ICMS", "DIVIDA"): "ICMS_DA_PRINCIPAL",
    ("ICMS", "MULTAS_DA"): "ICMS_DA_MULTAS",
    ("ICMS", "DEDUCAO"): "ICMS_OUTRAS_DEDUCOES",
    ("COTA_RECEBIDA", "PRINCIPAL"): "COTA_RECEBIDA_BRUTO",
    ("COTA_RECEBIDA", "DEDUCAO"): "COTA_RECEBIDA_OUTRAS_DEDUCOES",
}

TOTAIS = {
    "ISS": "ISS_TOTAL",
    "ICMS": "ICMS_TOTAL",
    "COTA_RECEBIDA": "COTA_RECEBIDA_TOTAL",
}


def regime_de(ano: int) -> str:
    return "ATE_2021" if int(ano) < 2022 else "DESDE_2022"


def digitos(natureza: str) -> str:
    return "".join(caractere for caractere in str(natureza) if caractere.isdigit())


def componente_da_natureza(natureza: str, ano: int) -> str | None:
    codigo = digitos(natureza)
    if len(codigo) < 7:
        return None
    familia = codigo[:6]
    bloco = FAMILIAS[regime_de(ano)].get(familia)
    if bloco is None:
        return None
    item = ITENS.get(codigo[-1])
    if item is None:
        return "ISS_OUTROS" if bloco == "ISS" else None
    return COMPONENTES.get((bloco, item))
