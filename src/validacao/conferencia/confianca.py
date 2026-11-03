"""Nível de confiança do valor principal de cada caso (escala 0 a 5).

A pergunta respondida é: o valor principal desta linha é confiável para entrar na apuração?
Fontes independentes: SIOPE, SIOPS e TCE. RREO e MSC são do mesmo declarante da DCA (SICONFI):
reforçam, mas não bastam sozinhos. "Coincide" = dentro da tolerância da regra DAX (padrão:
1% e R$ 1.000 — a divergência exige exceder os dois).

  5 Muito alta     DCA = 2+ independentes; nenhuma independente diverge; sem REP/REE/ESC na DCA
  4 Alta           DCA = 2+ independentes, mas uma terceira diverge (ou o 5 com REP/REE/ESC na DCA)
  3 Moderada       DCA = 1 independente e nenhuma diverge; ou sem DCA e RREO confirmado por independente;
                   ou cota-parte com independentes = DCA ÷ 0,8 (DCA líquida de FUNDEB — depende de P12)
  2 Baixa          DCA = 1 independente e outra diverge; DCA só = RREO/MSC; só a DCA; sem DCA e RREO sem
                   confirmação; sem DCA e RREO, mas 2+ independentes concordam entre si
  1 Muito baixa    DCA diverge de todas, mas as independentes concordam entre si (alternativa consistente)
  0 Sem confiança  DCA diverge de todas e as demais divergem entre si; ou sem DCA e sem consenso
  None             Não avaliado (exercício não exigível ou fora do universo)

O ICMS estadual fica limitado ao nível 4 enquanto o conceito do SIOPE e do SIOPS dos estados
não for confirmado. A decisão do analista não altera o nível: aparece como selo à parte.
"""

from __future__ import annotations

from validacao.conferencia.comparacao import sigla
from validacao.conferencia.dinheiro import percentual, reais_curto

NIVEIS = {
    5: ("Muito alta", "c5"),
    4: ("Alta", "c4"),
    3: ("Moderada", "c3"),
    2: ("Baixa", "c2"),
    1: ("Muito baixa", "c1"),
    0: ("Sem confiança", "c0"),
}
NAO_AVALIADO = ("Não avaliado", "cna")
INDEPENDENTES = ("SIOPE", "SIOPS", "TCE")
MESMO_DECLARANTE = ("SICONFI_RREO", "SICONFI_MSC")
TETO_COMPONENTE = {"ICMS_UF": 4}
FATOR_FUNDEB = 0.8
MARGEM_FUNDEB = 0.005
REGRAS_SOBRE_DCA = {"REP": "repetição entre exercícios", "REE": "repetição entre entes", "ESC": "possível erro de escala"}


def tolerancia_da_configuracao(configuracao: dict) -> dict:
    params = (configuracao.get("regras", {}).get("DAX", {}) or {}).get("parametros", {})
    return {
        "absoluta": int(round(float(params.get("tolerancia_absoluta", 1000.0)) * 100)),
        "relativa": float(params.get("tolerancia_relativa", 0.01)),
        "modo": params.get("modo", "AMBAS"),
    }


TOLERANCIA_PADRAO = {"absoluta": 100_000, "relativa": 0.01, "modo": "AMBAS"}


def _excede(absoluta: int, pct: float | None, tol: dict) -> bool:
    passa_abs = absoluta > tol["absoluta"]
    if pct is None:
        return passa_abs
    passa_rel = abs(pct) > tol["relativa"]
    return (passa_abs and passa_rel) if tol.get("modo", "AMBAS") == "AMBAS" else (passa_abs or passa_rel)


def _perto(x: int, y: int, tol: dict) -> bool:
    base = max(abs(x), abs(y))
    pct = None if base == 0 else abs(x - y) / base
    return not _excede(abs(x - y), pct, tol)


def _lista(nomes: list[str]) -> str:
    nomes = [sigla(n) for n in nomes]
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]


def _consenso(valores: list[tuple[str, int]], tol: dict) -> list[tuple[str, int]]:
    """Maior grupo de 2+ fontes que concordam entre si (ou lista vazia)."""
    melhor: list = []
    for i, (_d, x) in enumerate(valores):
        grupo = [(d, y) for d, y in valores if _perto(x, y, tol)]
        if len(grupo) >= 2 and len(grupo) > len(melhor):
            melhor = grupo
    return melhor


def classificar(componente: str, principal: str | None, valores: dict, situacao_exercicio: str,
                diagnostico: str, tol: dict | None = None) -> tuple[int | None, str]:
    """Devolve (nível, motivo). `valores` com as chaves longas (expandir_valores)."""
    tol = tol or TOLERANCIA_PADRAO
    if diagnostico == "EXCLUIDO":
        return None, "Fora do universo."
    if situacao_exercicio == "NAO_EXIGIVEL":
        return None, "Exercício não exigível na data de corte."

    def tem(d):
        item = valores.get(d)
        return bool(item) and item.get("estado") in ("VALOR", "ZERO") and item.get("original") is not None

    def comparavel(d):
        item = valores.get(d) or {}
        return tem(d) and item.get("comparabilidade") in ("SIM", "RESSALVA") and item.get("assinada") is not None

    def concorda(d):
        item = valores[d]
        return not _excede(abs(item["assinada"]), item.get("percentual"), tol)

    def descreve_div(d):
        item = valores[d]
        pct = item.get("percentual")
        return f"{sigla(d)} {percentual(pct, 1, sinal=True) if pct is not None else reais_curto(item['assinada'])}"

    nivel, motivo = _classificar(componente, principal, valores, tem, comparavel, concorda, descreve_div, tol)
    teto = TETO_COMPONENTE.get(componente)
    if nivel is not None and teto is not None and nivel > teto:
        nivel = teto
        motivo += " Limitado a 'Alta': conceito do SIOPE/SIOPS dos estados ainda não confirmado."
    return nivel, motivo


