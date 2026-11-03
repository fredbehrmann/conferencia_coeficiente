"""Gráfico de linha em SVG (sem bibliotecas externas) para a série de um caso."""

from __future__ import annotations

from html import escape

from validacao.conferencia.comparacao import sigla
from validacao.conferencia.dinheiro import reais, reais_curto

# Ordem categórica fixa por declaração (paleta de referência validada; nunca reciclada).
CORES = {
    "SICONFI_DCA": "var(--serie-1)",
    "SICONFI_RREO": "var(--serie-2)",
    "SICONFI_MSC": "var(--serie-3)",
    "SIOPE": "var(--serie-4)",
    "SIOPS": "var(--serie-5)",
    "TCE": "var(--serie-6)",
    "BALANCO": "var(--serie-7)",
    "SIMPLES": "var(--serie-8)",
}
TRACOS = {"SICONFI_DCA": "", "SICONFI_RREO": "6 3", "SIOPE": "", "SIOPS": "2 3", "TCE": "8 3 2 3"}


def _escala(valor: float) -> str:
    if valor >= 1e9:
        return f"{valor / 1e9:.1f} bi".replace(".", ",")
    if valor >= 1e6:
        return f"{valor / 1e6:.1f} mi".replace(".", ",")
    if valor >= 1e3:
        return f"{valor / 1e3:.0f} mil"
    return f"{valor:.0f}"


def _passo(maximo: float) -> float:
    bruto = maximo / 4
    if bruto <= 0:
        return 1
    potencia = 10 ** len(str(int(bruto))) / 10
    for multiplo in (1, 2, 2.5, 5, 10):
        if bruto <= multiplo * potencia:
            return multiplo * potencia
    return 10 * potencia


def serie_svg(matriz: dict[str, dict[int, dict]], exercicios: list[int], destaque: int, ordem: list[str]) -> str:
    series = {}
    for declaracao in ordem:
        pontos = {}
        for ano, info in matriz.get(declaracao, {}).items():
            valor = info.get("original")
            if valor is not None and info.get("estado") in ("VALOR", "ZERO"):
                pontos[ano] = valor / 100
        if pontos:
            series[declaracao] = pontos
    if not series:
        return ""
    largura, altura = 640, 220
    esquerda, direita, topo, base = 62, 70, 14, 30
    maximo = max(max(p.values()) for p in series.values())
    minimo = min(0.0, min(min(p.values()) for p in series.values()))
    passo = _passo(maximo - minimo if maximo > minimo else max(abs(maximo), 1))
    teto = passo * (int(maximo / passo) + 1) if maximo > 0 else passo
    chao = minimo if minimo < 0 else 0.0
    def x(ano):
        indice = exercicios.index(ano)
        return esquerda + indice * (largura - esquerda - direita) / max(len(exercicios) - 1, 1)
    def y(valor):
        return topo + (teto - valor) * (altura - topo - base) / (teto - chao)
    partes = [f'<svg class="grafico" viewBox="0 0 {largura} {altura}" role="img" aria-label="Série por fonte, {exercicios[0]} a {exercicios[-1]}">']
    banda = (largura - esquerda - direita) / max(len(exercicios) - 1, 1)
    if destaque in exercicios:
        partes.append(f'<rect class="g-destaque" x="{x(destaque) - banda / 2:.1f}" y="{topo}" width="{banda:.1f}" height="{altura - topo - base}"/>')
    marca = chao
    while marca <= teto + 1e-6:
        partes.append(f'<line class="g-grade" x1="{esquerda}" x2="{largura - direita}" y1="{y(marca):.1f}" y2="{y(marca):.1f}"/>')
        partes.append(f'<text class="g-eixo" x="{esquerda - 6}" y="{y(marca) + 4:.1f}" text-anchor="end">{_escala(marca)}</text>')
        marca += passo
    for ano in exercicios:
        partes.append(f'<text class="g-eixo{" g-eixo-forte" if ano == destaque else ""}" x="{x(ano):.1f}" y="{altura - 10}" text-anchor="middle">{ano}</text>')
    rotulos = []
    for declaracao, pontos in series.items():
        cor = CORES.get(declaracao, "var(--serie-8)")
        anos = sorted(pontos)
        caminho = " ".join(f"{'M' if i == 0 else 'L'}{x(a):.1f},{y(pontos[a]):.1f}" for i, a in enumerate(anos))
        traco = TRACOS.get(declaracao, "")
        partes.append(f'<path class="g-linha" d="{caminho}" stroke="{cor}"' + (f' stroke-dasharray="{traco}"' if traco else "") + "/>")
        for a in anos:
            texto = escape(f"{sigla(declaracao)} {a}: {reais(int(round(pontos[a] * 100)))}")
            partes.append(f'<circle class="g-ponto" cx="{x(a):.1f}" cy="{y(pontos[a]):.1f}" r="4" fill="{cor}"><title>{texto}</title></circle>')
        ultimo = anos[-1]
        rotulos.append([y(pontos[ultimo]), x(ultimo), sigla(declaracao)])
    rotulos.sort()
    for i in range(1, len(rotulos)):
        if rotulos[i][0] - rotulos[i - 1][0] < 12:
            rotulos[i][0] = rotulos[i - 1][0] + 12
    for posicao_y, posicao_x, nome in rotulos:
        partes.append(f'<text class="g-rotulo" x="{posicao_x + 8:.1f}" y="{posicao_y + 4:.1f}">{escape(nome)}</text>')
    partes.append("</svg>")
    return "".join(partes)


