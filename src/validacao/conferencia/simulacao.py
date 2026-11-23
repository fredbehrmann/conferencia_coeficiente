"""Simulação comparativa das metodologias de tratamento dos valores.

O módulo é deliberadamente somente leitura: usa a última execução concluída e não
grava decisões, valores calculados ou parâmetros.  Os resultados servem para
comparar três hipóteses metodológicas antes de qualquer aprovação.
"""

from __future__ import annotations

import csv
import io
from copy import copy
import itertools
import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.conferencia import funil
from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import ROTULO_CURTO, SIGLAS
from validacao.conferencia.execucao import expandir_valores, ultima_execucao
from validacao.db import sem_acento
from validacao.modelos import Ente


FONTES_DEGRAUS = ("SICONFI_DCA", "SICONFI_RREO", "SIOPE", "SIOPS")
# Sefaz e BB (cota-parte) e Confaz (ICMS estadual) entram como fontes externas. A 2ª extração das Sefaz
# (SEFAZ_2) fica de fora: não é independente da 1ª, e a metodologia usa só a 1ª.
INDEPENDENTES = ("SIOPE", "SIOPS", "TCE", "BB", "SEFAZ", "CONFAZ")
MESMO_DECLARANTE = ("SICONFI_RREO", "SICONFI_MSC")
ORDEM_FONTE = {fonte: indice for indice, fonte in enumerate(
    ("TCE", "BB", "SEFAZ", "CONFAZ", "SIOPE", "SIOPS", "SICONFI_RREO", "SICONFI_MSC", "SICONFI_DCA")
)}
GRUPO_FONTE = {
    "SICONFI_DCA": "SICONFI", "SICONFI_RREO": "SICONFI", "SICONFI_MSC": "SICONFI",
    "SIOPE": "SIOPE", "SIOPS": "SIOPS", "TCE": "TERCEIRO",
    "BALANCO": "ENTE", "SIMPLES": "TERCEIRO", "SEFAZ": "ESTADO", "BB": "BANCO", "CONFAZ": "CONFAZ",
}

# Parâmetros explicitamente exibidos na tela.  Alterá-los muda a hipótese,
# portanto não ficam escondidos em consultas ou no template.
TOL_DEGRAUS_REL = 0.005
TOL_DEGRAUS_ABS = 500  # R$ 5,00, em centavos
TOL_EVIDENCIA_REL = 0.01
TOL_EVIDENCIA_ABS = 100_000  # R$ 1.000,00, em centavos (usada só pela pré-classificação da validação)
FATOR_FUNDEB = Decimal("0.80")
PERCENTIL_DESTAQUE_IMPACTO = 0.99
LIMITE_DESTAQUE_IMPACTO_ABS = 5_000_000_000  # R$ 50 milhões, em centavos
LIMITE_DESTAQUE_PARTICIPACAO = 0.0005  # 0,05% do componente no exercício
POLITICA_EVIDENCIA_VERSAO = "M2.4-2026-11-19"

# ---- Modelo 2 (M2.3): parâmetros explícitos. Alterá-los muda a hipótese.
# 1 · Pesos de independência. SIOPE e SIOPS são declarados pelo próprio ente: juntos valem 1,25, não 2.
# Sefaz e BB informam o repasse ao Município e não dependem dele (peso de fonte externa). O Confaz é
# informado pelo próprio Estado em outro sistema: vale como o RREO.
PESOS_FONTE = {"TCE": 1.0, "SEFAZ": 1.0, "BB": 1.0, "CONFAZ": 0.5, "SICONFI_RREO": 0.5, "SICONFI_MSC": 0.25}
PESO_SIOPE_E_SIOPS = 1.25
PESO_SIOPE_OU_SIOPS = 0.75
LIMIAR_MANTER = 1.5               # soma mínima para manter a DCA automaticamente
LIMIAR_SUBSTITUIR = 2.0           # soma mínima para trocar uma DCA com valor (e maior que a soma a favor dela)
LIMIAR_SUBSTITUIR_AUSENTE = 1.25  # DCA ausente ou zerada: basta SIOPE + SIOPS, ou TCE + RREO
LIMIAR_SUBSTITUIR_ISOLADA = 1.75  # DCA com valor, mas sem nenhuma fonte que a confirme
PESO_MINIMO_MARGEM = 1.0          # abaixo do limiar de manutenção, a DCA ainda prevalece com folga
MARGEM_MANTER = 0.5               # ... se a evidência a favor superar a contrária em pelo menos 0,5
# 9 · Concordância com piso absoluto pequeno; o que não move o coeficiente é tratado como imaterial.
TOL_CONCORDANCIA_REL = 0.01
TOL_CONCORDANCIA_ABS = 10_000     # R$ 100,00
LIMITE_IMATERIAL_ENTE = 0.001     # diferença que altera ≤ 0,1% da receita média do ente
# 2 · Materialidade: decide se a SUBSTITUIÇÃO precisa de visto, nunca se a evidência é suficiente.
LIMITE_MATERIAL_ENTE = 0.05       # altera ≥ 5% da receita média do ente
LIMITE_MATERIAL_NACIONAL = 0.00001  # altera ≥ 0,001 p.p. do total nacional
FAIXA_CONCEITO = 0.10             # até 10% sem evidência forte contra: prevalece a DCA (presumido conceito)
# 4 · Teste temporal
FATOR_SALTO = 3
SALTO_MINIMO_ABS = 100_000_000    # R$ 1 milhão
# 5 · Cópia entre entes
COPIA_VALOR_MINIMO = 1_000_000    # R$ 10 mil (mesmo piso do alerta de repetição entre entes)
# 7 · Decomposição por parcelas
TOL_PARCELA_REL = 0.002
TOL_PARCELA_ABS = 10_000
PARCELA_MINIMA_REL = 0.005
FATOR_EXPLICADA = 0.5  # fonte cuja diferença é explicada por parcela confirma a DCA com metade do peso
PARCELAS_DECOMPOSICAO = {"MULTAS_JUROS", "DA_PRINCIPAL", "DA_MULTAS", "FECOP", "OUTROS", "OUTRAS_DEDUCOES",
                         "DEDUCAO_CONST", "FUNDEB", "FECOP_FUNDEB", "FECOP_OUTRAS_DEDUCOES"}
ROTULO_PARCELA = {
    "MULTAS_JUROS": "multas e juros", "DA_PRINCIPAL": "dívida ativa", "DA_MULTAS": "multas da dívida ativa",
    "FECOP": "FECOP", "OUTROS": "outros itens", "OUTRAS_DEDUCOES": "outras deduções",
    "DEDUCAO_CONST": "dedução constitucional", "FUNDEB": "FUNDEB", "FECOP_FUNDEB": "FUNDEB do FECOP",
    "FECOP_OUTRAS_DEDUCOES": "deduções do FECOP",
}
# 10 · Conservação por UF (cota-parte)
PESO_CONSERVACAO = 0.5
CONSERVACAO_MINIMA_REL = 0.005    # a troca precisa mover ao menos 0,5% do repasse da UF
CONSERVACAO_FAIXA_VALIDA = (0.5, 2.0)  # repasse estadual fora desta razão é tratado como não confiável
# P12 (definida): o coeficiente usa a cota-parte BRUTA; a retenção de 20% para o FUNDEB não a reduz.
CONCEITO_COTA = "cota-parte bruta (a retenção do FUNDEB não reduz a cota-parte)"
ALERTAS_IMPEDITIVOS = {"ESC", "REP", "REE", "TOT", "NEG"}
# Com evidência forte (peso ≥ 2), inconsistência da composição não invalida o total confirmado;
# a repetição entre entes passa a ser tratada pela regra de cópia (5).
ALERTAS_TOLERADOS_EVIDENCIA_FORTE = {"TOT", "REE"}


@dataclass
class EntradaCaso:
    id: int
    id_ente: int
    nome_ente: str
    uf: str
    esfera: str
    capital: bool
    exercicio: int
    componente: str
    situacao_exercicio: str
    diagnostico: str
    principal: str | None
    valor_principal: int | None
    estado_principal: str
    confianca: int | None
    confianca_motivo: str
    tipos: set[str]
    maior_dif_abs: int
    valores: dict


@dataclass
class FiltrosSimulador:
    q: str = ""
    uf: str = ""
    componente: str = ""
    status: str = ""
    diferenca: str = "modelos"
    ordem: str = "impacto"
    pagina: int = 1

    @classmethod
    def de_query(cls, params) -> "FiltrosSimulador":
        def texto(nome: str, padrao: str = "") -> str:
            # String vazia é um valor válido em ``diferenca`` e significa
            # "Toda a base". Só use o padrão quando o parâmetro estiver ausente.
            valor = params.get(nome)
            return str(padrao if valor is None else valor).strip()

        filtros = cls(
            q=texto("q"), uf=texto("uf").upper(), componente=texto("componente"),
            status=texto("status"), diferenca=texto("diferenca", "modelos"),
            ordem=texto("ordem", "impacto"),
        )
        if filtros.diferenca not in ("", "modelos", "ajuste"):
            filtros.diferenca = "modelos"
        if filtros.ordem not in ("impacto", "diferenca", "ente"):
            filtros.ordem = "impacto"
        try:
            filtros.pagina = max(1, int(params.get("pagina") or 1))
        except (TypeError, ValueError):
            filtros.pagina = 1
        return filtros

    def query(self, **troca) -> str:
        dados = {
            "q": self.q, "uf": self.uf, "componente": self.componente, "status": self.status,
            "diferenca": self.diferenca, "ordem": self.ordem, "pagina": self.pagina,
        }
        dados.update(troca)
        return urlencode({k: v for k, v in dados.items() if v not in ("", None)})


def _presente(item: dict | None) -> bool:
    return bool(item) and item.get("estado") in ("VALOR", "ZERO") and item.get("original") is not None


def _valor(item: dict | None, normalizado: bool = True) -> int | None:
    if not _presente(item):
        return None
    if normalizado and item.get("normalizado") is not None and item.get("comparabilidade") in (
        "SIM", "RESSALVA", "REFERENCIA"
    ):
        return int(item["normalizado"])
    return int(item["original"])


def _pct_diferenca(a: int, b: int) -> float | None:
    base = max(abs(a), abs(b))
    return None if base == 0 else abs(a - b) / base


def _perto(a: int, b: int, relativa: float, absoluta: int) -> bool:
    diferenca = abs(a - b)
    pct = _pct_diferenca(a, b)
    # Degraus: tolerância relativa com piso em reais. Evidência: a mesma
    # convenção conservadora já usada pela aplicação (ambos precisam exceder).
    return diferenca <= absoluta or (pct is not None and pct <= relativa)


def _grupos_concordantes(valores: list[tuple[str, int]], relativa: float, absoluta: int) -> list[tuple[str, int]]:
    """Maior grupo concordante; empates usam a precedência pública de fontes."""
    candidatos: list[list[tuple[str, int]]] = []
    for _fonte, ancora in valores:
        grupo = [(fonte, valor) for fonte, valor in valores if _perto(ancora, valor, relativa, absoluta)]
        if len(grupo) >= 2:
            candidatos.append(grupo)
    if not candidatos:
        return []
    candidatos.sort(key=lambda grupo: (
        -len({GRUPO_FONTE.get(f, f) for f, _ in grupo}), -len(grupo),
        min(ORDEM_FONTE.get(f, 99) for f, _ in grupo),
    ))
    return candidatos[0]


