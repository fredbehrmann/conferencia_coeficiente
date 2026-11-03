from collections import defaultdict

from validacao.cargas.baixar import paginas
from validacao.catalogo import POR_ID
from validacao.de_para import TOTAIS, componente_da_natureza

BASE = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt"
ENTREGUE = {"HO", "RE", "EN"}


def buscar_msc(id_ente: int, ano: int) -> list[dict]:
    url = (
        f"{BASE}/msc_orcamentaria?id_ente={id_ente}&an_referencia={ano}"
        f"&me_referencia=12&co_tipo_matriz=MSCE&classe_conta=6&id_tv=period_change"
    )
    return paginas(url)


def agregar_msc(linhas: list[dict], ano: int) -> dict[str, float]:
    acumulado: dict[str, float] = defaultdict(float)
    for linha in linhas:
        conta = str(linha.get("conta_contabil") or "")
        if not conta.startswith("6212"):
            continue
        componente = componente_da_natureza(str(linha.get("natureza_receita") or ""), ano)
        if componente is None:
            continue
        valor = float(linha.get("valor") or 0)
        sinal = 1.0 if linha.get("natureza_conta") == "D" else -1.0
        acumulado[componente] += sinal * valor
    itens = {componente: valor for componente, valor in acumulado.items() if abs(valor) > 0.005}
    for bloco, total in TOTAIS.items():
        soma = 0.0
        algum = False
        for componente, valor in itens.items():
            definicao = POR_ID.get(componente)
            if definicao is None or definicao.bloco != bloco or componente == total:
                continue
            if definicao.papel == "SOMA":
                soma += valor
                algum = True
            elif definicao.papel == "SUBTRAI":
                soma -= abs(valor)
                algum = True
        if algum:
            itens[total] = soma
    return itens


def buscar_rreo(id_ente: int, ano: int) -> list[dict]:
    url = (
        f"{BASE}/rreo?an_exercicio={ano}&nr_periodo=6&co_tipo_demonstrativo=RREO"
        f"&no_anexo=RREO-Anexo%2001&id_ente={id_ente}"
    )
    return paginas(url)


def agregar_rreo(linhas: list[dict]) -> dict[str, float]:
    escolhidos: dict[str, float] = {}
    for linha in linhas:
        if linha.get("coluna") != "Até o Bimestre (c)":
            continue
        bloco = _bloco_rreo(str(linha.get("conta") or ""))
        if bloco is None or bloco in escolhidos:
            continue
        valor = linha.get("valor")
        if valor is None:
            continue
        escolhidos[TOTAIS[bloco]] = float(valor)
    return escolhidos


def buscar_entrega(id_ente: int, ano: int) -> bool | None:
    url = f"{BASE}/extrato_entregas?id_ente={id_ente}&an_referencia={ano}"
    linhas = paginas(url)
    dca = [
        linha
        for linha in linhas
        if "DCA" in str(linha.get("entregavel") or "").upper()
        or "BALANÇO ANUAL" in str(linha.get("entregavel") or "").upper()
        or "BALANCO ANUAL" in str(linha.get("entregavel") or "").upper()
    ]
    if not dca:
        return False
    return any(str(linha.get("status_relatorio") or "").upper() in ENTREGUE for linha in dca)


def _bloco_rreo(conta: str) -> str | None:
    texto = conta.upper()
    if "IPVA" in texto or "ITCMD" in texto:
        return None
    if "COTA-PARTE DO ICMS" in texto or "COTA PARTE DO ICMS" in texto:
        return "COTA_RECEBIDA"
    if "SIMPLES" in texto and "ISS" in texto:
        return None
    if "ISS" in texto or "IMPOSTO SOBRE SERVI" in texto:
        return "ISS"
    if "ICMS" in texto:
        return "ICMS"
    return None