def mini_serie(valores: dict[int, int | None], exercicios: list[int], cor: str = "var(--serie-1)",
               rotulo: str = "") -> str:
    """Linha pequena (small multiple) com o total por exercício, em bilhões ou milhões."""
    pontos = {ano: v / 100 for ano, v in valores.items() if v}
    if not pontos:
        return '<p class="sutil">Sem valores.</p>'
    largura, altura, esquerda, direita, topo, base = 300, 110, 18, 18, 18, 22
    maximo = max(pontos.values())
    def x(ano):
        return esquerda + exercicios.index(ano) * (largura - esquerda - direita) / max(len(exercicios) - 1, 1)
    def y(valor):
        return topo + (maximo - valor) * (altura - topo - base) / maximo if maximo else altura - base
    anos = [a for a in exercicios if a in pontos]
    caminho = " ".join(f"{'M' if i == 0 else 'L'}{x(a):.1f},{y(pontos[a]):.1f}" for i, a in enumerate(anos))
    partes = [f'<svg class="grafico mini" viewBox="0 0 {largura} {altura}" role="img" aria-label="{escape(rotulo)}: total por exercício">']
    partes.append(f'<line class="g-grade" x1="{esquerda}" x2="{largura - direita}" y1="{altura - base}" y2="{altura - base}"/>')
    partes.append(f'<path class="g-linha" d="{caminho}" stroke="{cor}"/>')
    for a in anos:
        partes.append(f'<circle class="g-ponto" cx="{x(a):.1f}" cy="{y(pontos[a]):.1f}" r="3.5" fill="{cor}"><title>{a}: {escape(reais_curto(int(pontos[a] * 100)))}</title></circle>')
    for a in (anos[0], anos[-1]):
        ancora = "start" if a == anos[0] else "end"
        partes.append(f'<text class="g-rotulo" x="{x(a):.1f}" y="{y(pontos[a]) - 7:.1f}" text-anchor="{ancora}">{_escala(pontos[a])}</text>')
    for a in exercicios:
        if a in (exercicios[0], exercicios[-1]) or a % 2 == 1:
            partes.append(f'<text class="g-eixo" x="{x(a):.1f}" y="{altura - 6}" text-anchor="middle">{a}</text>')
    partes.append("</svg>")
    return "".join(partes)


def legenda(ordem: list[str], matriz) -> list[tuple[str, str, str]]:
    return [(sigla(d), CORES.get(d, "var(--serie-8)"), TRACOS.get(d, "")) for d in ordem if d in matriz]


__all__ = ["serie_svg", "legenda", "CORES", "reais_curto"]