def _mediana_centavos(valores: list[int]) -> int:
    ordenados = sorted(valores)
    meio = len(ordenados) // 2
    valor = Decimal(ordenados[meio]) if len(ordenados) % 2 else (
        Decimal(ordenados[meio - 1]) + Decimal(ordenados[meio])
    ) / 2
    return int(valor.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _medoide(grupo: list[tuple[str, int]]) -> tuple[str, int]:
    """Observação real mais central; nunca cria um valor inexistente."""
    return min(
        grupo,
        key=lambda item: (sum(abs(item[1] - outro[1]) for outro in grupo), ORDEM_FONTE.get(item[0], 99)),
    )


def _registro_do_consenso(grupo: list[tuple[str, int]]) -> tuple[str, int, str]:
    """Seleciona um registro real; empate segue a precedência pública das fontes."""
    fonte, valor = _medoide(grupo)
    formula = (
        "registro real mais central; empate pela precedência "
        "TCE > BB > Sefaz > Confaz > SIOPE > SIOPS; nunca média sintética"
    )
    return fonte, valor, formula


def _fontes_texto(fontes: list[str] | tuple[str, ...]) -> str:
    return " + ".join(SIGLAS.get(fonte, fonte) for fonte in fontes)


def _resultado(valor_original: int | None, valor: int | None, fonte: str, regra: str, status: str,
               justificativa: str, formula: str = "", aplicavel: bool = True) -> dict:
    valor_cenario = valor if valor is not None else (valor_original or 0)
    resultado = {
        "valor_original": valor_original,
        "valor_proposto": valor,
        "valor_cenario": valor_cenario,
        "fonte": fonte,
        "formula": formula,
        "regra": regra,
        "status": status,
        "justificativa": justificativa,
        "aplicavel": aplicavel,
        "diferenca": valor_cenario - (valor_original or 0),
    }
    _atualizar_cenario_aprovado(resultado)
    return resultado


def _atualizar_cenario_aprovado(resultado: dict) -> None:
    original = resultado.get("valor_original") or 0
    aprovado = resultado.get("valor_cenario", original) if resultado.get("status") in ("AUTOMATICA", "REFERENCIA") else original
    resultado["valor_aprovado"] = aprovado
    resultado["diferenca_aprovada"] = aprovado - original


def _tornar_pendente(resultado: dict, sufixo: str, motivo: str) -> None:
    if resultado["status"] != "AUTOMATICA":
        return
    resultado["status"] = "PENDENTE"
    if sufixo and sufixo not in resultado["regra"].split("+"):
        resultado["regra"] += f"+{sufixo}"
    resultado["justificativa"] += " " + motivo
    _atualizar_cenario_aprovado(resultado)


def _fora_universo(caso: EntradaCaso) -> str:
    if caso.diagnostico == "EXCLUIDO":
        return "Exclusão registrada na execução de validação."
    if caso.situacao_exercicio != "EXIGIVEL":
        return "Exercício ainda não exigível na data de corte."
    if caso.id_ente == 5300108 and caso.componente in ("ISS_MUN", "COTA_ICMS_MUN"):
        return "Brasília não integra o universo municipal; o Distrito Federal possui componentes próprios."
    if caso.id_ente == 5101837 and caso.exercicio < 2025:
        return "Boa Esperança do Norte não estava instalado neste exercício."
    return ""


def _valores_degraus(caso: EntradaCaso) -> tuple[dict[str, int], list[str]]:
    valores: dict[str, int] = {}
    notas: list[str] = []
    for fonte in FONTES_DEGRAUS:
        if fonte == "SIOPS" and caso.exercicio == 2019:
            continue
        item = caso.valores.get(fonte)
        valor = _valor(item)
        if valor is None:
            continue
        if caso.componente == "COTA_ICMS_MUN" and (
            item.get("conceito") == "LIQUIDO_FUNDEB" or "FLF" in set(item.get("alertas") or [])
        ):
            valor = int((Decimal(valor) / FATOR_FUNDEB).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            notas.append(f"{SIGLAS.get(fonte, fonte)} reconvertida de líquida para bruta (÷ 0,80)")
        valores[fonte] = valor

    # A classificação atual identifica também o padrão inverso: DCA = 80% de
    # duas fontes independentes. A hipótese dos degraus trata a cota como bruta.
    dca = valores.get("SICONFI_DCA")
    if caso.componente == "COTA_ICMS_MUN" and dca and padrao_p12(caso) is not None:
        valores["SICONFI_DCA"] = int(
            (Decimal(dca) / FATOR_FUNDEB).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        notas.append("DCA declarada líquida do FUNDEB reconvertida para bruta (÷ 0,80)")
    return valores, notas


def aplicar_degraus(caso: EntradaCaso, estimativa: tuple[int | None, str] = (None, "")) -> dict:
    original = caso.valor_principal
    fora = _fora_universo(caso)
    if fora:
        return _resultado(original, original, "—", "UNIVERSO", "EXCLUIDO",
                          fora, aplicavel=False)

    valores, notas = _valores_degraus(caso)
    dca = valores.get("SICONFI_DCA")
    alternativas = [(f, valores[f]) for f in FONTES_DEGRAUS[1:] if f in valores]
    nota = (" " + "; ".join(notas) + ".") if notas else ""

    if dca is not None and dca != 0:
        confirmam = [(f, v) for f, v in alternativas if _perto(dca, v, TOL_DEGRAUS_REL, TOL_DEGRAUS_ABS)]
        if confirmam:
            fontes = [f for f, _ in confirmam]
            return _resultado(
                original, dca, "DCA", "D1A", "AUTOMATICA",
                f"DCA mantida: concorda com {_fontes_texto(fontes)} dentro de 0,5% (piso R$ 5).{nota}",
                "DCA após as normalizações do modelo" if notas else "",
            )
        grupo = _grupos_concordantes(alternativas, TOL_DEGRAUS_REL, TOL_DEGRAUS_ABS)
        if grupo:
            fontes = [f for f, _ in grupo]
            proposto = _mediana_centavos([v for _, v in grupo])
            return _resultado(
                original, proposto, f"Mediana: {_fontes_texto(fontes)}", "D2B", "AUTOMATICA",
                "DCA isolada contra pelo menos duas outras bases concordantes; substituída pela mediana.",
                f"mediana({', '.join(SIGLAS.get(f, f) for f in fontes)}){nota}",
            )
        regra = "D1C" if not alternativas else "D1B"
        motivo = (
            "DCA é a única declaração disponível e permanece por falta de alternativa."
            if not alternativas else
            "DCA diverge, mas as demais bases não formam maioria concordante; a DCA prevalece."
        )
        return _resultado(original, dca, "DCA", regra, "AUTOMATICA", motivo + nota,
                          "DCA após as normalizações do modelo" if notas else "")

    if alternativas:
        fontes = [f for f, _ in alternativas]
        proposto = _mediana_centavos([v for _, v in alternativas])
        estado = "zerada" if dca == 0 else "vazia"
        return _resultado(
            original, proposto, f"Mediana: {_fontes_texto(fontes)}", "D2A", "AUTOMATICA",
            f"DCA {estado}; aplicado o valor mediano das demais fontes disponíveis.",
            f"mediana({', '.join(SIGLAS.get(f, f) for f in fontes)}){nota}",
        )

    valor_estimado, formula = estimativa
    if valor_estimado is not None:
        return _resultado(
            original, valor_estimado, "Estimativa", "D3", "AUTOMATICA",
            "Nenhuma fonte informa valor; aplicado o exercício vizinho corrigido pela variação nacional.", formula,
        )
    return _resultado(
        original, None, "Estimativa não calculável", "D3", "PENDENTE",
        "Nenhuma fonte informa valor e não há exercício vizinho suficiente para a estimativa.",
    )


def _valor_evidencia(caso: EntradaCaso, fonte: str) -> int | None:
    item = caso.valores.get(fonte)
    if not _presente(item):
        return None
    if caso.principal is not None and fonte != caso.principal and item.get("comparabilidade") not in ("SIM", "RESSALVA"):
        return None
    return _valor(item)


def _alertas_da_fonte(caso: EntradaCaso, fonte: str) -> set[str]:
    return set((caso.valores.get(fonte) or {}).get("alertas") or [])


def _criticos(caso: EntradaCaso, fonte: str) -> set[str]:
    # Alertas de MSC/RREO ou de uma fonte descartada não contaminam a fonte
    # efetivamente adotada.
    return _alertas_da_fonte(caso, fonte) & ALERTAS_IMPEDITIVOS


def _criticos_grupo(caso: EntradaCaso, grupo: list[tuple[str, int]]) -> set[str]:
    return set().union(*(_criticos(caso, fonte) for fonte, _ in grupo)) if grupo else set()


def _p(numero: float) -> str:
    """Peso ou limiar com vírgula decimal (exibição)."""
    return f"{numero:.2f}".replace(".", ",")


# ------------------------------------------------------------------ 1 · pesos de independência

def peso_evidencia(fontes) -> float:
    """Soma dos pesos das fontes. SIOPE e SIOPS juntos valem menos que duas fontes externas:
    os dois são declarados pelo próprio ente e costumam vir da mesma contabilidade."""
    fontes = set(fontes)
    peso = sum(PESOS_FONTE.get(fonte, 0.0) for fonte in fontes)
    autodeclaradas = fontes & {"SIOPE", "SIOPS"}
    if len(autodeclaradas) == 2:
        peso += PESO_SIOPE_E_SIOPS
    elif autodeclaradas:
        peso += PESO_SIOPE_OU_SIOPS
    return round(peso, 4)


def _concorda(a: int, b: int) -> bool:
    """Concordância entre duas declarações: 1% ou R$ 100 (item 9: piso absoluto pequeno)."""
    return _perto(a, b, TOL_CONCORDANCIA_REL, TOL_CONCORDANCIA_ABS)


def _melhor_grupo(valores: list[tuple[str, int]]) -> tuple[list[tuple[str, int]], float]:
    """Grupo de fontes concordantes de maior peso; empate pela precedência pública."""
    melhor, melhor_peso = [], 0.0
    for _fonte, ancora in valores:
        grupo = [(f, v) for f, v in valores if _concorda(ancora, v)]
        peso = peso_evidencia(f for f, _ in grupo)
        chave = (peso, len(grupo), -min(ORDEM_FONTE.get(f, 99) for f, _ in grupo))
        if not melhor or chave > (melhor_peso, len(melhor), -min(ORDEM_FONTE.get(f, 99) for f, _ in melhor)):
            melhor, melhor_peso = grupo, peso
    return melhor, melhor_peso


# ------------------------------------------------------------------ contexto da simulação

@dataclass
class ContextoEvidencia:
    """Informações de toda a base usadas caso a caso (série, parcelas, porte, conservação)."""

    parcelas: dict = field(default_factory=dict)          # (ente, ano, comp, decl) -> {parcela: valor}
    serie_dca: dict = field(default_factory=dict)         # (ente, comp) -> {ano: valor}
    receita_media_ente: dict = field(default_factory=dict)  # ente -> receita média (todas as componentes)
    anos_componente: dict = field(default_factory=dict)   # comp -> nº de exercícios exigíveis
    total_nacional: int = 0                               # receita média nacional (original)
    conservacao: dict = field(default_factory=dict)       # (uf, ano) -> {"repasse": int, "soma": int}
    uf_ente: dict = field(default_factory=dict)


CONTEXTO_VAZIO = ContextoEvidencia()


# ------------------------------------------------------------------ 4 · teste temporal

def _teste_temporal(caso: EntradaCaso, valor: int | None, contexto: ContextoEvidencia) -> str:
    """Pico ou vale isolado na série da DCA, que confirmações não anulam."""
    if not valor or valor <= 0:
        return ""
    serie = contexto.serie_dca.get((caso.id_ente, caso.componente)) or {}
    vizinhos = [serie[a] for a in (caso.exercicio - 1, caso.exercicio + 1) if serie.get(a, 0) > 0]
    if not vizinhos:
        return ""
    referencia = _mediana_centavos(vizinhos)
    if valor >= FATOR_SALTO * referencia and valor - referencia >= SALTO_MINIMO_ABS:
        return f"pico isolado: {valor / referencia:.1f}× a mediana dos exercícios vizinhos".replace(".", ",")
    if valor * FATOR_SALTO <= referencia and referencia - valor >= SALTO_MINIMO_ABS:
        return f"queda isolada: {referencia / valor:.1f}× abaixo da mediana dos exercícios vizinhos".replace(".", ",")
    return ""


# ------------------------------------------------------------------ 7 · decomposição por parcelas

def _explicar_por_parcelas(caso: EntradaCaso, dca: int, fonte: str, valor: int, contexto: ContextoEvidencia) -> str:
    """A diferença DCA × fonte coincide com uma ou duas parcelas declaradas? Então é conceito, não erro."""
    diferenca = abs(dca - valor)
    if diferenca == 0:
        return ""
    candidatos = []
    for declaracao in (fonte, "SICONFI_DCA"):
        for parcela, quantia in (contexto.parcelas.get((caso.id_ente, caso.exercicio, caso.componente, declaracao)) or {}).items():
            if quantia:
                candidatos.append((f"{SIGLAS.get(declaracao, declaracao)} {ROTULO_PARCELA.get(parcela, parcela)}", abs(quantia)))
    tolerancia = max(TOL_PARCELA_ABS, int(TOL_PARCELA_REL * max(abs(dca), abs(valor))))
    for tamanho in (1, 2):
        for combinacao in itertools.combinations(candidatos, tamanho):
            soma = sum(q for _, q in combinacao)
            if soma >= PARCELA_MINIMA_REL * max(abs(dca), 1) and abs(diferenca - soma) <= tolerancia:
                return " + ".join(nome for nome, _ in combinacao)
    return ""


# ------------------------------------------------------------------ 8 · cota-parte declarada líquida do FUNDEB

def padrao_p12(caso: EntradaCaso, grupo: list[tuple[str, int]] | None = None) -> int | None:
    """Cota-parte com DCA ≈ 80% do consenso das outras fontes: DCA declarada líquida do FUNDEB.

    Devolve o valor bruto de referência (registro real do consenso) ou None. Não depende do texto
    do motivo da confiança."""
    if caso.componente != "COTA_ICMS_MUN":
        return None
    dca = _valor_evidencia(caso, "SICONFI_DCA")
    if not dca or dca <= 0:
        return None
    if grupo is None:
        outros = [(f, v) for f in INDEPENDENTES + MESMO_DECLARANTE[:1] if (v := _valor_evidencia(caso, f)) is not None]
        if not any(abs(dca - int(Decimal(v) * FATOR_FUNDEB)) <= max(TOL_CONCORDANCIA_ABS, dca // 100) for _, v in outros):
            return None  # atalho: nenhuma fonte está a 80% da DCA
        grupo, peso = _melhor_grupo(outros)
        if peso < LIMIAR_SUBSTITUIR_AUSENTE:
            return None
    if not grupo:
        return None
    _fonte, bruto = _medoide(grupo)
    if bruto > 0 and _perto(dca, int(Decimal(bruto) * FATOR_FUNDEB), 0.01, TOL_CONCORDANCIA_ABS) and not _concorda(dca, bruto):
        return bruto
    return None


def _bruto_fundeb(valor: int) -> int:
    return int((Decimal(valor) / FATOR_FUNDEB).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _reais(centavos: int) -> str:
    return "R$ " + f"{centavos / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def converter_liquidas_fundeb(caso: EntradaCaso, dca: int | None,
                              outras: list[tuple[str, int]]) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """Cota-parte: fontes declaradas líquidas do FUNDEB (= 80% do bruto) voltam ao bruto antes da comparação.

    O coeficiente usa a cota bruta (P12), então uma fonte que informou a cota já deduzida dos 20% do FUNDEB
    não contradiz a DCA bruta: confirma-a, a menos do conceito. Com DCA, a fonte é líquida quando ÷ 0,80
    concorda com a DCA; sem DCA, quando ÷ 0,80 concorda com outra fonte. Devolve as fontes convertidas e
    {fonte: valor líquido declarado}."""
    if caso.componente != "COTA_ICMS_MUN":
        return outras, {}
    liquidas: dict[str, int] = {}
    convertidas = []
    for fonte, valor in outras:
        if valor > 0:
            bruto = _bruto_fundeb(valor)
            if dca and dca > 0:
                liquida = not _concorda(dca, valor) and _concorda(dca, bruto)
            else:
                liquida = any(g != fonte and w > 0 and not _concorda(valor, w) and _concorda(bruto, w) for g, w in outras)
            if liquida:
                liquidas[fonte] = valor
                convertidas.append((fonte, bruto))
                continue
        convertidas.append((fonte, valor))
    return convertidas, liquidas


def _nota_liquidas(liquidas: dict[str, int]) -> str:
    if not liquidas:
        return ""
    partes = [f"{SIGLAS.get(f, f)} informou {_reais(v)}, 80% de {_reais(_bruto_fundeb(v))}" for f, v in liquidas.items()]
    return (" Declarada(s) líquida(s) do FUNDEB, convertida(s) para o bruto antes da comparação: "
            + "; ".join(partes) + ". A retenção do FUNDEB não reduz a cota-parte no coeficiente.")


# ------------------------------------------------------------------ 2 e 9 · materialidade pelo efeito

def _efeito(caso: EntradaCaso, diferenca: int, contexto: ContextoEvidencia) -> tuple[float, float]:
    """Efeito de uma diferença no ano sobre a receita média do ente (fração) e sobre o total nacional (fração)."""
    anos = contexto.anos_componente.get(caso.componente) or 7
    na_media = abs(diferenca) / anos
    receita = contexto.receita_media_ente.get(caso.id_ente) or 0
    sobre_ente = na_media / receita if receita else (1.0 if na_media else 0.0)
    sobre_nacional = na_media / contexto.total_nacional if contexto.total_nacional else 0.0
    return sobre_ente, sobre_nacional


def _imaterial(caso: EntradaCaso, diferenca: int, contexto: ContextoEvidencia) -> bool:
    if not contexto.receita_media_ente:
        return False
    sobre_ente, _ = _efeito(caso, diferenca, contexto)
    return sobre_ente <= LIMITE_IMATERIAL_ENTE


def _motivos_visto(caso: EntradaCaso, diferenca: int, contexto: ContextoEvidencia) -> list[str]:
    """Substituição que precisa de visto humano antes de valer (não bloqueia manutenção da DCA)."""
    motivos = []
    if caso.esfera != "MUNICIPIO":
        motivos.append("Estado/DF")
    if contexto.receita_media_ente:
        sobre_ente, sobre_nacional = _efeito(caso, diferenca, contexto)
        if sobre_ente >= LIMITE_MATERIAL_ENTE:
            motivos.append(f"altera {sobre_ente:.1%} da receita média do ente".replace(".", ","))
        if sobre_nacional >= LIMITE_MATERIAL_NACIONAL:
            motivos.append(f"altera {sobre_nacional * 100:.4f} p.p. do total nacional".replace(".", ",", 1))
    return motivos


# ------------------------------------------------------------------ 10 · conservação por UF

def _conservacao(caso: EntradaCaso, dca: int | None, alternativa: int, contexto: ContextoEvidencia) -> str:
    """A troca aproxima a soma das cotas municipais da UF do repasse declarado pelo Estado?

    Devolve "alternativa", "dca" ou "" (sem efeito mensurável). Só para cota-parte municipal."""
    if caso.componente != "COTA_ICMS_MUN" or dca is None:
        return ""
    dados = contexto.conservacao.get((caso.uf, caso.exercicio))
    if not dados or not dados.get("repasse"):
        return ""
    repasse, soma = dados["repasse"], dados["soma"]
    troca = alternativa - dca
    if abs(troca) < CONSERVACAO_MINIMA_REL * repasse:
        return ""
    lacuna_antes = soma - repasse
    lacuna_depois = lacuna_antes + troca
    if abs(lacuna_depois) <= abs(lacuna_antes) - abs(troca) / 2:
        return "alternativa"
    if abs(lacuna_depois) >= abs(lacuna_antes) + abs(troca) / 2:
        return "dca"
    return ""


# ------------------------------------------------------------------ modelo 2

def _resultado_evidencia(caso, valor, fonte, regra, status, justificativa, *, formula="", peso_dca=0.0,
                         peso_alternativa=0.0, visto=None, grupo=None) -> dict:
    resultado = _resultado(caso.valor_principal, valor, fonte, regra, status, justificativa, formula)
    resultado.update(peso_dca=peso_dca, peso_alternativa=peso_alternativa, motivos_visto=visto or [],
                     fontes_grupo=[f for f, _ in (grupo or [])])
    return resultado


def _substituir(caso, contexto, grupo, peso_dca, peso_alt, regra, texto, dca=None) -> dict:
    # O valor adotado é um registro real do grupo; a MSC só confirma, nunca é adotada.
    fonte, candidato, formula = _registro_do_consenso([g for g in grupo if g[0] != "SICONFI_MSC"] or grupo)
    if any(f == "SICONFI_RREO" for f, _ in grupo) and regra == "E2A":
        fonte, candidato = "SICONFI_RREO", dict(grupo)["SICONFI_RREO"]
        formula = "RREO confirmado por fonte independente"
    criticos = _criticos_grupo(caso, grupo)
    visto = _motivos_visto(caso, candidato - (dca or 0), contexto)
    if criticos:
        status = "PENDENTE"
    elif visto:
        status = "VISTO"
    else:
        status = "AUTOMATICA"
    justificativa = (
        f"{texto} Evidência da alternativa: {_fontes_texto([f for f, _ in grupo])} (peso {_p(peso_alt)})"
        + (f" contra {_p(peso_dca)} a favor da DCA." if dca is not None else ".")
        + (f" Alertas impeditivos: {', '.join(sorted(criticos))}." if criticos else "")
        + (f" Sujeita a visto: {'; '.join(visto)}." if visto and not criticos else "")
    )
    resultado = _resultado_evidencia(caso, candidato, SIGLAS.get(fonte, fonte), regra, status, justificativa,
                                     formula=formula, peso_dca=peso_dca, peso_alternativa=peso_alt, visto=visto,
                                     grupo=grupo)
    if dca is not None:
        resultado["impacto_conflito"] = abs(candidato - dca)
        resultado["divergencia_max_pct"] = _pct_diferenca(dca, candidato) or 0
    return resultado


def aplicar_evidencia(caso: EntradaCaso, contexto: ContextoEvidencia | None = None) -> dict:
    contexto = contexto or CONTEXTO_VAZIO
    original = caso.valor_principal
    fora = _fora_universo(caso)
    if fora:
        resultado = _resultado_evidencia(caso, original, "—", "UNIVERSO", "EXCLUIDO", fora)
        resultado["aplicavel"] = False
        return resultado

    dca = _valor_evidencia(caso, "SICONFI_DCA")
    outras = [(f, v) for f in INDEPENDENTES + MESMO_DECLARANTE if (v := _valor_evidencia(caso, f)) is not None]
    outras, liquidas = converter_liquidas_fundeb(caso, dca, outras)
    nota_fundeb = _nota_liquidas(liquidas)
    estadual = caso.esfera != "MUNICIPIO"

    def anotar(resultado: dict) -> dict:
        resultado["fontes_liquidas_fundeb"] = list(liquidas)
        if nota_fundeb and nota_fundeb not in resultado["justificativa"]:
            resultado["justificativa"] += nota_fundeb
        return resultado

    # ---------------- DCA ausente ou zerada: a alternativa precisa de evidência mínima (sem DCA para pesar contra)
    if dca is None or dca == 0:
        confirmam_zero = [(f, v) for f, v in outras if dca == 0 and v == 0]
        if dca == 0 and confirmam_zero and peso_evidencia(f for f, _ in confirmam_zero) >= LIMIAR_MANTER:
            return anotar(_resultado_evidencia(caso, 0, "DCA", "E1A", "AUTOMATICA", "DCA zerada, confirmada por "
                                        + _fontes_texto([f for f, _ in confirmam_zero]) + ".",
                                        peso_dca=peso_evidencia(f for f, _ in confirmam_zero)))
        nao_zero = [(f, v) for f, v in outras if v != 0]
        grupo, peso = _melhor_grupo(nao_zero)
        estado = "zerada" if dca == 0 else "ausente"
        if grupo and peso >= LIMIAR_SUBSTITUIR_AUSENTE:
            regra = "E2A" if any(f == "SICONFI_RREO" for f, _ in grupo) else "E2B"
            return anotar(_substituir(caso, contexto, grupo, 0.0, peso, regra, f"DCA {estado}.", dca=dca))
        if nao_zero:
            fonte, candidato = sorted(nao_zero, key=lambda item: ORDEM_FONTE.get(item[0], 99))[0]
            return anotar(_resultado_evidencia(
                caso, candidato, SIGLAS.get(fonte, fonte), "E2C", "PENDENTE",
                f"DCA {estado} e evidência insuficiente para escolher a alternativa (peso {_p(peso)}, mínimo "
                f"{_p(LIMIAR_SUBSTITUIR_AUSENTE)}).", peso_alternativa=peso, grupo=grupo))
        return anotar(_resultado_evidencia(caso, None, "Estimativa", "E3", "PENDENTE",
                                    "Nenhuma fonte informa valor; estimativa exige critério aprovado e revisão."))

    # ---------------- DCA com valor: evidência a favor × evidência contra
    confirmam = [(f, v) for f, v in outras if _concorda(dca, v)]
    divergem = [(f, v) for f, v in outras if not _concorda(dca, v)]

    # 8: DCA declarada líquida do FUNDEB (= 80% do bruto das outras fontes). P12 definida: vale a cota
    # bruta, então a DCA é trocada pelo bruto (antes da decomposição: a parcela do FUNDEB "explicaria" a diferença).
    grupo_div, peso_div = _melhor_grupo(divergem)
    bruto = padrao_p12(caso, grupo_div) if grupo_div and peso_div >= LIMIAR_SUBSTITUIR_AUSENTE else None
    if bruto is not None:
        return anotar(_substituir(
            caso, contexto, grupo_div, peso_evidencia(f for f, _ in confirmam), peso_div, "E2E",
            f"DCA declarada líquida do FUNDEB: {_reais(dca)} = 80% do bruto informado pelas demais fontes. "
            f"O coeficiente usa a cota-parte bruta (a retenção do FUNDEB não reduz a cota-parte); adotado o bruto de {_reais(bruto)}.",
            dca=dca))
    explicadas = {}
    for fonte, valor in divergem:  # 7: diferença igual a parcela declarada = conceito, não contradição
        explicacao = _explicar_por_parcelas(caso, dca, fonte, valor, contexto)
        if explicacao:
            explicadas[fonte] = explicacao
    contrarias = [(f, v) for f, v in divergem if f not in explicadas]
    peso_dca = peso_evidencia(f for f, _ in confirmam)
    if explicadas:  # confirma a DCA a menos do conceito: entra com peso reduzido
        com_explicadas = peso_evidencia([f for f, _ in confirmam] + list(explicadas))
        peso_dca = round(peso_dca + FATOR_EXPLICADA * (com_explicadas - peso_dca), 4)
    grupo_alt, peso_alt = _melhor_grupo(contrarias)

    # 10: conservação por UF reforça o lado que aproxima a soma da UF do repasse do Estado
    nota_conservacao = ""
    if grupo_alt:
        lado = _conservacao(caso, dca, _medoide(grupo_alt)[1], contexto)
        if lado == "alternativa":
            peso_alt = round(peso_alt + PESO_CONSERVACAO, 4)
            nota_conservacao = " A troca aproxima a soma das cotas da UF do repasse declarado pelo Estado."
        elif lado == "dca":
            peso_dca = round(peso_dca + PESO_CONSERVACAO, 4)
            nota_conservacao = " Manter a DCA é coerente com o repasse declarado pelo Estado à UF."

    # alertas e testes que confirmações não anulam
    criticos = _criticos(caso, "SICONFI_DCA")
    if peso_dca >= LIMIAR_SUBSTITUIR:
        criticos -= ALERTAS_TOLERADOS_EVIDENCIA_FORTE
    elif "VAR" in _alertas_da_fonte(caso, "SICONFI_DCA"):
        criticos.add("VAR")
    temporal = _teste_temporal(caso, dca, contexto)
    notas = ""
    if explicadas:
        notas += " Diferenças explicadas por parcelas declaradas: " + "; ".join(
            f"{SIGLAS.get(f, f)} = {texto}" for f, texto in explicadas.items()) + "."
    notas += nota_conservacao

    # 2: evidência forte contra a DCA substitui, qualquer que seja o tamanho da diferença
    limiar_troca = LIMIAR_SUBSTITUIR_ISOLADA if peso_dca == 0 else LIMIAR_SUBSTITUIR
    if grupo_alt and peso_alt >= limiar_troca and peso_alt > peso_dca:
        if peso_dca >= LIMIAR_MANTER:
            return anotar(_resultado_evidencia(
                caso, dca, "DCA (provisória)", "E5", "PENDENTE",
                f"Evidências fortes em conflito: DCA {_p(peso_dca)} × {_fontes_texto([f for f, _ in grupo_alt])} "
                f"{_p(peso_alt)}.{notas}", peso_dca=peso_dca, peso_alternativa=peso_alt, grupo=grupo_alt))
        return anotar(_substituir(caso, contexto, grupo_alt, peso_dca, peso_alt, "E2D",
                           ("DCA isolada: nenhuma fonte a confirma e há consenso contrário." if peso_dca == 0
                            else "DCA contrariada por evidência forte.") + notas, dca=dca))

    # 1 e 6: manutenção da DCA pela soma dos pesos (o RREO desempata o perfil D)
    impedimentos = []
    if criticos:
        impedimentos.append(f"alertas impeditivos na DCA: {', '.join(sorted(criticos))}")
    if temporal:
        impedimentos.append(temporal)
    if estadual and "SICONFI_RREO" not in {f for f, _ in confirmam}:
        impedimentos.append("ICMS estadual exige também o RREO concordante")

    def manter(regra: str, texto: str) -> dict:
        status = "PENDENTE" if impedimentos else "AUTOMATICA"
        unilateral = contrarias and all(v < dca for _, v in contrarias)
        return anotar(_resultado_evidencia(
            caso, dca, "DCA", regra, status,
            texto + notas
            + (" Divergência unilateral para menos (perfil típico de conceito, p. ex. FUNDEB líquido)." if unilateral and regra in ("E1A", "E1B") else "")
            + (" Impedimentos: " + "; ".join(impedimentos) + "." if impedimentos else ""),
            peso_dca=peso_dca, peso_alternativa=peso_alt, grupo=grupo_alt))

    confirmadoras = _fontes_texto([f for f, _ in confirmam] + [f"{SIGLAS.get(f, f)} (a menos de parcela)" for f in explicadas])
    if peso_dca >= LIMIAR_SUBSTITUIR:
        return anotar(manter("E1A", f"DCA confirmada por {confirmadoras} (peso {_p(peso_dca)})."))
    if peso_dca >= LIMIAR_MANTER and peso_dca > peso_alt:
        return anotar(manter("E1B", f"DCA confirmada por {confirmadoras} (peso {_p(peso_dca)}),"
                             f" acima da evidência contrária ({_p(peso_alt)})."))
    if peso_dca >= PESO_MINIMO_MARGEM and peso_dca - peso_alt >= MARGEM_MANTER:
        return anotar(manter("E1B", f"DCA confirmada por {confirmadoras} (peso {_p(peso_dca)}),"
                             f" com folga sobre a evidência contrária ({_p(peso_alt)}); prevalece a DCA (art. 116, § 2º)."))

    # 9: divergência que não move a receita média do ente
    maior = max((abs(dca - v) for _, v in contrarias), default=0)
    if contrarias and _imaterial(caso, maior, contexto):
        reais = f"{maior / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        limite = f"{LIMITE_IMATERIAL_ENTE:.1%}".replace(".", ",")
        return anotar(manter("E1C", f"Divergência imaterial: a maior diferença (R$ {reais}) altera menos de {limite} "
                             "da receita média do ente."))
    if not contrarias and explicadas:
        return anotar(manter("E1D", "Todas as divergências são explicadas por parcelas declaradas (diferença de conceito)."))

    # 2: diferença pequena sem evidência forte contra: prevalece a DCA (art. 116, § 2º), presumida de conceito
    if grupo_alt:
        diferenca = _pct_diferenca(dca, _medoide(grupo_alt)[1]) or 0
        if diferenca <= FAIXA_CONCEITO:
            pct = f"{diferenca:.1%}".replace(".", ",")
            return anotar(manter("E1D", f"Diferença de {pct} sem evidência forte contra a DCA (peso {_p(peso_alt)}): "
                                 "prevalece a DCA, presumida diferença de conceito ou período."))
    if not outras:
        return anotar(_resultado_evidencia(caso, dca, "DCA (provisória)", "E4", "PENDENTE",
                                    "A DCA não possui outra fonte comparável." + notas))
    return anotar(_resultado_evidencia(
        caso, dca, "DCA (provisória)", "E4", "PENDENTE",
        f"Sem evidência suficiente: a favor da DCA {_p(peso_dca)}, contra {_p(peso_alt)}"
        + (f" ({_fontes_texto([f for f, _ in grupo_alt])})" if grupo_alt else "") + "." + notas
        + (" Impedimentos: " + "; ".join(impedimentos) + "." if impedimentos else ""),
        peso_dca=peso_dca, peso_alternativa=peso_alt, grupo=grupo_alt))


# ------------------------------------------------------------------ 5 · cópia entre entes

def aplicar_copias(linhas: list[dict]) -> int:
    """Valor idêntico (até o centavo) na DCA de dois ou mais municípios, todos 'confirmados': cópia entre sistemas.

    Se mais de um membro do grupo seria mantido automaticamente, nenhum deles é: as confirmações vêm,
    provavelmente, de uma mesma cópia. Devolve quantos casos voltaram a pendente."""
    grupos: dict[tuple, list[dict]] = {}
    for linha in linhas:
        caso: EntradaCaso = linha["caso"]
        dca = _valor_evidencia(caso, "SICONFI_DCA")
        if caso.esfera != "MUNICIPIO" or not dca or dca < COPIA_VALOR_MINIMO:
            continue
        grupos.setdefault((caso.componente, caso.exercicio, dca), []).append(linha)
    alterados = 0
    for (_comp, _ano, _valor), membros in grupos.items():
        if len(membros) < 2:
            continue
        mantidos = [m for m in membros if m["evidencia"]["regra"] in ("E1A", "E1B") and m["evidencia"]["status"] == "AUTOMATICA"]
        if len(mantidos) < 2:
            continue
        nomes = ", ".join(f'{m["caso"].nome_ente}-{m["caso"].uf}' for m in membros)
        for membro in mantidos:
            resultado = membro["evidencia"]
            resultado["status"] = "PENDENTE"
            resultado["regra"] += "+CPY"
            resultado["justificativa"] += (
                f" Valor idêntico até o centavo, e confirmado, em mais de um município ({nomes}): indício de cópia"
                " entre sistemas; as confirmações não bastam."
            )
            _atualizar_cenario_aprovado(resultado)
            alterados += 1
    return alterados


def _carregar(session: Session) -> tuple[cf.Execucao | None, list[EntradaCaso]]:
    execucao = ultima_execucao(session)
    if execucao is None:
        return None, []
    linhas = session.execute(
        select(
            cf.Caso.id, cf.Caso.id_ente, cf.Caso.nome_ente, cf.Caso.uf, cf.Caso.esfera, Ente.capital,
            cf.Caso.exercicio, cf.Caso.componente_id, cf.Caso.situacao_exercicio, cf.Caso.diagnostico,
            cf.Caso.declaracao_principal, cf.Caso.valor_principal, cf.Caso.estado_principal,
            cf.Caso.confianca, cf.Caso.confianca_motivo, cf.Caso.tipos, cf.Caso.maior_dif_abs, cf.Caso.valores,
        )
        .join(Ente, Ente.id_ente == cf.Caso.id_ente)
        .where(cf.Caso.execucao_id == execucao.id)
        .order_by(cf.Caso.id)
    )
    casos = []
    for linha in linhas:
        casos.append(EntradaCaso(
            id=linha[0], id_ente=linha[1], nome_ente=linha[2], uf=linha[3], esfera=linha[4], capital=bool(linha[5]),
            exercicio=linha[6], componente=linha[7], situacao_exercicio=linha[8], diagnostico=linha[9],
            principal=linha[10], valor_principal=linha[11], estado_principal=linha[12], confianca=linha[13],
            confianca_motivo=linha[14] or "", tipos=set(filter(None, (linha[15] or "").split("|"))),
            maior_dif_abs=int(linha[16] or 0), valores=expandir_valores(linha[17]),
        ))
    return execucao, casos


def _contexto_estimativas(casos: list[EntradaCaso]) -> tuple[dict[int, int], dict[tuple[int, str, int], int]]:
    totais: dict[int, int] = {}
    serie: dict[tuple[int, str, int], int] = {}
    for caso in casos:
        if _fora_universo(caso):
            continue
        valor = caso.valor_principal
        if valor is None:
            continue
        totais[caso.exercicio] = totais.get(caso.exercicio, 0) + valor
        serie[(caso.id_ente, caso.componente, caso.exercicio)] = valor
    return totais, serie


def _estimativa_vizinhos(caso: EntradaCaso, totais: dict[int, int], serie: dict[tuple[int, str, int], int]) -> tuple[int | None, str]:
    candidatos: list[tuple[int, str]] = []
    for direcao in (-1, 1):
        ano = caso.exercicio + direcao
        valor = serie.get((caso.id_ente, caso.componente, ano))
        if valor is None or not totais.get(ano) or not totais.get(caso.exercicio):
            continue
        estimado = int((Decimal(valor) * Decimal(totais[caso.exercicio]) / Decimal(totais[ano])).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ))
        candidatos.append((estimado, f"{ano} × total nacional {caso.exercicio} ÷ total nacional {ano}"))
    if not candidatos:
        return None, ""
    valor = _mediana_centavos([v for v, _ in candidatos])
    return valor, "mediana(" + "; ".join(f for _, f in candidatos) + ")"


def _metricas(casos: list[dict], nome_modelo: str, campo_valor: str = "valor_cenario") -> tuple[dict[int, dict], int]:
    componentes_anos: dict[str, set[int]] = {}
    por_ente_comp_ano: dict[tuple[int, str, int], int] = {}
    for linha in casos:
        caso: EntradaCaso = linha["caso"]
        resultado = linha[nome_modelo]
        if not resultado["aplicavel"]:
            continue
        componentes_anos.setdefault(caso.componente, set()).add(caso.exercicio)
        por_ente_comp_ano[(caso.id_ente, caso.componente, caso.exercicio)] = int(resultado[campo_valor] or 0)

    por_ente: dict[int, Decimal] = {}
    entes_componentes = {
        (linha["caso"].id_ente, linha["caso"].componente)
        for linha in casos if linha[nome_modelo]["aplicavel"]
    }
    for id_ente, componente in entes_componentes:
        anos = componentes_anos.get(componente) or set()
        if not anos:
            continue
        total = sum(por_ente_comp_ano.get((id_ente, componente, ano), 0) for ano in anos)
        por_ente[id_ente] = por_ente.get(id_ente, Decimal(0)) + Decimal(total) / Decimal(len(anos))
    total_nacional = sum(por_ente.values(), Decimal(0))
    saida = {
        id_ente: {
            "receita_media": int(valor.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            "coeficiente": float(valor / total_nacional) if total_nacional else 0.0,
        }
        for id_ente, valor in por_ente.items()
    }
    return saida, int(total_nacional.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _anotar_impactos(linhas: list[dict]) -> dict[str, dict[int, dict]]:
    metricas = {}
    totais = {}
    metricas["original"], totais["original"] = _metricas(linhas, "original")
    metricas["degraus"], totais["degraus"] = _metricas(linhas, "degraus")
    metricas["evidencia"], totais["evidencia"] = _metricas(linhas, "evidencia", "valor_aprovado")
    metricas["evidencia_potencial"], totais["evidencia_potencial"] = _metricas(
        linhas, "evidencia", "valor_cenario"
    )
    metricas["funil"], totais["funil"] = _metricas(linhas, "funil", "valor_aprovado")
    for linha in linhas:
        caso: EntradaCaso = linha["caso"]
        base = metricas["original"].get(caso.id_ente, {"receita_media": 0, "coeficiente": 0.0})
        for modelo in ("degraus", "evidencia", "funil"):
            atual = metricas[modelo].get(caso.id_ente, {"receita_media": 0, "coeficiente": 0.0})
            linha[modelo].update(
                receita_media_original=base["receita_media"], receita_media=atual["receita_media"],
                diferenca_receita_media=atual["receita_media"] - base["receita_media"],
                coeficiente_original=base["coeficiente"], coeficiente=atual["coeficiente"],
                diferenca_coeficiente=atual["coeficiente"] - base["coeficiente"],
            )
        potencial = metricas["evidencia_potencial"].get(caso.id_ente, {"receita_media": 0, "coeficiente": 0.0})
        linha["evidencia"].update(
            receita_media_potencial=potencial["receita_media"],
            diferenca_receita_media_potencial=potencial["receita_media"] - base["receita_media"],
            coeficiente_potencial=potencial["coeficiente"],
            diferenca_coeficiente_potencial=potencial["coeficiente"] - base["coeficiente"],
        )
        linha["diferenca_modelos"] = linha["evidencia"]["valor_aprovado"] - linha["degraus"]["valor_cenario"]
        linha["diferenca_modelos_potencial"] = (
            linha["evidencia"]["valor_cenario"] - linha["degraus"]["valor_cenario"]
        )
        linha["diferenca_funil_degraus"] = linha["funil"]["valor_aprovado"] - linha["degraus"]["valor_cenario"]
        linha["diferenca_funil_evidencia"] = linha["funil"]["valor_aprovado"] - linha["evidencia"]["valor_aprovado"]
        linha["modelos_divergem"] = (
            linha["diferenca_modelos"] != 0
            or linha["diferenca_modelos_potencial"] != 0
            or linha["diferenca_funil_degraus"] != 0
            or linha["diferenca_funil_evidencia"] != 0
            or linha["degraus"]["status"] != linha["evidencia"]["status"]
            or linha["funil"]["status"] != linha["evidencia"]["status"]
            or linha["degraus"]["fonte"] != linha["evidencia"]["fonte"]
            or linha["funil"]["fonte"] != linha["evidencia"]["fonte"]
        )
        linha["impacto_max"] = max(
            abs(linha["degraus"]["diferenca_receita_media"]),
            abs(linha["evidencia"]["diferenca_receita_media"]),
            abs(linha["evidencia"]["diferenca_receita_media_potencial"]),
            abs(linha["funil"]["diferenca_receita_media"]),
        )
    return {"entes": metricas, "totais": totais}


def _percentil(valores: list[int], proporcao: float) -> int:
    if not valores:
        return 0
    ordenados = sorted(valores)
    indice = max(0, math.ceil(proporcao * len(ordenados)) - 1)
    return ordenados[indice]


def _anotar_materialidade(linhas: list[dict]) -> None:
    """Anota a relevância do caso (porte e impacto) para ordenar e auditar.

    Na M2.3 a materialidade já atua dentro da regra: uma SUBSTITUIÇÃO material fica com status
    VISTO (proposta, sujeita a confirmação humana). Aqui ficam só os destaques exibidos na tela."""
    totais_componente_ano: dict[tuple[str, int], int] = {}
    for linha in linhas:
        caso: EntradaCaso = linha["caso"]
        if linha["original"]["aplicavel"] and caso.valor_principal is not None:
            chave = (caso.componente, caso.exercicio)
            totais_componente_ano[chave] = totais_componente_ano.get(chave, 0) + abs(caso.valor_principal)
    for linha in linhas:
        resultado = linha["evidencia"]
        caso: EntradaCaso = linha["caso"]
        escore = max(abs(resultado["diferenca"]), int(resultado.get("impacto_conflito") or 0))
        total = totais_componente_ano.get((caso.componente, caso.exercicio), 0)
        participacao = escore / total if total else 0.0
        resultado["impacto_score"] = escore
        resultado["impacto_participacao"] = participacao
        destaques = []
        if caso.esfera != "MUNICIPIO":
            destaques.append("Estado/DF")
        elif caso.capital:
            destaques.append("capital")
        if escore >= LIMITE_DESTAQUE_IMPACTO_ABS:
            destaques.append("diferença de pelo menos R$ 50 milhões")
        if participacao >= LIMITE_DESTAQUE_PARTICIPACAO:
            destaques.append("pelo menos 0,05% do componente no exercício")
        resultado["relevancia"] = destaques


def _ranking(linhas: list[dict], modelo: str) -> dict[str, list[dict]]:
    vistos = {}
    for linha in linhas:
        caso: EntradaCaso = linha["caso"]
        if caso.esfera != "MUNICIPIO" or caso.id_ente in vistos:
            continue
        resultado = linha[modelo]
        vistos[caso.id_ente] = {
            "id_ente": caso.id_ente, "nome": caso.nome_ente, "uf": caso.uf,
            "receita_media_original": resultado["receita_media_original"],
            "receita_media": resultado["receita_media"],
            "diferenca_receita_media": resultado["diferenca_receita_media"],
            "coeficiente_original": resultado["coeficiente_original"],
            "coeficiente": resultado["coeficiente"],
            "diferenca_coeficiente": resultado["diferenca_coeficiente"],
        }
    ganhos = sorted((v for v in vistos.values() if v["diferenca_coeficiente"] > 0),
                    key=lambda v: (-v["diferenca_coeficiente"], v["id_ente"]))
    perdas = sorted((v for v in vistos.values() if v["diferenca_coeficiente"] < 0),
                    key=lambda v: (v["diferenca_coeficiente"], v["id_ente"]))
    return {"ganhos": ganhos, "perdas": perdas, "ganhos_top": ganhos[:15], "perdas_top": perdas[:15]}


def _montar_contexto(session: Session, casos: list[EntradaCaso]) -> ContextoEvidencia:
    contexto = ContextoEvidencia()
    anos: dict[str, set[int]] = {}
    por_ente_comp: dict[tuple[int, str], int] = {}
    soma_cota: dict[tuple[str, int], int] = {}
    for caso in casos:
        contexto.uf_ente[caso.id_ente] = caso.uf
        item = caso.valores.get("SICONFI_DCA")
        if _presente(item) and item.get("estado") == "VALOR":
            contexto.serie_dca.setdefault((caso.id_ente, caso.componente), {})[caso.exercicio] = int(item["original"])
        if _fora_universo(caso) or caso.valor_principal is None:
            continue
        anos.setdefault(caso.componente, set()).add(caso.exercicio)
        por_ente_comp[(caso.id_ente, caso.componente)] = por_ente_comp.get((caso.id_ente, caso.componente), 0) + caso.valor_principal
        if caso.componente == "COTA_ICMS_MUN":
            soma_cota[(caso.uf, caso.exercicio)] = soma_cota.get((caso.uf, caso.exercicio), 0) + caso.valor_principal
    contexto.anos_componente = {comp: len(lista) for comp, lista in anos.items()}
    for (id_ente, comp), total in por_ente_comp.items():
        media = total // max(1, contexto.anos_componente.get(comp, 1))
        contexto.receita_media_ente[id_ente] = contexto.receita_media_ente.get(id_ente, 0) + media
    contexto.total_nacional = sum(contexto.receita_media_ente.values())

    # 7 · parcelas declaradas (SIOPE, TCE e a própria DCA)
    consulta = select(cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.componente_id, cf.Registro.declaracao_id,
                      cf.Registro.parcela, cf.Registro.valor_centavos).where(
        cf.Registro.vigente.is_(True), cf.Registro.declaracao_id.in_(("SIOPE", "TCE", "SICONFI_DCA")),
        cf.Registro.parcela.in_(PARCELAS_DECOMPOSICAO), cf.Registro.valor_centavos.is_not(None))
    for id_ente, ano, comp, declaracao, parcela, valor in session.execute(consulta):
        if comp == "COTA_ICMS_MUN" and parcela in ("FUNDEB", "FECOP_FUNDEB"):
            continue  # o FUNDEB da cota-parte é tratado pela conversão bruto/líquido (regra E2E e fontes líquidas)
        contexto.parcelas.setdefault((id_ente, ano, comp, declaracao), {})[parcela] = valor

    # 10 · repasse declarado pelo Estado (DCA; na falta, SIOPS)
    repasses: dict[tuple[str, int], dict[str, int]] = {}
    consulta = select(cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.declaracao_id, cf.Registro.valor_centavos).where(
        cf.Registro.vigente.is_(True), cf.Registro.campo == "COTA_REPASSADA", cf.Registro.valor_centavos.is_not(None))
    ufs = dict(session.execute(select(Ente.id_ente, Ente.uf)).all())
    for id_ente, ano, declaracao, valor in session.execute(consulta):
        repasses.setdefault((ufs.get(id_ente), ano), {})[declaracao] = valor
    for chave, fontes in repasses.items():
        repasse = fontes.get("SICONFI_DCA") or fontes.get("SIOPS")
        soma = soma_cota.get(chave, 0)
        if repasse and soma and CONSERVACAO_FAIXA_VALIDA[0] <= soma / repasse <= CONSERVACAO_FAIXA_VALIDA[1]:
            contexto.conservacao[chave] = {"repasse": repasse, "soma": soma}
    return contexto


def _contexto_funil(session: Session, casos: list[EntradaCaso]) -> funil.ContextoFunil:
    parcelas: dict[tuple[int, int, str], dict[str, int]] = {}
    consulta = select(cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.componente_id, cf.Registro.parcela,
                      cf.Registro.valor_centavos).where(
        cf.Registro.vigente.is_(True), cf.Registro.declaracao_id == "TCE",
        cf.Registro.componente_id.in_(("ISS_MUN", "ISS_DF")), cf.Registro.valor_centavos.is_not(None))
    for id_ente, ano, comp, parcela, valor in session.execute(consulta):
        parcelas.setdefault((id_ente, ano, comp), {})[parcela] = valor
    # compensação da LC 194 registrada pelo Município na DCA (ajuste E4 da cota-parte)
    compensacoes = dict(((id_ente, ano), valor) for id_ente, ano, valor in session.execute(
        select(cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.valor_centavos).where(
            cf.Registro.vigente.is_(True), cf.Registro.declaracao_id == "SICONFI_DCA",
            cf.Registro.componente_id == "COTA_ICMS_MUN", cf.Registro.parcela == "COMPENSACAO",
            cf.Registro.valor_centavos.is_not(None))))
    return funil.montar_contexto(casos, parcelas, compensacoes)


def _resumo_conservacao(linhas: list[dict], contexto: ContextoEvidencia) -> dict:
    """Quantas UF-exercícios fecham a soma das cotas municipais com o repasse do Estado (±2%) em cada cenário."""
    somas = {modelo: {} for modelo in ("original", "degraus", "evidencia", "evidencia_potencial", "funil")}
    for linha in linhas:
        caso: EntradaCaso = linha["caso"]
        if caso.componente != "COTA_ICMS_MUN" or not linha["original"]["aplicavel"]:
            continue
        chave = (caso.uf, caso.exercicio)
        somas["original"][chave] = somas["original"].get(chave, 0) + (caso.valor_principal or 0)
        somas["degraus"][chave] = somas["degraus"].get(chave, 0) + int(linha["degraus"]["valor_cenario"] or 0)
        somas["evidencia"][chave] = somas["evidencia"].get(chave, 0) + int(linha["evidencia"]["valor_aprovado"] or 0)
        somas["evidencia_potencial"][chave] = (somas["evidencia_potencial"].get(chave, 0)
                                               + int(linha["evidencia"]["valor_cenario"] or 0))
        somas["funil"][chave] = somas["funil"].get(chave, 0) + int(linha["funil"]["valor_aprovado"] or 0)
    resultado = {}
    for modelo, valores in somas.items():
        dentro = desvio = n = 0
        for chave, dados in contexto.conservacao.items():
            if chave not in valores:
                continue
            razao = valores[chave] / dados["repasse"]
            n += 1
            dentro += abs(razao - 1) <= 0.02
            desvio += abs(razao - 1)
        resultado[modelo] = {"uf_anos": n, "dentro_2pct": dentro, "desvio_medio": desvio / n if n else 0.0}
    return resultado


def simular(session: Session) -> dict:
    execucao, casos = _carregar(session)
    if execucao is None:
        return {"execucao": None, "linhas": [], "resumo": {}, "rankings": {}}
    totais, serie = _contexto_estimativas(casos)
    contexto = _montar_contexto(session, casos)
    contexto_funil = _contexto_funil(session, casos)
    linhas = []
    for caso in casos:
        original = _resultado(
            caso.valor_principal, caso.valor_principal, SIGLAS.get(caso.principal, caso.principal or "—"),
            "ATUAL", "REFERENCIA", "Valor principal vigente na execução de validação.",
            aplicavel=not bool(_fora_universo(caso)),
        )
        linhas.append({
            "caso": caso,
            "original": original,
            "degraus": aplicar_degraus(caso, _estimativa_vizinhos(caso, totais, serie)),
            "evidencia": aplicar_evidencia(caso, contexto),
            "funil": funil.aplicar_funil(caso, contexto_funil, _fora_universo(caso)),
        })
    copias = aplicar_copias(linhas)
    duplicidades_funil = funil.marcar_verificacoes(linhas)
    _anotar_materialidade(linhas)
    agregados = _anotar_impactos(linhas)
    rankings = {modelo: _ranking(linhas, modelo) for modelo in ("degraus", "evidencia", "funil")}
    aplicaveis = [linha for linha in linhas if linha["original"]["aplicavel"]]
    regras = {}
    etapas_funil: dict[str, int] = {}
    for linha in aplicaveis:
        chave = (linha["evidencia"]["regra"].split("+")[0], linha["evidencia"]["status"])
        regras[chave] = regras.get(chave, 0) + 1
        etapas_funil[linha["funil"]["regra"]] = etapas_funil.get(linha["funil"]["regra"], 0) + 1
    acumulado = 0
    funil_etapas = []
    for codigo in funil.ROTULO_ETAPA:
        quantidade = etapas_funil.get(codigo, 0)
        acumulado += quantidade if codigo != "FP" else 0
        funil_etapas.append({"regra": codigo, "rotulo": funil.ROTULO_ETAPA[codigo], "casos": quantidade,
                             "pct": quantidade / len(aplicaveis) if aplicaveis else 0.0,
                             "acumulado": (acumulado / len(aplicaveis) if aplicaveis else 0.0) if codigo != "FP" else None})
    resumo = {
        "casos": len(aplicaveis),
        "entes": len({linha["caso"].id_ente for linha in aplicaveis}),
        "divergencias": sum(linha["modelos_divergem"] for linha in aplicaveis),
        "ajustes_degraus": sum(linha["degraus"]["diferenca"] != 0 for linha in aplicaveis),
        "ajustes_evidencia": sum(linha["evidencia"]["diferenca_aprovada"] != 0 for linha in aplicaveis),
        "candidatos_evidencia": sum(linha["evidencia"]["diferenca"] != 0 for linha in aplicaveis),
        "automaticas_degraus": sum(linha["degraus"]["status"] == "AUTOMATICA" for linha in aplicaveis),
        "pendentes_degraus": sum(linha["degraus"]["status"] == "PENDENTE" for linha in aplicaveis),
        "automaticas_evidencia": sum(linha["evidencia"]["status"] == "AUTOMATICA" for linha in aplicaveis),
        "visto_evidencia": sum(linha["evidencia"]["status"] == "VISTO" for linha in aplicaveis),
        "pendentes_evidencia": sum(linha["evidencia"]["status"] == "PENDENTE" for linha in aplicaveis),
        "copias_evidencia": copias,
        "automaticas_funil": sum(linha["funil"]["status"] == "AUTOMATICA" for linha in aplicaveis),
        "pendentes_funil": sum(linha["funil"]["status"] == "PENDENTE" for linha in aplicaveis),
        "ajustes_funil": sum(linha["funil"]["diferenca_aprovada"] != 0 for linha in aplicaveis),
        "confirmacao_externa_funil": sum(linha["funil"]["confirmacao_externa"] for linha in aplicaveis),
        "marcados_funil": sum(bool(linha["funil"]["marcadores"]) for linha in aplicaveis),
        "duplicidades_funil": duplicidades_funil,
        "etapas_funil": funil_etapas,
        "grupos_tce_liquido": sorted(contexto_funil.grupos_tce_liquido),
        "regras_evidencia": sorted(regras.items()),
        "conservacao": _resumo_conservacao(linhas, contexto),
        "total_original": agregados["totais"]["original"],
        "total_degraus": agregados["totais"]["degraus"],
        "total_evidencia": agregados["totais"]["evidencia"],
        "total_evidencia_potencial": agregados["totais"]["evidencia_potencial"],
        "total_funil": agregados["totais"]["funil"],
        "municipios_ganham_funil": len(rankings["funil"]["ganhos"]),
        "municipios_perdem_funil": len(rankings["funil"]["perdas"]),
        "municipios_ganham_degraus": len(rankings["degraus"]["ganhos"]),
        "municipios_perdem_degraus": len(rankings["degraus"]["perdas"]),
        "municipios_ganham_evidencia": len(rankings["evidencia"]["ganhos"]),
        "municipios_perdem_evidencia": len(rankings["evidencia"]["perdas"]),
    }
    return {
        "execucao": execucao, "linhas": linhas, "resumo": resumo, "rankings": rankings,
        "parametros": {
            "degraus": "0,5% com piso R$ 5; SIOPS 2019 fora; Fundeb ÷ 0,80; mediana; estimativa por vizinhos",
            "evidencia": (
                f"pesos: TCE, BB e Sefaz 1,0 · SIOPE+SIOPS 1,25 (um só 0,75) · RREO e Confaz 0,5 · MSC 0,25 · conservação da UF 0,5; "
                f"manter a DCA com peso ≥ {_p(LIMIAR_MANTER)} (ou ≥ {_p(PESO_MINIMO_MARGEM)} com folga de "
                f"{_p(MARGEM_MANTER)}); trocar com peso ≥ {_p(LIMIAR_SUBSTITUIR)} e maior que o da DCA "
                f"({_p(LIMIAR_SUBSTITUIR_ISOLADA)} se nada confirma a DCA; {_p(LIMIAR_SUBSTITUIR_AUSENTE)} se ela falta); concordância 1% ou R$ 100; divergência "
                "imaterial (≤ 0,1% da receita média do ente) não decide; troca material ou estadual sujeita a visto; "
                "teste temporal, cópia entre entes e decomposição por parcelas; " + CONCEITO_COTA
                + ", com as fontes declaradas líquidas do FUNDEB convertidas para o bruto"
            ),
            "funil": (
                "etapas na ordem: 1a DCA = fonte externa (± R$ 10,00); 1b DCA corrigida (Fundeb ÷ 0,8 ou ÷ 1,2, "
                "unidade, compensação da LC 194 registrada à parte; no ISS, deduções não abatidas e composição "
                "incompleta); 2a RREO, SIOPS ou SIOPE = fonte externa; 2b demonstrativo corrigido; 3a DCA = dois "
                "demonstrativos iguais (sem DCA, dois iguais); 3b concordância após correção; 4 fonte externa "
                "sozinha; sem nada disso, pendente. Fontes externas: cota-parte, Sefaz, BB e tribunal de contas (líquido "
                "do Fundeb convertido ao bruto por grupo, pela razão ÷ Sefaz), nessa ordem; ISS, tribunal "
                "de contas. Verificações V1 a V7 como marcadores; dedução do art. X+7 ainda não aplicada"
            ),
            "impacto": (
                "média nominal dos exercícios exigíveis por componente; cenário aprovado aplica só decisões "
                "automáticas (conserva o original nas pendências e nos vistos); cenário potencial usa todos os "
                "candidatos; sem atualização monetária P6"
            ),
        },
        "politica_evidencia": POLITICA_EVIDENCIA_VERSAO,
    }


def filtrar(simulacao: dict, filtros: FiltrosSimulador, tamanho: int = 50, paginar: bool = True) -> dict:
    linhas = list(simulacao.get("linhas") or [])
    if filtros.q:
        busca = sem_acento(filtros.q)
        linhas = [l for l in linhas if busca in sem_acento(l["caso"].nome_ente)
                  or str(l["caso"].id_ente).startswith(filtros.q)]
    if filtros.uf:
        linhas = [l for l in linhas if l["caso"].uf == filtros.uf]
    if filtros.componente:
        linhas = [l for l in linhas if l["caso"].componente == filtros.componente]
    if filtros.status:
        modelo, _, status = filtros.status.partition("_")
        chave = modelo if modelo in ("evidencia", "funil") else "degraus"
        linhas = [l for l in linhas if l[chave]["status"] == status.upper()]
    if filtros.diferenca == "modelos":
        linhas = [l for l in linhas if l["modelos_divergem"]]
    elif filtros.diferenca == "ajuste":
        linhas = [l for l in linhas if l["degraus"]["diferenca"] or l["evidencia"]["diferenca"] or l["funil"]["diferenca"]]

    if filtros.ordem == "ente":
        linhas.sort(key=lambda l: (l["caso"].uf, sem_acento(l["caso"].nome_ente), l["caso"].exercicio, l["caso"].componente))
    elif filtros.ordem == "diferenca":
        linhas.sort(key=lambda l: (-max(abs(l["diferenca_modelos"]), abs(l["diferenca_modelos_potencial"]),
                                        abs(l["diferenca_funil_degraus"]), abs(l["diferenca_funil_evidencia"])), l["caso"].id))
    else:
        linhas.sort(key=lambda l: (-l["impacto_max"], -abs(l["diferenca_modelos"]), l["caso"].id))

    total = len(linhas)
    paginas = max(1, math.ceil(total / tamanho))
    filtros.pagina = min(filtros.pagina, paginas)
    exibidas = linhas
    if paginar:
        inicio = (filtros.pagina - 1) * tamanho
        exibidas = linhas[inicio:inicio + tamanho]
    return {"linhas": exibidas, "total": total, "paginas": paginas}


# Colunas da exportação: (campo, título no Excel, tipo).  O CSV mantém os nomes técnicos; os dois
# formatos trazem valores em reais (no CSV, com vírgula decimal, para abrir direto no Excel).
COLUNAS_EXPORTACAO = [
    ("execucao", "Execução", "inteiro"),
    ("codigo_ente", "Código IBGE", "inteiro"),
    ("ente", "Ente", "texto"),
    ("uf", "UF", "texto"),
    ("esfera", "Esfera", "texto"),
    ("exercicio", "Exercício", "inteiro"),
    ("componente", "Componente", "texto"),
    ("politica_evidencia", "Política do Modelo 2", "texto"),
    ("valor_original", "Valor original (R$)", "moeda"),
    ("degraus_valor", "M1 · valor (R$)", "moeda"),
    ("degraus_fonte_formula", "M1 · fonte ou fórmula", "texto"),
    ("degraus_regra", "M1 · regra", "texto"),
    ("degraus_status", "M1 · status", "texto"),
    ("degraus_justificativa", "M1 · justificativa", "longo"),
    ("degraus_receita_media", "M1 · receita média do ente (R$)", "moeda"),
    ("degraus_coeficiente", "M1 · coeficiente", "coeficiente"),
    ("degraus_delta_coeficiente", "M1 · variação do coeficiente", "coeficiente"),
    ("evidencia_valor_candidato", "M2 · valor proposto (R$)", "moeda"),
    ("evidencia_valor_aprovado", "M2 · valor aprovado (R$)", "moeda"),
    ("evidencia_fonte_formula", "M2 · fonte ou fórmula", "texto"),
    ("evidencia_regra", "M2 · regra", "texto"),
    ("evidencia_status", "M2 · status", "texto"),
    ("evidencia_justificativa", "M2 · justificativa", "longo"),
    ("evidencia_peso_dca", "M2 · peso a favor da DCA", "peso"),
    ("evidencia_peso_alternativa", "M2 · peso da alternativa", "peso"),
    ("evidencia_fontes_alternativa", "M2 · fontes da alternativa", "texto"),
    ("evidencia_motivos_visto", "M2 · motivos do visto", "texto"),
    ("evidencia_relevancia", "M2 · relevância", "texto"),
    ("evidencia_fontes_liquidas_fundeb", "M2 · fontes declaradas líquidas do FUNDEB", "texto"),
    ("evidencia_receita_media_aprovada", "M2 · receita média aprovada (R$)", "moeda"),
    ("evidencia_coeficiente_aprovado", "M2 · coeficiente aprovado", "coeficiente"),
    ("evidencia_delta_coeficiente_aprovado", "M2 · variação do coeficiente aprovado", "coeficiente"),
    ("evidencia_receita_media_potencial", "M2 · receita média potencial (R$)", "moeda"),
    ("evidencia_coeficiente_potencial", "M2 · coeficiente potencial", "coeficiente"),
    ("evidencia_delta_coeficiente_potencial", "M2 · variação do coeficiente potencial", "coeficiente"),
    ("diferenca_valor_modelos_aprovada", "Diferença M2 aprovado − M1 (R$)", "moeda"),
    ("diferenca_valor_modelos_potencial", "Diferença M2 potencial − M1 (R$)", "moeda"),
    ("funil_valor", "M3 · valor (R$)", "moeda"),
    ("funil_fonte_formula", "M3 · fonte ou ajuste", "texto"),
    ("funil_regra", "M3 · regra", "texto"),
    ("funil_etapa", "M3 · etapa", "texto"),
    ("funil_status", "M3 · status", "texto"),
    ("funil_confirmacao_externa", "M3 · confirmado por fonte externa", "texto"),
    ("funil_justificativa", "M3 · justificativa", "longo"),
    ("funil_marcadores", "M3 · marcadores de verificação", "texto"),
    ("funil_receita_media", "M3 · receita média do ente (R$)", "moeda"),
    ("funil_coeficiente", "M3 · coeficiente", "coeficiente"),
    ("funil_delta_coeficiente", "M3 · variação do coeficiente", "coeficiente"),
    ("diferenca_valor_funil_degraus", "Diferença M3 − M1 (R$)", "moeda"),
    ("diferenca_valor_funil_evidencia", "Diferença M3 − M2 aprovado (R$)", "moeda"),
]


def _registros_exportacao(simulacao: dict, filtros: FiltrosSimulador) -> list[dict]:
    """Uma linha por caso, com tipos nativos (centavos inteiros, frações, pesos em float)."""
    linhas = filtrar(simulacao, filtros, paginar=False)["linhas"]
    execucao = simulacao.get("execucao")
    registros = []
    for linha in linhas:
        caso: EntradaCaso = linha["caso"]
        d, e, f = linha["degraus"], linha["evidencia"], linha["funil"]
        registros.append({
            "execucao": execucao.id if execucao else None, "politica_evidencia": POLITICA_EVIDENCIA_VERSAO,
            "codigo_ente": caso.id_ente, "ente": caso.nome_ente,
            "uf": caso.uf, "esfera": caso.esfera, "exercicio": caso.exercicio,
            "componente": ROTULO_CURTO.get(caso.componente, caso.componente), "valor_original": caso.valor_principal,
            "degraus_valor": d["valor_proposto"], "degraus_fonte_formula": d["formula"] or d["fonte"],
            "degraus_regra": d["regra"], "degraus_status": d["status"],
            "degraus_justificativa": d["justificativa"], "degraus_receita_media": d["receita_media"],
            "degraus_coeficiente": d["coeficiente"],
            "degraus_delta_coeficiente": d["diferenca_coeficiente"],
            "evidencia_valor_candidato": e["valor_proposto"], "evidencia_valor_aprovado": e["valor_aprovado"],
            "evidencia_fonte_formula": e["formula"] or e["fonte"],
            "evidencia_regra": e["regra"], "evidencia_status": e["status"],
            "evidencia_justificativa": e["justificativa"],
            "evidencia_relevancia": " | ".join(e.get("relevancia") or []),
            "evidencia_peso_dca": float(e.get("peso_dca", 0) or 0),
            "evidencia_peso_alternativa": float(e.get("peso_alternativa", 0) or 0),
            "evidencia_fontes_alternativa": _fontes_texto(e.get("fontes_grupo") or []),
            "evidencia_motivos_visto": " | ".join(e.get("motivos_visto") or []),
            "evidencia_fontes_liquidas_fundeb": _fontes_texto(e.get("fontes_liquidas_fundeb") or []),
            "evidencia_receita_media_aprovada": e["receita_media"],
            "evidencia_coeficiente_aprovado": e["coeficiente"],
            "evidencia_delta_coeficiente_aprovado": e["diferenca_coeficiente"],
            "evidencia_receita_media_potencial": e["receita_media_potencial"],
            "evidencia_coeficiente_potencial": e["coeficiente_potencial"],
            "evidencia_delta_coeficiente_potencial": e["diferenca_coeficiente_potencial"],
            "diferenca_valor_modelos_aprovada": linha["diferenca_modelos"],
            "diferenca_valor_modelos_potencial": linha["diferenca_modelos_potencial"],
            "funil_valor": f["valor_proposto"], "funil_fonte_formula": f["formula"] or f["fonte"],
            "funil_regra": f["regra"], "funil_etapa": f["etapa"], "funil_status": f["status"],
            "funil_confirmacao_externa": "sim" if f["confirmacao_externa"] else "não",
            "funil_justificativa": f["justificativa"], "funil_marcadores": " | ".join(f["marcadores"]),
            "funil_receita_media": f["receita_media"], "funil_coeficiente": f["coeficiente"],
            "funil_delta_coeficiente": f["diferenca_coeficiente"],
            "diferenca_valor_funil_degraus": linha["diferenca_funil_degraus"],
            "diferenca_valor_funil_evidencia": linha["diferenca_funil_evidencia"],
        })
    return registros


def exportar_csv(simulacao: dict, filtros: FiltrosSimulador) -> bytes:
    arquivo = io.StringIO(newline="")
    campos = [campo for campo, _titulo, _tipo in COLUNAS_EXPORTACAO]
    tipos = {campo: tipo for campo, _titulo, tipo in COLUNAS_EXPORTACAO}
    escritor = csv.DictWriter(arquivo, fieldnames=campos, delimiter=";", lineterminator="\n")
    escritor.writeheader()
    for registro in _registros_exportacao(simulacao, filtros):
        saida = {}
        for campo, valor in registro.items():
            if valor is None:
                saida[campo] = ""
            elif tipos[campo] == "moeda":
                # Reais com vírgula decimal e sem separador de milhar: o Excel em português
                # lê como número (e não em notação científica de centavos).
                saida[campo] = f"{valor / 100:.2f}".replace(".", ",")
            elif tipos[campo] == "coeficiente":
                saida[campo] = f"{valor:.12f}".replace(".", ",")
            elif tipos[campo] == "peso":
                saida[campo] = f"{valor:.2f}".replace(".", ",")
            else:
                saida[campo] = valor
        escritor.writerow(saida)
    return ("﻿" + arquivo.getvalue()).encode("utf-8")


ROTULO_STATUS = {"AUTOMATICA": "Automática", "VISTO": "Sujeita a visto", "PENDENTE": "Pendente",
                 "EXCLUIDO": "Excluído", "REFERENCIA": "Referência"}


def _resumo_exportacao(simulacao: dict, filtros: FiltrosSimulador, registros: list[dict]) -> tuple[list, list]:
    """Pares (rótulo, valor) do cabeçalho do resumo e linhas (modelo · regra, automáticas, vistos, pendentes)."""
    execucao = simulacao.get("execucao")
    parametros = simulacao.get("parametros") or {}
    filtros_texto = ", ".join(f"{nome} = {valor}" for nome, valor in (
        ("busca", filtros.q), ("UF", filtros.uf), ("componente", filtros.componente), ("status", filtros.status),
        ("recorte", {"modelos": "casos em que os modelos divergem", "ajuste": "casos com ajuste",
                     "": "toda a base"}.get(filtros.diferenca, filtros.diferenca)),
    ) if valor) or "nenhum"
    pares = [
        ("Execução de validação", execucao.id if execucao else "—"),
        ("Política do Modelo 2", POLITICA_EVIDENCIA_VERSAO),
        ("Cota-parte do ICMS", CONCEITO_COTA),
        ("Filtros aplicados", filtros_texto),
        ("Casos exportados", len(registros)),
        ("Parâmetros do Modelo 1", parametros.get("degraus", "")),
        ("Parâmetros do Modelo 2", parametros.get("evidencia", "")),
        ("Parâmetros do Modelo 3", parametros.get("funil", "")),
        ("Cálculo do impacto", parametros.get("impacto", "")),
    ]
    contagem: dict[tuple[str, str], dict[str, int]] = {}
    for registro in registros:
        for modelo, prefixo in (("M1", "degraus"), ("M2", "evidencia"), ("M3", "funil")):
            por_status = contagem.setdefault((modelo, registro[f"{prefixo}_regra"]), {})
            status = registro[f"{prefixo}_status"]
            por_status[status] = por_status.get(status, 0) + 1
    linhas = [(f"{modelo} · {regra}", *(por_status.get(st) or None for st in ("AUTOMATICA", "VISTO", "PENDENTE")))
              for (modelo, regra), por_status in sorted(contagem.items())]
    return pares, linhas


NOTA_EXPORTACAO = ("Valores em reais. Status 'Sujeita a visto' e 'Pendente' conservam o valor original no cenário "
                   "aprovado; o cenário potencial usa o valor proposto.")


def _xlsx_rapido(simulacao: dict, filtros: FiltrosSimulador, registros: list[dict]) -> bytes:
    """Excel via xlsxwriter (bem mais rápido na base inteira)."""
    import xlsxwriter

    saida = io.BytesIO()
    livro = xlsxwriter.Workbook(saida, {"constant_memory": True, "in_memory": True})
    base = {"font_name": "Arial", "font_size": 10}
    formatos = {
        "moeda": livro.add_format({**base, "num_format": "#,##0.00"}),
        "coeficiente": livro.add_format({**base, "num_format": "0.000000000000"}),
        "peso": livro.add_format({**base, "num_format": "0.00"}),
        "inteiro": livro.add_format({**base, "num_format": "0"}),
        "texto": livro.add_format(base),
        "longo": livro.add_format({**base, "text_wrap": True, "valign": "top"}),
    }
    cab = livro.add_format({**base, "bold": True, "font_color": "#FFFFFF", "bg_color": "#1F4E79",
                            "text_wrap": True, "valign": "vcenter"})
    larguras = {"moeda": 18, "coeficiente": 18, "peso": 12, "inteiro": 11, "texto": 18, "longo": 60}
    aba = livro.add_worksheet("Casos")
    for indice, (_campo, titulo, tipo) in enumerate(COLUNAS_EXPORTACAO):
        aba.set_column(indice, indice, max(larguras[tipo], min(len(titulo) + 2, 40)), formatos[tipo])
        aba.write_string(0, indice, titulo, cab)
    aba.freeze_panes(1, 7)
    plano = [(campo, tipo, formatos[tipo], campo.endswith("_status")) for campo, _t, tipo in COLUNAS_EXPORTACAO]
    for numero, registro in enumerate(registros, start=1):
        for coluna, (campo, tipo, formato, eh_status) in enumerate(plano):
            valor = registro.get(campo)
            if valor is None or valor == "":
                continue
            if tipo == "moeda":
                aba.write_number(numero, coluna, round(valor / 100, 2), formato)
            elif tipo in ("coeficiente", "peso", "inteiro") and isinstance(valor, (int, float)):
                aba.write_number(numero, coluna, valor, formato)
            else:
                aba.write_string(numero, coluna, ROTULO_STATUS.get(valor, valor) if eh_status else str(valor), formato)
    aba.autofilter(0, 0, len(registros), len(COLUNAS_EXPORTACAO) - 1)

    resumo = livro.add_worksheet("Resumo")
    titulo = livro.add_format({**base, "font_size": 12, "bold": True})
    rotulo = livro.add_format({**base, "bold": True, "valign": "top"})
    texto = livro.add_format({**base, "text_wrap": True, "valign": "top"})
    numero_fmt = livro.add_format({**base, "num_format": "#,##0"})
    nota = livro.add_format({**base, "font_size": 9, "italic": True})
    resumo.set_column(0, 0, 38)
    resumo.set_column(1, 1, 70)
    resumo.set_column(2, 3, 16)
    pares, linhas = _resumo_exportacao(simulacao, filtros, registros)
    resumo.write_string(0, 0, "Simulador de escolha de valores — exportação", titulo)
    linha = 2
    for chave, valor in pares:
        resumo.write_string(linha, 0, chave, rotulo)
        if isinstance(valor, int):
            resumo.write_number(linha, 1, valor, numero_fmt)
        else:
            resumo.write_string(linha, 1, str(valor), texto)
        linha += 1
    linha += 1
    resumo.write_string(linha, 0, "Casos por regra e status (nesta exportação)", titulo)
    linha += 1
    for coluna, nome in enumerate(("Modelo · regra", "Automática", "Sujeita a visto", "Pendente")):
        resumo.write_string(linha, coluna, nome, cab)
    for item in linhas:
        linha += 1
        resumo.write_string(linha, 0, item[0], texto)
        for coluna, valor in enumerate(item[1:], start=1):
            if valor:
                resumo.write_number(linha, coluna, valor, numero_fmt)
    resumo.write_string(linha + 2, 0, NOTA_EXPORTACAO, nota)
    livro.close()
    return saida.getvalue()


def exportar_xlsx(simulacao: dict, filtros: FiltrosSimulador) -> bytes:
    """Mesmo conteúdo do CSV, em Excel: valores em reais, números formatados, filtros e aba de resumo."""
    try:
        import xlsxwriter  # noqa: F401
    except ImportError:
        return _xlsx_openpyxl(simulacao, filtros)
    return _xlsx_rapido(simulacao, filtros, _registros_exportacao(simulacao, filtros))


def _xlsx_openpyxl(simulacao: dict, filtros: FiltrosSimulador) -> bytes:
    """Alternativa sem dependência extra (mais lenta na base inteira)."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    formatos = {"moeda": "#,##0.00", "coeficiente": "0.000000000000", "peso": "0.00", "inteiro": "0"}
    larguras = {"moeda": 18, "coeficiente": 18, "peso": 12, "inteiro": 11, "texto": 18, "longo": 60}
    fonte = Font(name="Arial", size=10)
    negrito = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    fundo = PatternFill("solid", fgColor="1F4E79")
    quebra = Alignment(wrap_text=True, vertical="top")

    livro = Workbook(write_only=True)
    aba = livro.create_sheet("Casos")
    aba.freeze_panes = "H2"
    for indice, (_campo, titulo, tipo) in enumerate(COLUNAS_EXPORTACAO, start=1):
        aba.column_dimensions[get_column_letter(indice)].width = max(larguras[tipo], min(len(titulo) + 2, 40))
    cabecalho = []
    for _campo, titulo, _tipo in COLUNAS_EXPORTACAO:
        celula = WriteOnlyCell(aba, value=titulo)
        celula.font, celula.fill, celula.alignment = negrito, fundo, Alignment(wrap_text=True, vertical="center")
        cabecalho.append(celula)
    aba.append(cabecalho)
    registros = _registros_exportacao(simulacao, filtros)
    # Um estilo pronto por tipo de coluna: copiar o StyleArray é muito mais rápido que
    # atribuir fonte e formato célula a célula (base inteira: ~80 mil linhas × 37 colunas).
    modelos_estilo = {}
    for tipo in larguras:
        modelo = WriteOnlyCell(aba)
        modelo.font = fonte
        if tipo in formatos:
            modelo.number_format = formatos[tipo]
        if tipo == "longo":
            modelo.alignment = quebra
        modelos_estilo[tipo] = modelo._style
    plano = [(campo, tipo, modelos_estilo[tipo], campo.endswith("_status")) for campo, _t, tipo in COLUNAS_EXPORTACAO]
    for registro in registros:
        linha = []
        for campo, tipo, estilo, eh_status in plano:
            valor = registro.get(campo)
            if valor is not None:
                if tipo == "moeda":
                    valor = round(valor / 100, 2)
                elif eh_status:
                    valor = ROTULO_STATUS.get(valor, valor)
            celula = WriteOnlyCell(aba, value=valor)
            celula._style = copy(estilo)
            linha.append(celula)
        aba.append(linha)
    ultima = get_column_letter(len(COLUNAS_EXPORTACAO))
    aba.auto_filter.ref = f"A1:{ultima}{len(registros) + 1}"

    # Resumo: filtros aplicados, parâmetros e contagens da consulta exportada.
    resumo = livro.create_sheet("Resumo")
    resumo.column_dimensions["A"].width = 38
    resumo.column_dimensions["B"].width = 22
    resumo.column_dimensions["C"].width = 22
    resumo.column_dimensions["D"].width = 22

    def titulo(texto):
        celula = WriteOnlyCell(resumo, value=texto)
        celula.font = Font(name="Arial", size=12, bold=True)
        return [celula]

    def par(rotulo, valor, formato=None):
        a = WriteOnlyCell(resumo, value=rotulo)
        a.font = Font(name="Arial", size=10, bold=True)
        b = WriteOnlyCell(resumo, value=valor)
        b.font = fonte
        if formato:
            b.number_format = formato
        b.alignment = Alignment(wrap_text=True, vertical="top")
        return [a, b]

    def cab(*textos):
        saida = []
        for texto in textos:
            celula = WriteOnlyCell(resumo, value=texto)
            celula.font, celula.fill = negrito, fundo
            saida.append(celula)
        return saida

    pares, linhas_resumo = _resumo_exportacao(simulacao, filtros, registros)
    resumo.append(titulo("Simulador de escolha de valores — exportação"))
    resumo.append([])
    for chave, valor in pares:
        resumo.append(par(chave, valor, "#,##0" if isinstance(valor, int) else None))
    resumo.append([])
    resumo.append(titulo("Casos por regra e status (nesta exportação)"))
    resumo.append(cab("Modelo · regra", "Automática", "Sujeita a visto", "Pendente"))
    for item in linhas_resumo:
        linha = [WriteOnlyCell(resumo, value=item[0])]
        for valor in item[1:]:
            celula = WriteOnlyCell(resumo, value=valor)
            celula.number_format = "#,##0"
            linha.append(celula)
        for celula in linha:
            celula.font = fonte
        resumo.append(linha)
    resumo.append([])
    nota = WriteOnlyCell(resumo, value=NOTA_EXPORTACAO)
    nota.font = Font(name="Arial", size=9, italic=True)
    resumo.append([nota])

    saida = io.BytesIO()
    livro.save(saida)
    return saida.getvalue()

