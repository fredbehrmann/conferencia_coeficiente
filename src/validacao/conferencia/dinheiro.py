"""Valores monetários em centavos inteiros e formatação em português do Brasil.

Todas as comparações usam centavos inteiros. O arredondamento acontece apenas
na exibição.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_ESPACOS = re.compile(r"[\s ]")


class ValorInvalido(ValueError):
    pass


def para_centavos(valor, escala: int = 1) -> int | None:
    """Converte número ou texto em centavos inteiros.

    Aceita "1.234,56", "1234,56", "1234.56", "R$ 1.234", números e Decimal.
    Retorna None para vazio. Levanta ValorInvalido para texto não numérico.
    """
    if valor is None:
        return None
    if isinstance(valor, bool):
        raise ValorInvalido(str(valor))
    if isinstance(valor, int):
        return valor * 100 * escala
    if isinstance(valor, float):
        if math.isnan(valor):
            return None
        if math.isinf(valor):
            raise ValorInvalido(str(valor))
        numero = Decimal(repr(valor))
    elif isinstance(valor, Decimal):
        numero = valor
    else:
        texto = _ESPACOS.sub("", str(valor)).replace("R$", "")
        if texto == "" or texto.lower() in {"nan", "none", "-"}:
            return None
        negativo = False
        if texto.startswith("(") and texto.endswith(")"):
            negativo = True
            texto = texto[1:-1]
        numero = _texto_para_decimal(texto)
        if negativo:
            numero = -numero
    centavos = (numero * escala * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(centavos)


def _texto_para_decimal(texto: str) -> Decimal:
    if not re.fullmatch(r"[+-]?[\d.,]+(e[+-]?\d+)?", texto, flags=re.IGNORECASE):
        raise ValorInvalido(texto)
    virgulas = texto.count(",")
    pontos = texto.count(".")
    if virgulas and pontos:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif virgulas:
        if virgulas > 1:
            raise ValorInvalido(texto)
        texto = texto.replace(",", ".")
    elif pontos > 1:
        texto = texto.replace(".", "")
    try:
        return Decimal(texto)
    except InvalidOperation as erro:
        raise ValorInvalido(texto) from erro


def _agrupar(inteiro: int) -> str:
    return f"{inteiro:,}".replace(",", ".")


def reais(centavos: int | None, casas: int = 2, prefixo: bool = True, vazio: str = "—") -> str:
    if centavos is None:
        return vazio
    sinal = "−" if centavos < 0 else ""
    valor = Decimal(abs(int(centavos))) / 100
    quantizado = valor.quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP)
    inteiro = int(quantizado)
    texto = _agrupar(inteiro)
    if casas:
        fracao = str(quantizado).split(".")[1] if "." in str(quantizado) else "0" * casas
        texto = f"{texto},{fracao}"
    return f"{sinal}{'R$ ' if prefixo else ''}{texto}"


def reais_curto(centavos: int | None, vazio: str = "—") -> str:
    """R$ 16,0 mi / R$ 2,3 bi / R$ 850 mil — para explicações e resumos."""
    if centavos is None:
        return vazio
    valor = Decimal(abs(int(centavos))) / 100
    sinal = "−" if centavos < 0 else ""
    if valor >= Decimal(1_000_000_000):
        numero = valor / Decimal(1_000_000_000)
        return f"{sinal}R$ {_decimal_br(numero, 2)} {'bilhão' if numero < 2 else 'bilhões'}"
    if valor >= Decimal(1_000_000):
        numero = valor / Decimal(1_000_000)
        return f"{sinal}R$ {_decimal_br(numero, 2)} {'milhão' if numero < 2 else 'milhões'}"
    if valor >= Decimal(10_000):
        return f"{sinal}R$ {_decimal_br(valor / Decimal(1_000), 1)} mil"
    return sinal + reais(abs(int(centavos)))


def _decimal_br(valor: Decimal, casas: int) -> str:
    quantizado = valor.quantize(Decimal(1).scaleb(-casas), rounding=ROUND_HALF_UP)
    inteiro, _, fracao = f"{quantizado:f}".partition(".")
    texto = _agrupar(int(inteiro))
    if casas:
        texto = f"{texto},{fracao.ljust(casas, '0')}"
    # remove zeros finais desnecessários (16,00 milhões -> 16 milhões)
    if "," in texto:
        texto = texto.rstrip("0").rstrip(",")
    return texto


def percentual(fracao: float | None, casas: int = 1, sinal: bool = False, vazio: str = "Não calculável") -> str:
    if fracao is None:
        return vazio
    valor = fracao * 100
    if round(abs(valor), casas) == 0:
        valor = 0.0
    texto = f"{abs(valor):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if sinal and valor > 0:
        return f"+{texto}%"
    if valor < 0:
        return f"−{texto}%"
    return f"{texto}%"


def diferenca(valor_fonte: int | None, referencia: int | None) -> dict:
    """Diferenças conforme a especificação.

    assinada = fonte − referência; absoluta = |assinada|;
    percentual = assinada ÷ |referência| (None quando a referência é zero).
    """
    if valor_fonte is None or referencia is None:
        return {"assinada": None, "absoluta": None, "percentual": None}
    assinada = int(valor_fonte) - int(referencia)
    percentual_ = None if referencia == 0 else assinada / abs(referencia)
    return {"assinada": assinada, "absoluta": abs(assinada), "percentual": percentual_}
