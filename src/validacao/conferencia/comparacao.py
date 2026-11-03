"""Comparabilidade e normalização antes da comparação numérica.

Cada declaração de um caso produz "medidas": o valor do componente expresso em
um conceito (LIQUIDO, BRUTO ou INDEFINIDO), com a derivação que levou a ele.
Duas fontes só são comparadas numa parcela comum. Quando não há parcela comum,
o par é "Não comparável diretamente" e a razão é registrada.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from validacao.conferencia.catalogo import PARCELAS_DEDUCAO, PARCELAS_SOMA, SIGLAS
from validacao.conferencia.dinheiro import diferenca, reais

ESTADOS_COM_VALOR = ("VALOR", "ZERO")
ROTULO_CONCEITO = {"LIQUIDO": "líquido", "BRUTO": "bruto", "INDEFINIDO": "sem conceito informado", "LIQUIDO_FUNDEB": "líquido de FUNDEB"}
ROTULO_PARCELA_COMPARADA = {
    "LIQUIDO": "valor líquido de deduções",
    "BRUTO": "receita bruta, antes de deduções",
    "LIQUIDO_FUNDEB": "valor líquido também da dedução para o FUNDEB",
}


@dataclass
class RegistroV:
    id: int
    declaracao: str
    campo: str
    parcela: str | None
    conceito: str | None
    valor: int | None
    estado: str
    periodo: str = "ANUAL"
    versao: int = 1
    vigente: bool = True
    duplicado: str | None = None
    lote_id: int | None = None
    data_extracao: object = None
    procedencia: str = "{}"
    valor_texto: str | None = None


@dataclass
class Medida:
    conceito: str
    valor: int
    derivacao: str
    registros: list[int] = field(default_factory=list)
    derivada: bool = False


@dataclass
class FonteCaso:
    declaracao: str
    registros: list[RegistroV]
    total: RegistroV | None
    medidas: dict[str, Medida]
    composicao: int | None
    composicao_texto: str
    composicao_conceito: str | None


@dataclass
class Comparacao:
    declaracao: str
    referencia: str
    situacao: str  # SIM | RESSALVA | NAO | SEM_DADO
    conceito: str | None = None
    valor_ref: int | None = None
    valor_fonte: int | None = None
    derivacao_ref: str = ""
    derivacao_fonte: str = ""
    motivo: str = ""
    parcela: str = ""
    assinada: int | None = None
    absoluta: int | None = None
    percentual: float | None = None
    inferencia: str = ""


def sigla(declaracao: str | None) -> str:
    if declaracao is None:
        return "—"
    return SIGLAS.get(declaracao, declaracao)


def montar_fonte(declaracao: str, registros: list[RegistroV]) -> FonteCaso:
    vigentes = [r for r in registros if r.vigente and r.periodo == "ANUAL"]
    por_parcela: dict[str, RegistroV] = {}
    for registro in sorted(vigentes, key=lambda r: r.id):
        if registro.parcela and registro.parcela not in por_parcela:
            por_parcela[registro.parcela] = registro
    total = por_parcela.get("TOTAL")
    medidas: dict[str, Medida] = {}
    if total is not None and total.estado in ESTADOS_COM_VALOR and total.conceito in ("LIQUIDO", "BRUTO", "INDEFINIDO", "LIQUIDO_FUNDEB"):
        medidas[total.conceito] = Medida(total.conceito, total.valor or 0, "valor declarado", [total.id])

    bruto = por_parcela.get("BRUTO")
    if bruto is not None and bruto.estado in ESTADOS_COM_VALOR and "BRUTO" not in medidas:
        valor = bruto.valor or 0
        partes = [f"receita bruta {reais(valor)}"]
        ids = [bruto.id]
        adicional = por_parcela.get("FECOP")
        if adicional is not None and adicional.conceito == "BRUTO" and adicional.valor:
            valor += adicional.valor
            partes.append(f"adicional {reais(adicional.valor)}")
            ids.append(adicional.id)
        texto = " + ".join(partes) if len(partes) > 1 else "receita bruta declarada"
        medidas["BRUTO"] = Medida("BRUTO", valor, texto, ids, derivada=len(partes) > 1)

    deducoes = [por_parcela[p] for p in PARCELAS_DEDUCAO if p in por_parcela and por_parcela[p].valor is not None]
    if "LIQUIDO" not in medidas and "BRUTO" in medidas and deducoes:
        base = medidas["BRUTO"]
        valor = base.valor - sum(abs(d.valor) for d in deducoes)
        texto = f"bruto {reais(base.valor)} − deduções {reais(sum(abs(d.valor) for d in deducoes))}"
        medidas["LIQUIDO"] = Medida("LIQUIDO", valor, texto, base.registros + [d.id for d in deducoes], derivada=True)

    liquido_fundeb = por_parcela.get("LIQUIDO_FUNDEB")
    if liquido_fundeb is not None and liquido_fundeb.estado in ESTADOS_COM_VALOR and "LIQUIDO_FUNDEB" not in medidas:
        medidas["LIQUIDO_FUNDEB"] = Medida("LIQUIDO_FUNDEB", liquido_fundeb.valor or 0, "valor líquido de FUNDEB declarado", [liquido_fundeb.id])
    fundeb = por_parcela.get("FUNDEB")
    if fundeb is not None and fundeb.valor and "LIQUIDO" in medidas and "LIQUIDO_FUNDEB" not in medidas:
        base = medidas["LIQUIDO"]
        medidas["LIQUIDO_FUNDEB"] = Medida(
            "LIQUIDO_FUNDEB", base.valor - abs(fundeb.valor),
            f"líquido {reais(base.valor)} − FUNDEB {reais(abs(fundeb.valor))}", base.registros + [fundeb.id], derivada=True,
        )

    itens = [por_parcela[p] for p in PARCELAS_SOMA if p in por_parcela and por_parcela[p].conceito == "ITEM"]
    composicao = None
    composicao_texto = ""
    composicao_conceito = None
    if itens and any(i.valor for i in itens):
        composicao = sum(i.valor or 0 for i in itens)
        composicao_texto = " + ".join(f"{p.parcela.lower().replace('_', ' ')} {reais(p.valor)}" for p in itens)
        composicao_conceito = "BRUTO"
        if deducoes and total is not None and total.conceito == "LIQUIDO":
            composicao -= sum(abs(d.valor) for d in deducoes)
            composicao_texto += f" − deduções {reais(sum(abs(d.valor) for d in deducoes))}"
            composicao_conceito = "LIQUIDO"
    elif total is not None and total.conceito == "LIQUIDO" and bruto is not None and "BRUTO" in medidas:
        # DCA: total líquido conferido contra bruto + adicional − deduções
        base = medidas["BRUTO"]
        composicao = base.valor - sum(abs(d.valor) for d in deducoes)
        composicao_texto = base.derivacao if base.derivada else f"receita bruta {reais(base.valor)}"
        if deducoes:
            composicao_texto += f" − deduções {reais(sum(abs(d.valor) for d in deducoes))}"
        composicao_conceito = "LIQUIDO"
    return FonteCaso(declaracao, registros, total, medidas, composicao, composicao_texto, composicao_conceito)


def valor_exibicao(fonte: FonteCaso) -> tuple[int | None, str]:
    """Valor original exibido para a fonte (total declarado) e seu estado."""
    if fonte.total is not None:
        return fonte.total.valor, fonte.total.estado
    if "BRUTO" in fonte.medidas:
        return fonte.medidas["BRUTO"].valor, "VALOR"
    return None, "AUSENTE"


def comparar(referencia: FonteCaso | None, fonte: FonteCaso, conceito_ref: str) -> Comparacao:
    nome = sigla(fonte.declaracao)
    if referencia is None or not referencia.medidas:
        return Comparacao(fonte.declaracao, "", "SEM_DADO", motivo="Sem valor de referência para comparar.")
    if not fonte.medidas:
        return Comparacao(fonte.declaracao, referencia.declaracao, "SEM_DADO", motivo=f"{nome} não informa valor neste exercício.")
    ref_nome = sigla(referencia.declaracao)
    ordem = [conceito_ref] + [c for c in ("LIQUIDO", "BRUTO", "LIQUIDO_FUNDEB") if c != conceito_ref]
    for conceito in ordem:
        if conceito in referencia.medidas and conceito in fonte.medidas:
            parcela = ROTULO_PARCELA_COMPARADA.get(conceito, conceito)
            if conceito != conceito_ref:
                parcela += f" (o conceito de referência é {ROTULO_CONCEITO.get(conceito_ref, conceito_ref)}; a referência foi levada ao mesmo conceito da fonte)"
            comparacao = _montar(fonte, referencia, conceito, "SIM", parcela, "")
            return _inferir_fundeb(comparacao, fonte, referencia, conceito) or comparacao
    if "INDEFINIDO" in fonte.medidas:
        alvo = conceito_ref if conceito_ref in referencia.medidas else next(iter(referencia.medidas))
        motivo = f"{nome} não informa se o valor é bruto ou líquido; comparado com o valor {ROTULO_CONCEITO.get(alvo, alvo)} da {ref_nome}."
        comparacao = _montar(fonte, referencia, alvo, "RESSALVA", "conceito não informado pela fonte", motivo, conceito_fonte="INDEFINIDO")
        return _inferir_fundeb(comparacao, fonte, referencia, "INDEFINIDO") or comparacao
    if "INDEFINIDO" in referencia.medidas:
        alvo = conceito_ref if conceito_ref in fonte.medidas else next(iter(fonte.medidas))
        motivo = f"A {ref_nome} não informa o conceito do valor; comparado com o valor {ROTULO_CONCEITO.get(alvo, alvo)} de {nome}."
        return _montar(fonte, referencia, alvo, "RESSALVA", "conceito da referência não informado", motivo, conceito_ref_="INDEFINIDO")
    conceitos_fonte = " e ".join(ROTULO_CONCEITO.get(c, c) for c in fonte.medidas)
    conceitos_ref = " e ".join(ROTULO_CONCEITO.get(c, c) for c in referencia.medidas)
    motivo = (
        f"{nome} informa valor {conceitos_fonte} e a {ref_nome} está disponível como {conceitos_ref}. "
        "Não há deduções identificadas que permitam levar os dois ao mesmo conceito."
    )
    comparacao = Comparacao(fonte.declaracao, referencia.declaracao, "NAO", motivo=motivo)
    comparacao.valor_fonte = next(iter(fonte.medidas.values())).valor
    comparacao.valor_ref = next(iter(referencia.medidas.values())).valor
    return comparacao


def _inferir_fundeb(comparacao: Comparacao, fonte: FonteCaso, referencia: FonteCaso, conceito_fonte: str) -> Comparacao | None:
    """Quando a fonte diverge da referência mas coincide com a referência deduzida do FUNDEB.

    É o caso de bases que publicam a cota-parte líquida de FUNDEB embora a classifiquem
    como bruta (ex.: TCE-SP). A comparação passa a ser feita no conceito líquido de
    FUNDEB, com ressalva, e a inferência fica registrada.
    """
    if comparacao.conceito == "LIQUIDO_FUNDEB" or "LIQUIDO_FUNDEB" not in referencia.medidas:
        return None
    if comparacao.absoluta is None or comparacao.absoluta <= max(100, abs(comparacao.valor_ref or 0) // 1000):
        return None
    alvo = referencia.medidas["LIQUIDO_FUNDEB"]
    valor = comparacao.valor_fonte or 0
    if abs(valor - alvo.valor) > max(100, abs(alvo.valor) // 1000):
        return None
    nome, ref_nome = sigla(fonte.declaracao), sigla(referencia.declaracao)
    motivo = (
        f"{nome} informa valor classificado como {ROTULO_CONCEITO.get(conceito_fonte, conceito_fonte)}, mas ele coincide com a "
        f"{ref_nome} deduzida do FUNDEB ({alvo.derivacao}). Tratado como valor líquido de FUNDEB: diferença de conceito, não de valor."
    )
    nova = _montar(fonte, referencia, "LIQUIDO_FUNDEB", "RESSALVA", ROTULO_PARCELA_COMPARADA["LIQUIDO_FUNDEB"], motivo,
                   conceito_fonte=conceito_fonte if conceito_fonte in fonte.medidas else comparacao.conceito)
    nova.inferencia = "FUNDEB"
    return nova


def _montar(fonte, referencia, conceito, situacao, parcela, motivo, conceito_fonte=None, conceito_ref_=None):
    medida_fonte = fonte.medidas[conceito_fonte or conceito]
    medida_ref = referencia.medidas[conceito_ref_ or conceito]
    dif = diferenca(medida_fonte.valor, medida_ref.valor)
    return Comparacao(
        declaracao=fonte.declaracao,
        referencia=referencia.declaracao,
        situacao=situacao,
        conceito=conceito,
        valor_ref=medida_ref.valor,
        valor_fonte=medida_fonte.valor,
        derivacao_ref=medida_ref.derivacao,
        derivacao_fonte=medida_fonte.derivacao,
        motivo=motivo,
        parcela=parcela,
        assinada=dif["assinada"],
        absoluta=dif["absoluta"],
        percentual=dif["percentual"],
    )


def valor_serie(fonte: FonteCaso, conceito_ref: str) -> int | None:
    """Valor usado nas regras de série temporal: o conceito de referência, se houver."""
    for conceito in (conceito_ref, "LIQUIDO", "BRUTO", "INDEFINIDO"):
        if conceito in fonte.medidas:
            return fonte.medidas[conceito].valor
    return None
