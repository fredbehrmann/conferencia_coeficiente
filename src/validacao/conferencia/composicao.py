"""Composição do valor de cada fonte no painel do caso: bruto, deduções e o líquido resultante.

Mostra, por declaração, as parcelas registradas (com procedência), a conta que leva ao valor
total e as medidas que o sistema derivou para comparar (líquido, bruto, líquido de FUNDEB).
Para a MSC, soma os lançamentos por conta contábil; para o RREO, mostra a linha do Anexo 3.
"""

from __future__ import annotations

import json
from collections import defaultdict

from validacao.conferencia.catalogo import PARCELAS
from validacao.conferencia.comparacao import RegistroV, montar_fonte
from validacao.conferencia.dinheiro import para_centavos, reais

# papel de cada parcela na conta
SOMA_BRUTO = ("BRUTO", "FECOP")
SOMA_ITENS = ("PRINCIPAL", "MULTAS_JUROS", "DA_PRINCIPAL", "DA_MULTAS", "OUTROS")
DEDUZ = ("OUTRAS_DEDUCOES", "FECOP_OUTRAS_DEDUCOES")
FUNDEB = ("FUNDEB", "FECOP_FUNDEB")
INFORMATIVAS = ("DEDUCAO_CONST", "FECOP_DEDUCAO_CONST", "COTA_REPASSADA")
CONCEITO_TEXTO = {"LIQUIDO": "líquido de deduções", "BRUTO": "bruto", "INDEFINIDO": "conceito não informado",
                  "LIQUIDO_FUNDEB": "líquido também do FUNDEB"}
CONTAS_MSC = {
    "6212": "Receita realizada (6.2.1.2)",
    "62131": "(−) Dedução para o FUNDEB (6.2.1.3.1)",
    "62132": "(−) Deduções da receita (6.2.1.3.2)",
    "62139": "(−) Outras deduções da receita (6.2.1.3.9)",
}


def _origem(registro, lote) -> str:
    p = json.loads(registro.procedencia or "{}")
    partes = []
    arquivo = p.get("arquivo") or (lote.arquivo.replace("\\", "/").rsplit("/", 1)[-1] if lote and lote.arquivo else "")
    if arquivo:
        partes.append(arquivo)
    for chave, rotulo in (("aba", "aba"), ("linha", "linha"), ("coluna", "coluna")):
        if p.get(chave):
            partes.append(f"{rotulo} {p[chave]}")
    return " · ".join(str(x) for x in partes)


def _linha(rotulo, campo, valor, sinal, origem="", destaque=False):
    return {"rotulo": rotulo, "campo": campo, "valor": valor, "sinal": sinal, "origem": origem, "destaque": destaque}


def _registros_vigentes(info, declaracao):
    return [(r, l) for r, l in info["registros"] if r.declaracao_id == declaracao and r.vigente and r.periodo == "ANUAL"]