def _classificar(componente, principal, valores, tem, comparavel, concorda, descreve_div, tol):
    indep_vals = [(d, valores[d]["original"]) for d in INDEPENDENTES if tem(d)]

    if principal is None:
        grupo = _consenso(indep_vals, tol)
        if grupo:
            return 2, (f"Sem DCA e sem RREO. {_lista([d for d, _ in grupo])} concordam entre si "
                       f"({reais_curto(grupo[0][1])}): há valor alternativo consistente, a confirmar.")
        if indep_vals:
            return 0, "Sem DCA e sem RREO; as fontes disponíveis não concordam entre si."
        return 0, "Nenhuma fonte informa valor."

    ref = valores.get(principal, {})
    valor_ref = ref.get("normalizado") if ref.get("normalizado") is not None else ref.get("original")
    nome_ref = sigla(principal)
    concordam = [d for d in INDEPENDENTES if comparavel(d) and concorda(d)]
    divergem = [d for d in INDEPENDENTES if comparavel(d) and not concorda(d)]
    siconfi_ok = [d for d in MESMO_DECLARANTE if d != principal and comparavel(d) and concorda(d)]

    if principal != "SICONFI_DCA":
        # substituta (normalmente o RREO quando falta a DCA)
        if concordam:
            return 3, f"Sem DCA. {nome_ref} confirmado por {_lista(concordam)}."
        if divergem:
            return 2, f"Sem DCA. {nome_ref} sem confirmação: {', '.join(descreve_div(d) for d in divergem)}."
        return 2, f"Sem DCA. {nome_ref} sem fonte independente para confirmar."

    zero = ref.get("estado") == "ZERO"
    regras_dca = [REGRAS_SOBRE_DCA[r] for r in (ref.get("alertas") or []) if r in REGRAS_SOBRE_DCA]

    if len(concordam) >= 2:
        if divergem:
            return 4, f"DCA = {_lista(concordam)}; diverge: {', '.join(descreve_div(d) for d in divergem)}."
        if regras_dca:
            return 4, f"DCA = {_lista(concordam)}, mas com alerta sobre a própria DCA ({', '.join(regras_dca)})."
        extra = f"; também {_lista(siconfi_ok)}" if siconfi_ok else ""
        return 5, f"DCA = {_lista(concordam)}{extra}."

    if componente == "COTA_ICMS_MUN" and valor_ref and valor_ref > 0:
        fundeb = [d for d, v in indep_vals if v and abs(v * FATOR_FUNDEB / valor_ref - 1) <= MARGEM_FUNDEB]
        if len(fundeb) >= 2:
            return 3, (f"{_lista(fundeb)} = DCA ÷ 0,8: a DCA está líquida do FUNDEB (20%). Diferença de conceito, "
                       f"não de valor; o valor a usar depende da definição P12.")

    if len(concordam) == 1:
        if divergem:
            return 2, f"DCA = {sigla(concordam[0])}; diverge: {', '.join(descreve_div(d) for d in divergem)}."
        extra = f" e {_lista(siconfi_ok)}" if siconfi_ok else ""
        return 3, f"DCA = {sigla(concordam[0])}{extra}; nenhuma outra independente para comparar."

    comparaveis = [d for d in INDEPENDENTES + MESMO_DECLARANTE if d != principal and comparavel(d)]
    if not comparaveis:
        return 2, "Só a DCA: nenhuma outra fonte comparável."
    if siconfi_ok:
        texto = f"DCA só = {_lista(siconfi_ok)} (mesmo declarante)"
        if divergem:
            texto += f"; independentes divergem: {', '.join(descreve_div(d) for d in divergem)}"
        return 2, texto + "."
    if not divergem:
        sic_div = [d for d in MESMO_DECLARANTE if d != principal and comparavel(d)]
        return 2, (f"Sem fonte independente comparável; {', '.join(descreve_div(d) for d in sic_div)} "
                   "(mesmo declarante).")
    grupo = _consenso([(d, valores[d]["original"]) for d in divergem], tol)
    inicio = "DCA declara zero" if zero else "DCA diverge de todas as fontes"
    if grupo:
        return 1, (f"{inicio}; {_lista([d for d, _ in grupo])} concordam entre si em {reais_curto(grupo[0][1])}. "
                   "Há valor alternativo consistente.")
    return 0, f"{inicio}, e as demais não concordam entre si: {', '.join(descreve_div(d) for d in divergem) or '—'}."