def composicao_fonte(info: dict, declaracao: str, detalhes: dict | None = None) -> dict | None:
    pares = _registros_vigentes(info, declaracao)
    if not pares:
        return None
    por_parcela: dict[str, tuple] = {}
    for r, l in sorted(pares, key=lambda par: par[0].id):
        if r.parcela and r.parcela not in por_parcela:
            por_parcela[r.parcela] = (r, l)

    linhas = []
    bruto = 0
    tem_bruto = False
    for parcela in SOMA_BRUTO:
        if parcela in por_parcela and por_parcela[parcela][0].conceito == "BRUTO":
            r, l = por_parcela[parcela]
            if r.valor_centavos is not None:
                linhas.append(_linha(PARCELAS.get(parcela, parcela), r.campo, r.valor_centavos, "+", _origem(r, l)))
                bruto += r.valor_centavos
                tem_bruto = True
    itens = 0
    tem_itens = False
    for parcela in SOMA_ITENS + ("FECOP",):
        if parcela in por_parcela and por_parcela[parcela][0].conceito == "ITEM":
            r, l = por_parcela[parcela]
            if r.valor_centavos is not None:
                linhas.append(_linha(PARCELAS.get(parcela, parcela), r.campo, r.valor_centavos, "+", _origem(r, l)))
                itens += r.valor_centavos
                tem_itens = True
    if tem_itens and not tem_bruto:
        bruto, tem_bruto = itens, True
    deducoes = 0
    tem_deducoes = False
    for parcela in DEDUZ:
        if parcela in por_parcela:
            r, l = por_parcela[parcela]
            if r.valor_centavos is not None:
                linhas.append(_linha(PARCELAS.get(parcela, parcela), r.campo, -abs(r.valor_centavos), "−", _origem(r, l)))
                deducoes += abs(r.valor_centavos)
                tem_deducoes = True

    conta = []
    if tem_bruto:
        if len([x for x in linhas if x["sinal"] == "+"]) > 1:
            conta.append(_linha("Receita bruta (soma das parcelas acima)", "", bruto, "="))
        if tem_deducoes:
            conta.append(_linha("(−) Deduções que formam o líquido", "", -deducoes, "−"))
            conta.append(_linha("Líquido calculado (bruto − deduções)", "", bruto - deducoes, "=", destaque=True))
        else:
            conta.append(_linha("Líquido calculado (nenhuma dedução registrada: igual ao bruto)", "", bruto, "=", destaque=True))

    fundeb_total = 0
    fundeb_linhas = []
    for parcela in FUNDEB:
        if parcela in por_parcela:
            r, l = por_parcela[parcela]
            if r.valor_centavos is not None:
                fundeb_linhas.append(_linha(PARCELAS.get(parcela, parcela), r.campo, -abs(r.valor_centavos), "−", _origem(r, l)))
                fundeb_total += abs(r.valor_centavos)
    informativas = []
    for parcela in INFORMATIVAS:
        if parcela in por_parcela:
            r, l = por_parcela[parcela]
            if r.valor_centavos is not None:
                informativas.append(_linha(PARCELAS.get(parcela, parcela), r.campo, r.valor_centavos, "i", _origem(r, l)))

    total = por_parcela.get("TOTAL")
    declarado = None
    conferencia = ""
    if total is not None:
        r, l = total
        declarado = _linha(f"Total declarado ({CONCEITO_TEXTO.get(r.conceito, r.conceito or '—')})", r.campo,
                           r.valor_centavos, "■", _origem(r, l), destaque=True)
        if tem_bruto and r.valor_centavos is not None:
            calculado = bruto - deducoes
            alvo = calculado - fundeb_total if r.conceito == "LIQUIDO_FUNDEB" else (bruto if r.conceito == "BRUTO" else calculado)
            dif = r.valor_centavos - alvo
            if dif == 0:
                conferencia = "O total declarado confere, ao centavo, com a conta acima."
            elif r.conceito == "INDEFINIDO" and r.valor_centavos in (bruto, calculado, calculado - fundeb_total):
                conferencia = "O total declarado coincide com uma das medidas calculadas (conceito não informado pela fonte)."
            else:
                conferencia = f"O total declarado difere da conta acima em {reais(dif)}."
    liq_fundeb = por_parcela.get("LIQUIDO_FUNDEB")
    declarado_fundeb = None
    if liq_fundeb is not None:
        r, l = liq_fundeb
        declarado_fundeb = _linha("Líquido de FUNDEB declarado", r.campo, r.valor_centavos, "■", _origem(r, l))

    # medidas que o sistema deriva para comparar (mesma lógica da validação)
    fonte = montar_fonte(declaracao, [
        RegistroV(id=r.id, declaracao=r.declaracao_id, campo=r.campo, parcela=r.parcela, conceito=r.conceito,
                  valor=r.valor_centavos, estado=r.estado, periodo=r.periodo, versao=r.versao, vigente=r.vigente,
                  duplicado=r.duplicado, lote_id=r.lote_id, data_extracao=r.data_extracao)
        for r, _l in pares
    ])
    medidas = [
        {"conceito": CONCEITO_TEXTO.get(m.conceito, m.conceito), "valor": m.valor, "derivacao": m.derivacao}
        for m in fonte.medidas.values()
    ]
    item = info["valores"].get(declaracao) or {}
    usado = None
    if item.get("normalizado") is not None:
        usado = {"valor": item["normalizado"], "parcela": item.get("parcela") or "", "derivacao": item.get("derivacao") or "",
                 "referencia": item.get("comparabilidade") == "REFERENCIA"}

    resultado = {
        "linhas": linhas, "conta": conta, "declarado": declarado, "declarado_fundeb": declarado_fundeb,
        "fundeb": fundeb_linhas, "fundeb_total": fundeb_total,
        "liquido_fundeb_calculado": (bruto - deducoes - fundeb_total) if (tem_bruto and fundeb_total) else None,
        "informativas": informativas, "conferencia": conferencia, "medidas": medidas, "usado": usado,
        "sem_abertura": not (linhas or fundeb_linhas or informativas), "extra": None,
    }
    if declaracao == "SICONFI_MSC" and detalhes and detalhes.get("msc"):
        resultado["extra"] = _msc(detalhes["msc"])
    if declaracao == "SICONFI_RREO" and detalhes and detalhes.get("rreo"):
        resultado["extra"] = _rreo(detalhes["rreo"])
    return resultado


def _num(texto) -> int:
    try:
        return para_centavos(str(texto).strip()) or 0
    except Exception:
        return 0


def _msc(linhas: list[dict]) -> dict:
    grupos: dict[str, dict] = defaultdict(lambda: {"valor": 0, "n": 0, "naturezas": defaultdict(int)})
    tipos = set()
    for l in linhas:
        conta = str(l.get("conta_contabil", ""))
        chave = next((k for k in sorted(CONTAS_MSC, key=len, reverse=True) if conta.startswith(k)), conta[:5])
        valor = _num(l.get("valor_sinalizado"))
        grupos[chave]["valor"] += valor
        grupos[chave]["n"] += 1
        grupos[chave]["naturezas"][str(l.get("natureza_receita", ""))] += valor
        if l.get("tipo_valor"):
            tipos.add(str(l["tipo_valor"]))
    contas = []
    for chave in sorted(grupos):
        g = grupos[chave]
        contas.append({
            "rotulo": CONTAS_MSC.get(chave, f"Conta {chave}"), "conta": chave, "valor": g["valor"], "n": g["n"],
            "naturezas": sorted(g["naturezas"].items()),
        })
    return {"tipo": "msc", "contas": contas, "total": sum(g["valor"] for g in grupos.values()), "tipos_valor": sorted(tipos),
            "lancamentos": len(linhas)}


def _rreo(linhas: list[dict]) -> dict:
    return {"tipo": "rreo", "linhas": [
        {"conta": l.get("cod_conta", ""), "rotulo": l.get("conta", ""), "coluna": l.get("coluna", ""), "anexo": l.get("anexo", ""),
         "valor": _num(l.get("valor"))}
        for l in linhas
    ]}


def composicoes(info: dict, detalhes: dict | None, ordem: list[str]) -> dict[str, dict]:
    resultado = {}
    for declaracao in ordem:
        try:
            item = composicao_fonte(info, declaracao, detalhes)
        except Exception as erro:  # auxiliar: não derruba o painel
            item = {"erro": str(erro)}
        if item is not None:
            resultado[declaracao] = item
    return resultado
