"""Regras de qualidade: independentes, configuráveis, versionadas e explicáveis.

Cada regra recebe o contexto de um caso (ente × exercício × componente) e
devolve alertas. Um alerta é uma suspeita: nenhuma regra altera, descarta ou
escolhe valores. Os parâmetros iniciais são hipóteses de trabalho.

Convenções de diferença (para uma fonte comparada à referência):
  diferença assinada   = valor da fonte − valor de referência
  diferença absoluta   = |diferença assinada|
  diferença percentual = diferença assinada ÷ |referência|  (não calculável se a referência é zero)

Tolerância: um limite só é "excedido" quando o valor é estritamente maior que
ele (igualdade com o limite não gera alerta). Quando o percentual não é
calculável, a parte relativa é considerada excedida e decide a tolerância absoluta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

from validacao.conferencia.comparacao import Comparacao, FonteCaso, sigla, valor_serie
from validacao.conferencia.dinheiro import percentual, reais, reais_curto

INFO, BAIXA, MEDIA, ALTA = 1, 2, 3, 4
GRAVIDADES = {0: "Nenhuma", INFO: "Informativa", BAIXA: "Baixa", MEDIA: "Média", ALTA: "Alta"}
GRAVIDADE_POR_NOME = {"INFO": INFO, "BAIXA": BAIXA, "MEDIA": MEDIA, "ALTA": ALTA}


@dataclass
class AlertaRascunho:
    regra: str
    gravidade: int
    mensagem: str
    declaracao_a: str | None = None
    declaracao_b: str | None = None
    valor_a: int | None = None
    valor_b: int | None = None
    dif_assinada: int | None = None
    dif_abs: int | None = None
    dif_pct: float | None = None
    evidencias: dict = field(default_factory=dict)


@dataclass
class Contexto:
    id_ente: int
    nome_ente: str
    uf: str
    exercicio: int
    exigivel: bool
    motivo_exigibilidade: str
    principal: str | None  # declaração usada como referência
    precedencia: list[str]
    conceito_ref: str
    fontes: dict[str, FonteCaso]
    comparacoes: dict[str, Comparacao]
    grupos: dict[str, str]
    series: dict[str, dict[int, int | None]]  # declaração -> exercício -> valor
    repeticoes_entes: dict[str, list[int]]  # declaração -> outros entes com o mesmo valor no exercício
    data_corte: object = None
    excluido: str | None = None
    zer_ativa: bool = True
    componente: str = "ISS_MUN"
    extras: dict = field(default_factory=dict)

    @property
    def referencia(self) -> FonteCaso | None:
        return self.fontes.get(self.principal) if self.principal else None

    def valor_referencia(self) -> int | None:
        ref = self.referencia
        return None if ref is None else valor_serie(ref, self.conceito_ref)


# ----------------------------------------------------------------------------
# Catálogo das regras
# ----------------------------------------------------------------------------
REGRAS: dict[str, dict] = {
    "AUS": {
        "nome": "Ausência na fonte principal",
        "tipo": "Ausência",
        "objetivo": "Apontar exercício exigível sem valor na declaração principal do SICONFI.",
        "aplicacao": "Ente existente no exercício; exercício cuja declaração principal já é exigível na data de corte.",
        "formula": "não existe registro vigente com valor (inclusive zero) na declaração principal",
        "parametros": {},
        "gravidade": "ALTA",
        "base": "Registros originais vigentes",
    },
    "INS": {
        "nome": "Validação insuficiente",
        "tipo": "Ausência",
        "objetivo": "Indicar quando não há fontes independentes comparáveis para conferir a referência.",
        "aplicacao": "Casos com valor de referência.",
        "formula": "nº de fontes de outro sistema com comparação possível (plena ou com ressalva) < mínimo",
        "parametros": {"minimo_comparadores": 1},
        "gravidade": "BAIXA",
        "base": "Valores normalizados",
    },
    "DSI": {
        "nome": "Divergência entre declarações do SICONFI",
        "tipo": "Divergência",
        "objetivo": "Comparar declarações do mesmo declarante no SICONFI (DCA, RREO, MSC).",
        "aplicacao": "Quando há mais de uma declaração do SICONFI com valor comparável.",
        "formula": "excede(|fonte − referência|, tolerância absoluta) e/ou excede(|dif| ÷ |referência|, tolerância relativa)",
        "parametros": {"tolerancia_absoluta": 100.0, "tolerancia_relativa": 0.001, "modo": "AMBAS"},
        "gravidade": "ALTA",
        "base": "Valores normalizados",
    },
    "DAX": {
        "nome": "Divergência com base auxiliar",
        "tipo": "Divergência",
        "objetivo": "Comparar a referência com cada base auxiliar comparável (SIOPE, SIOPS, TCE, balanço).",
        "aplicacao": "Pares com parcela comum (comparação plena ou com ressalva).",
        "formula": "excede(|fonte − referência|, tolerância absoluta) e/ou excede(|dif| ÷ |referência|, tolerância relativa); gravidade alta se |dif %| > faixa alta",
        "parametros": {"tolerancia_absoluta": 1000.0, "tolerancia_relativa": 0.01, "modo": "AMBAS", "faixa_alta": 0.20},
        "gravidade": "MEDIA",
        "base": "Valores normalizados",
    },
    "DUP": {
        "nome": "Duplicidade ou versões conflitantes",
        "tipo": "Integridade",
        "objetivo": "Detectar o mesmo campo informado mais de uma vez no mesmo lote.",
        "aplicacao": "Registros marcados na importação como duplicados.",
        "formula": "mesma declaração, ente, exercício e campo repetidos no lote (iguais ou com valores diferentes)",
        "parametros": {},
        "gravidade": "ALTA",
        "base": "Registros originais",
    },
    "ZER": {
        "nome": "Zero contra valor positivo",
        "tipo": "Valor atípico",
        "objetivo": "Apontar zero declarado numa fonte enquanto outra informa valor positivo.",
        "aplicacao": "Todas as declarações do caso.",
        "formula": "uma fonte = 0 e outra > valor mínimo",
        "parametros": {"valor_minimo": 1.0},
        "gravidade": "ALTA",
        "base": "Valores normalizados",
    },
    "NEG": {
        "nome": "Valor negativo",
        "tipo": "Valor atípico",
        "objetivo": "Apontar valor negativo em campo cujo conceito não admite sinal negativo.",
        "aplicacao": "Totais, receita bruta e itens da composição (deduções são excluídas).",
        "formula": "valor < 0",
        "parametros": {},
        "gravidade": "ALTA",
        "base": "Registros originais",
    },
    "ESC": {
        "nome": "Possível erro de escala ou separador",
        "tipo": "Valor atípico",
        "objetivo": "Detectar valores que diferem da referência ou da própria série por potência de 10.",
        "aplicacao": "Pares comparáveis com valores positivos; série com pelo menos 2 outros exercícios.",
        "formula": "razão ≈ 10^k ou 10^−k, k ∈ {1, 2, 3}, com tolerância relativa",
        "parametros": {"tolerancia_razao": 0.03},
        "gravidade": "MEDIA",
        "base": "Valores normalizados",
    },
    "REP": {
        "nome": "Repetição entre exercícios",
        "tipo": "Repetição",
        "objetivo": "Indício a investigar: o mesmo valor, até o centavo, no exercício anterior.",
        "aplicacao": "Declaração principal; valores acima do mínimo.",
        "formula": "valor(N) = valor(N−1) e valor ≥ mínimo",
        "parametros": {"valor_minimo": 1000.0},
        "gravidade": "MEDIA",
        "base": "Registros originais",
    },
    "REE": {
        "nome": "Repetição entre entes",
        "tipo": "Repetição",
        "objetivo": "Indício a investigar: o mesmo valor, até o centavo, em outro ente no mesmo exercício.",
        "aplicacao": "Declaração principal; valores acima do mínimo.",
        "formula": "valor(ente) = valor(outro ente) e valor ≥ mínimo",
        "parametros": {"valor_minimo": 10000.0},
        "gravidade": "BAIXA",
        "base": "Registros originais",
    },
    "VAR": {
        "nome": "Variação atípica na série",
        "tipo": "Série temporal",
        "objetivo": "Sinalizar crescimento ou queda atípicos e picos ou vales isolados. Pode ter explicação econômica.",
        "aplicacao": "Declaração principal com exercícios vizinhos positivos.",
        "formula": "razão anual > crescimento máximo ou < 1 ÷ crescimento máximo; ou valor ÷ média dos vizinhos > fator de pico (com vizinhos próximos entre si)",
        "parametros": {"crescimento_maximo": 3.0, "fator_pico": 2.0, "vizinhos_max": 1.5, "tolerancia_corroboracao": 0.15},
        "gravidade": "MEDIA",
        "base": "Valores normalizados",
    },
    "TOT": {
        "nome": "Total incompatível com componentes",
        "tipo": "Integridade",
        "objetivo": "Conferir o total declarado contra a soma das parcelas conhecidas da mesma declaração.",
        "aplicacao": "Declarações com composição conhecida (DCA: bruto + adicional − deduções; SIOPE e TCE: principal + multas + dívida ativa + adicional). Em base auxiliar a gravidade máxima é baixa.",
        "formula": "excede(|total − composição|, tolerância absoluta) e excede(relativa)",
        "parametros": {"tolerancia_absoluta": 1.0, "tolerancia_relativa": 0.0001, "modo": "AMBAS"},
        "gravidade": "MEDIA",
        "base": "Registros originais",
    },
    "RET": {
        "nome": "Retificação ou informação após a data de corte",
        "tipo": "Versão",
        "objetivo": "Indicar que o valor vigente substituiu outro ou foi obtido após a data de corte.",
        "aplicacao": "Registros vigentes com versão > 1 ou data de extração posterior à data de corte.",
        "formula": "versão > 1 ou data_extração > data de corte",
        "parametros": {},
        "gravidade": "MEDIA",
        "base": "Registros originais",
    },
    "NCP": {
        "nome": "Não comparável diretamente",
        "tipo": "Comparabilidade",
        "objetivo": "Registrar fontes que informam outra grandeza (conceito ou abrangência).",
        "aplicacao": "Fontes com valor, mas sem parcela comum com a referência.",
        "formula": "conceitos disponíveis na fonte ∩ conceitos da referência = ∅",
        "parametros": {},
        "gravidade": "INFO",
        "base": "Mapeamentos",
    },
    "FLF": {
        "nome": "Fonte líquida de FUNDEB",
        "tipo": "Comparabilidade",
        "objetivo": "Identificar bases que publicam o valor já deduzido do FUNDEB (diferença de conceito, não de valor).",
        "aplicacao": "Pares em que a fonte diverge da referência mas coincide com a referência menos o FUNDEB.",
        "formula": "|fonte − (referência − FUNDEB)| ≤ 0,1% e |fonte − referência| > 0,1%",
        "parametros": {},
        "gravidade": "INFO",
        "base": "Valores normalizados",
    },
    "UFC": {
        "nome": "Cota-parte repassada × soma das cotas municipais",
        "tipo": "Conciliação",
        "objetivo": "Conferir a cota-parte lançada pelo estado contra a soma das cotas-partes recebidas pelos municípios da UF.",
        "aplicacao": "ICMS estadual, quando há cota repassada na DCA do estado e cotas municipais no exercício.",
        "formula": "excede(|repasse estadual − Σ cotas municipais|, tolerâncias); referência informativa: repasse ÷ ICMS ≈ 25%",
        "parametros": {"tolerancia_absoluta": 1000.0, "tolerancia_relativa": 0.05, "modo": "AMBAS"},
        "gravidade": "MEDIA",
        "base": "Valores principais dos casos de cota-parte e registros da DCA do estado",
    },
    "PAR": {
        "nome": "Período incompleto ou declaração não exigível",
        "tipo": "Comparabilidade",
        "objetivo": "Diferenciar exercício em curso, período parcial e declaração ainda não exigível de ausência.",
        "aplicacao": "Exercício não exigível na data de corte ou registros de período parcial.",
        "formula": "data de corte < prazo da declaração principal, ou período ≠ ANUAL",
        "parametros": {},
        "gravidade": "INFO",
        "base": "Calendário e registros",
    },
}

ORDEM = list(REGRAS)


def parametros_iniciais() -> dict:
    return {
        codigo: {
            "ativa": True,
            "gravidade": definicao["gravidade"],
            "parametros": dict(definicao["parametros"]),
            "status": "HIPOTESE",
        }
        for codigo, definicao in REGRAS.items()
    }


def _reais_para_centavos(valor: float) -> int:
    return int(round(float(valor) * 100))


def excede(absoluta: int, pct: float | None, tol_abs_reais: float, tol_rel: float, modo: str) -> bool:
    excede_abs = absoluta > _reais_para_centavos(tol_abs_reais)
    excede_rel = True if pct is None else abs(pct) > float(tol_rel)
    if modo == "QUALQUER":
        return excede_abs or excede_rel
    return excede_abs and excede_rel


def texto_tolerancia(params: dict) -> str:
    modo = "ambos precisam ser excedidos" if params.get("modo", "AMBAS") == "AMBAS" else "basta exceder um deles"
    return (
        f"limites: {percentual(params['tolerancia_relativa'], 2)} e "
        f"{reais(_reais_para_centavos(params['tolerancia_absoluta']))} ({modo})"
    )


# ----------------------------------------------------------------------------
# Avaliação
# ----------------------------------------------------------------------------

def avaliar(ctx: Contexto, configuracao: dict) -> list[AlertaRascunho]:
    alertas: list[AlertaRascunho] = []
    if ctx.excluido:
        return alertas
    ctx.zer_ativa = configuracao.get("ZER", {}).get("ativa", True)
    for codigo in ORDEM:
        ajuste = configuracao.get(codigo, {})
        if not ajuste.get("ativa", True):
            continue
        params = {**REGRAS[codigo]["parametros"], **ajuste.get("parametros", {})}
        gravidade = GRAVIDADE_POR_NOME.get(ajuste.get("gravidade", REGRAS[codigo]["gravidade"]), MEDIA)
        funcao = _FUNCOES[codigo]
        for alerta in funcao(ctx, params, gravidade):
            alerta.evidencias.setdefault("parametros", params)
            alertas.append(alerta)
    return alertas


def _aus(ctx: Contexto, params, gravidade):
    if not ctx.exigivel:
        return []
    ref = ctx.referencia
    if ref is not None and ref.medidas:
        return []
    disponiveis = {
        d: valor_serie(f, ctx.conceito_ref) for d, f in ctx.fontes.items() if f.medidas and d != ctx.principal
    }
    principal = sigla(ctx.precedencia[0]) if ctx.precedencia else "fonte principal"
    if disponiveis:
        lista = "; ".join(f"{sigla(d)} {reais_curto(v)}" for d, v in disponiveis.items())
        texto = f"A {principal} não informa valor para {ctx.exercicio}. Há valores em outras fontes: {lista}. A escolha de outro valor depende da metodologia aplicável."
    else:
        texto = f"A {principal} não informa valor para {ctx.exercicio}, e nenhuma outra fonte carregada informa."
    estado = "sem registro"
    if ref is not None and ref.total is not None:
        estado = {"AUSENTE": "célula vazia", "INVALIDO": "formato inválido"}.get(ref.total.estado, ref.total.estado.lower())
        texto += f" Situação do registro: {estado}."
    return [AlertaRascunho("AUS", gravidade, texto, declaracao_a=ctx.precedencia[0] if ctx.precedencia else None,
                           evidencias={"disponiveis": disponiveis, "estado": estado})]


def _independentes_comparaveis(ctx: Contexto) -> list[str]:
    grupo_ref = ctx.grupos.get(ctx.principal or "", "")
    return [
        d for d, c in ctx.comparacoes.items()
        if c.situacao in ("SIM", "RESSALVA") and ctx.grupos.get(d) != grupo_ref
    ]


def _ins(ctx: Contexto, params, gravidade):
    if ctx.valor_referencia() is None:
        return []
    comparaveis = _independentes_comparaveis(ctx)
    minimo = int(params["minimo_comparadores"])
    if len(comparaveis) >= minimo:
        return []
    nao_comp = [sigla(d) for d, c in ctx.comparacoes.items() if c.situacao == "NAO"]
    texto = (
        f"Sem validação suficiente: {len(comparaveis)} fonte(s) independente(s) comparável(is), mínimo configurado {minimo}."
    )
    if nao_comp:
        texto += f" Há valor em {', '.join(nao_comp)}, mas sem parcela comum com a referência."
    return [AlertaRascunho("INS", gravidade, texto, declaracao_a=ctx.principal, evidencias={"comparaveis": comparaveis})]


def _divergencias(ctx: Contexto, params, gravidade, codigo, mesmo_grupo: bool):
    alertas = []
    grupo_ref = ctx.grupos.get(ctx.principal or "", "")
    for declaracao, comp in ctx.comparacoes.items():
        if comp.situacao not in ("SIM", "RESSALVA") or comp.absoluta is None:
            continue
        if comp.valor_ref == 0 and comp.valor_fonte and _ativa_zer(ctx):
            continue  # referência zero contra valor positivo é tratada pela regra ZER
        if (ctx.grupos.get(declaracao) == grupo_ref) != mesmo_grupo:
            continue
        if not excede(comp.absoluta, comp.percentual, params["tolerancia_absoluta"], params["tolerancia_relativa"], params.get("modo", "AMBAS")):
            continue
        g = gravidade
        if codigo == "DAX":
            faixa = float(params.get("faixa_alta", 0.2))
            g = ALTA if (comp.percentual is None or abs(comp.percentual) > faixa) else gravidade
        fonte, ref = sigla(declaracao), sigla(ctx.principal)
        pct = "o percentual não é calculável porque a referência é zero" if comp.percentual is None else (
            f"equivalente a {percentual(abs(comp.percentual))} da referência"
        )
        direcao = "acima" if (comp.assinada or 0) > 0 else "abaixo"
        texto = (
            f"{_artigo(declaracao).capitalize()} {fonte} informa {reais_curto(comp.valor_fonte)} e o valor de referência ({ref}) é {reais_curto(comp.valor_ref)}. "
            f"A diferença é de {reais_curto(comp.absoluta)} {direcao} da referência, {pct}. "
            f"Comparação na parcela: {comp.parcela}. Excede os {texto_tolerancia(params)}."
        )
        if comp.situacao == "RESSALVA":
            texto += f" Ressalva: {comp.motivo}"
        concordam = [
            sigla(d) for d, c in ctx.comparacoes.items()
            if d != declaracao and c.situacao in ("SIM", "RESSALVA") and c.absoluta is not None
            and not excede(c.absoluta, c.percentual, params["tolerancia_absoluta"], params["tolerancia_relativa"], params.get("modo", "AMBAS"))
        ]
        if concordam:
            texto += f" Concordam com a referência dentro dos limites: {', '.join(concordam)}."
        alertas.append(
            AlertaRascunho(
                codigo, g, texto, declaracao_a=ctx.principal, declaracao_b=declaracao,
                valor_a=comp.valor_ref, valor_b=comp.valor_fonte, dif_assinada=comp.assinada,
                dif_abs=comp.absoluta, dif_pct=comp.percentual,
                evidencias={"conceito": comp.conceito, "ressalva": comp.situacao == "RESSALVA", "concordam": concordam,
                            "derivacao_fonte": comp.derivacao_fonte, "derivacao_ref": comp.derivacao_ref},
            )
        )
    return alertas


def _ativa_zer(ctx) -> bool:
    return getattr(ctx, "zer_ativa", True)


def _artigo(declaracao: str | None) -> str:
    return "a" if declaracao in ("SICONFI_DCA", "SICONFI_MSC", "INFO_UF") else "o"


def _dsi(ctx, params, gravidade):
    return _divergencias(ctx, params, gravidade, "DSI", mesmo_grupo=True)


def _dax(ctx, params, gravidade):
    return _divergencias(ctx, params, gravidade, "DAX", mesmo_grupo=False)


def _dup(ctx, params, gravidade):
    alertas = []
    for declaracao, fonte in ctx.fontes.items():
        conflitos = [r for r in fonte.registros if r.duplicado == "CONFLITANTE"]
        iguais = [r for r in fonte.registros if r.duplicado == "IGUAL"]
        if conflitos:
            campos = ", ".join(sorted({r.campo for r in conflitos}))
            alertas.append(AlertaRascunho("DUP", gravidade, f"{sigla(declaracao)}: o campo {campos} aparece mais de uma vez no mesmo lote com valores diferentes. Vale o primeiro registro lido; os demais estão preservados.", declaracao_a=declaracao, evidencias={"registros": [r.id for r in conflitos]}))
        elif iguais:
            alertas.append(AlertaRascunho("DUP", INFO, f"{sigla(declaracao)}: registro repetido no mesmo lote com valor idêntico.", declaracao_a=declaracao, evidencias={"registros": [r.id for r in iguais]}))
    return alertas


def _zer(ctx, params, gravidade):
    minimo = _reais_para_centavos(params["valor_minimo"])
    zeros = {d: f for d, f in ctx.fontes.items() if f.total is not None and f.total.estado == "ZERO"}
    if not zeros:
        return []
    positivos = {d: valor_serie(f, ctx.conceito_ref) for d, f in ctx.fontes.items() if d not in zeros}
    positivos = {d: v for d, v in positivos.items() if v is not None and v > minimo}
    if not positivos:
        return []
    alertas = []
    for d_zero in zeros:
        maior = max(positivos.items(), key=lambda item: item[1])
        lista = "; ".join(f"{sigla(d)} {reais_curto(v)}" for d, v in positivos.items())
        if d_zero == ctx.principal:
            texto = (f"A referência ({sigla(d_zero)}) declara zero, mas {lista}. A diferença em relação a {sigla(maior[0])} é de "
                     f"{reais_curto(maior[1])}; o percentual não é calculável porque a referência é zero.")
            alertas.append(AlertaRascunho("ZER", gravidade, texto, declaracao_a=d_zero, declaracao_b=maior[0], valor_a=0,
                                          valor_b=maior[1], dif_assinada=maior[1], dif_abs=maior[1], dif_pct=None,
                                          evidencias={"positivos": positivos}))
        else:
            ref = ctx.valor_referencia()
            texto = f"{sigla(d_zero)} declara zero enquanto {lista}."
            alertas.append(AlertaRascunho("ZER", gravidade, texto, declaracao_a=ctx.principal, declaracao_b=d_zero, valor_a=ref,
                                          valor_b=0, dif_assinada=None if ref is None else -ref,
                                          dif_abs=None if ref is None else abs(ref), dif_pct=None if not ref else -1.0,
                                          evidencias={"positivos": positivos}))
    return alertas


def _neg(ctx, params, gravidade):
    alertas = []
    for declaracao, fonte in ctx.fontes.items():
        for r in fonte.registros:
            if not r.vigente or r.valor is None or r.valor >= 0:
                continue
            if r.conceito in ("DEDUCAO", "INFORMATIVO"):
                continue
            alertas.append(AlertaRascunho("NEG", gravidade, f"{sigla(declaracao)} informa valor negativo ({reais(r.valor)}) no campo {r.campo}, cujo conceito não admite sinal negativo.", declaracao_a=declaracao, valor_a=r.valor, evidencias={"registro": r.id, "campo": r.campo}))
    return alertas


def _milhar(numero: int) -> str:
    return f"{numero:,}".replace(",", ".")


def _potencia(razao: float, tolerancia: float) -> int | None:
    if razao <= 0:
        return None
    for k in (1, 2, 3):
        for alvo in (10 ** k, 10 ** -k):
            if abs(razao / alvo - 1) <= tolerancia:
                return k if alvo > 1 else -k
    return None


def _esc(ctx, params, gravidade):
    tol = float(params["tolerancia_razao"])
    alertas = []
    for declaracao, comp in ctx.comparacoes.items():
        if comp.situacao not in ("SIM", "RESSALVA") or not comp.valor_ref or not comp.valor_fonte:
            continue
        if comp.valor_ref <= 0 or comp.valor_fonte <= 0:
            continue
        k = _potencia(comp.valor_fonte / comp.valor_ref, tol)
        if k is None:
            continue
        fator = 10 ** abs(k)
        sentido = "maior" if k > 0 else "menor"
        texto = (f"{sigla(declaracao)} ({reais_curto(comp.valor_fonte)}) é cerca de {_milhar(fator)} vezes {sentido} "
                 f"que a referência ({reais_curto(comp.valor_ref)}). Possível erro de escala ou de separador decimal em uma das fontes.")
        alertas.append(AlertaRascunho("ESC", gravidade, texto, declaracao_a=ctx.principal, declaracao_b=declaracao,
                                      valor_a=comp.valor_ref, valor_b=comp.valor_fonte, dif_assinada=comp.assinada,
                                      dif_abs=comp.absoluta, dif_pct=comp.percentual, evidencias={"potencia": k}))
    serie = ctx.series.get(ctx.principal or "", {})
    atual = serie.get(ctx.exercicio)
    outros = [v for ano, v in serie.items() if ano != ctx.exercicio and v and v > 0]
    if atual and atual > 0 and len(outros) >= 2:
        mediana = median(outros)
        k = _potencia(atual / mediana, tol)
        if k is not None:
            fator = 10 ** abs(k)
            sentido = "maior" if k > 0 else "menor"
            texto = (f"O valor de {ctx.exercicio} na {sigla(ctx.principal)} ({reais_curto(atual)}) é cerca de {_milhar(fator)} vezes "
                     f"{sentido} que a mediana dos demais exercícios ({reais_curto(int(mediana))}). Possível erro de escala.")
            alertas.append(AlertaRascunho("ESC", gravidade, texto, declaracao_a=ctx.principal, valor_a=atual,
                                          evidencias={"potencia": k, "mediana_serie": int(mediana)}))
    return alertas


def _rep(ctx, params, gravidade):
    if not ctx.principal:
        return []
    serie = ctx.series.get(ctx.principal, {})
    atual = serie.get(ctx.exercicio)
    anterior = serie.get(ctx.exercicio - 1)
    minimo = _reais_para_centavos(params["valor_minimo"])
    if atual is None or anterior is None or atual != anterior or abs(atual) < minimo:
        return []
    texto = (f"A {sigla(ctx.principal)} informa exatamente o mesmo valor em {ctx.exercicio - 1} e {ctx.exercicio} "
             f"({reais(atual)}). Repetição até o centavo é indício a investigar (possível relatório de outro exercício).")
    return [AlertaRascunho("REP", gravidade, texto, declaracao_a=ctx.principal, valor_a=atual, valor_b=anterior,
                           evidencias={"exercicio_repetido": ctx.exercicio - 1})]


def _ree(ctx, params, gravidade):
    if not ctx.principal:
        return []
    outros = ctx.repeticoes_entes.get(ctx.principal) or []
    atual = ctx.series.get(ctx.principal, {}).get(ctx.exercicio)
    if not outros or atual is None or abs(atual) < _reais_para_centavos(params["valor_minimo"]):
        return []
    lista = ", ".join(str(o) for o in outros[:5]) + ("…" if len(outros) > 5 else "")
    texto = (f"O valor da {sigla(ctx.principal)} ({reais(atual)}) é idêntico, até o centavo, ao de {len(outros)} outro(s) ente(s) "
             f"no mesmo exercício (IBGE {lista}). Indício a investigar.")
    return [AlertaRascunho("REE", gravidade, texto, declaracao_a=ctx.principal, valor_a=atual, evidencias={"entes": outros})]


def _razao(serie: dict[int, int | None], ano: int, anterior: int) -> float | None:
    a, b = serie.get(ano), serie.get(anterior)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return a / b


def _var(ctx, params, gravidade):
    if not ctx.principal:
        return []
    serie = ctx.series.get(ctx.principal, {})
    atual = serie.get(ctx.exercicio)
    if atual is None or atual <= 0:
        return []
    maximo = float(params["crescimento_maximo"])
    fator = float(params["fator_pico"])
    vizinhos_max = float(params["vizinhos_max"])
    tol = float(params["tolerancia_corroboracao"])
    achados = []
    razao = _razao(serie, ctx.exercicio, ctx.exercicio - 1)
    if razao is not None and (razao > maximo or razao < 1 / maximo):
        movimento = "crescimento" if razao > 1 else "queda"
        achados.append(("anual", razao, f"{movimento} de {percentual(abs(razao - 1), 0)} em relação a {ctx.exercicio - 1} "
                                         f"({reais_curto(serie[ctx.exercicio - 1])} → {reais_curto(atual)})"))
    antes, depois = serie.get(ctx.exercicio - 1), serie.get(ctx.exercicio + 1)
    if antes and depois and antes > 0 and depois > 0 and max(antes, depois) / min(antes, depois) <= vizinhos_max:
        media = (antes + depois) / 2
        r = atual / media
        if r > fator or r < 1 / fator:
            forma = "pico" if r > 1 else "vale"
            achados.append(("pico", r, f"{forma} isolado: {reais_curto(atual)} contra {reais_curto(antes)} em {ctx.exercicio - 1} "
                                      f"e {reais_curto(depois)} em {ctx.exercicio + 1}"))
    if not achados:
        return []
    alertas = []
    for tipo, razao_principal, descricao in achados:
        corroboram = []
        for declaracao, outra in ctx.series.items():
            if declaracao == ctx.principal or ctx.comparacoes.get(declaracao) is None:
                continue
            if ctx.comparacoes[declaracao].situacao not in ("SIM", "RESSALVA"):
                continue
            if tipo == "anual":
                r = _razao(outra, ctx.exercicio, ctx.exercicio - 1)
            else:
                a, b, c = outra.get(ctx.exercicio - 1), outra.get(ctx.exercicio), outra.get(ctx.exercicio + 1)
                r = None if not (a and b and c and a > 0 and c > 0) else b / ((a + c) / 2)
            if r is not None and abs(r / razao_principal - 1) <= tol:
                corroboram.append(sigla(declaracao))
        g = BAIXA if corroboram else gravidade
        texto = f"Na {sigla(ctx.principal)}: {descricao}."
        if corroboram:
            texto += f" O movimento é corroborado por {', '.join(corroboram)}; os valores são preservados e a variação fica sinalizada para análise."
        else:
            texto += " Nenhuma fonte comparável apresenta o mesmo movimento."
        alertas.append(AlertaRascunho("VAR", g, texto, declaracao_a=ctx.principal, valor_a=atual,
                                      evidencias={"tipo": tipo, "razao": razao_principal, "corroboram": corroboram}))
    return alertas


def _tot(ctx, params, gravidade):
    alertas = []
    for declaracao, fonte in ctx.fontes.items():
        if fonte.total is None or fonte.total.valor is None or fonte.composicao is None:
            continue
        if fonte.composicao_conceito and fonte.total.conceito not in (fonte.composicao_conceito, "INDEFINIDO"):
            continue
        dif = fonte.total.valor - fonte.composicao
        pct = None if fonte.composicao == 0 else dif / abs(fonte.composicao)
        if not excede(abs(dif), pct, params["tolerancia_absoluta"], params["tolerancia_relativa"], params.get("modo", "AMBAS")):
            continue
        g = gravidade if declaracao == ctx.principal else min(gravidade, BAIXA)
        texto = (f"{sigla(declaracao)}: o total declarado ({reais(fonte.total.valor)}) difere da soma das parcelas "
                 f"({fonte.composicao_texto} = {reais(fonte.composicao)}) em {reais(abs(dif))}.")
        if declaracao != ctx.principal:
            texto += " Inconsistência interna da base auxiliar; não afeta diretamente a referência."
        alertas.append(AlertaRascunho("TOT", g, texto, declaracao_a=declaracao, valor_a=fonte.total.valor,
                                      valor_b=fonte.composicao, dif_assinada=dif, dif_abs=abs(dif), dif_pct=pct,
                                      evidencias={"composicao": fonte.composicao_texto}))
    return alertas


def _ret(ctx, params, gravidade):
    alertas = []
    for declaracao, fonte in ctx.fontes.items():
        total = fonte.total
        if total is None:
            continue
        anteriores = [r for r in fonte.registros if not r.vigente and r.campo == total.campo]
        if total.versao > 1:
            anterior = max(anteriores, key=lambda r: r.versao) if anteriores else None
            texto = f"{sigla(declaracao)}: o valor vigente é a versão {total.versao}"
            if anterior is not None:
                texto += f"; a versão {anterior.versao} informava {reais(anterior.valor)} (diferença de {reais((total.valor or 0) - (anterior.valor or 0))})"
            texto += "."
            alertas.append(AlertaRascunho("RET", gravidade, texto, declaracao_a=declaracao, valor_a=total.valor,
                                          valor_b=None if anterior is None else anterior.valor,
                                          evidencias={"versao": total.versao, "registro_anterior": None if anterior is None else anterior.id}))
        if ctx.data_corte is not None and total.data_extracao is not None and total.data_extracao > ctx.data_corte:
            alertas.append(AlertaRascunho("RET", gravidade, f"{sigla(declaracao)}: informação extraída em {total.data_extracao:%d/%m/%Y}, depois da data de corte ({ctx.data_corte:%d/%m/%Y}).", declaracao_a=declaracao))
    return alertas


def _ncp(ctx, params, gravidade):
    return [
        AlertaRascunho("NCP", gravidade, f"{sigla(d)} — não comparável diretamente: {c.motivo}", declaracao_a=ctx.principal,
                       declaracao_b=d, valor_a=c.valor_ref, valor_b=c.valor_fonte)
        for d, c in ctx.comparacoes.items() if c.situacao == "NAO"
    ]


def _flf(ctx, params, gravidade):
    return [
        AlertaRascunho("FLF", gravidade, f"{sigla(d)}: {c.motivo}", declaracao_a=ctx.principal, declaracao_b=d,
                       valor_a=c.valor_ref, valor_b=c.valor_fonte, dif_assinada=c.assinada, dif_abs=c.absoluta, dif_pct=c.percentual)
        for d, c in ctx.comparacoes.items() if c.inferencia == "FUNDEB"
    ]


def _ufc(ctx, params, gravidade):
    if ctx.componente != "ICMS_UF" or ctx.principal is None:
        return []
    soma = ctx.extras.get("cota_municipal_uf")
    fonte = ctx.fontes.get(ctx.principal)
    if soma is None or fonte is None:
        return []
    repasse = None
    origem = ""
    for registro in fonte.registros:
        if registro.vigente and registro.parcela == "COTA_REPASSADA" and registro.valor is not None:
            repasse, origem = abs(registro.valor), "cota repassada lançada"
            break
    if repasse is None:
        for registro in fonte.registros:
            if registro.vigente and registro.parcela == "DEDUCAO_CONST" and registro.valor is not None:
                repasse, origem = abs(registro.valor), "dedução constitucional do ICMS"
                break
    if repasse is None:
        return []
    dif = repasse - soma
    pct = None if soma == 0 else dif / abs(soma)
    if not excede(abs(dif), pct, params["tolerancia_absoluta"], params["tolerancia_relativa"], params.get("modo", "AMBAS")):
        return []
    icms = ctx.valor_referencia()
    proporcao = "" if not icms else f" O repasse equivale a {percentual(repasse / icms)} do ICMS declarado (referência legal: 25%)."
    texto = (
        f"A {origem} pelo estado na {sigla(ctx.principal)} é {reais_curto(repasse)}, e a soma das cotas-partes declaradas pelos "
        f"{ctx.extras.get('municipios_uf', 0)} municípios da UF é {reais_curto(soma)}. Diferença de {reais_curto(abs(dif))} "
        f"({percentual(abs(pct)) if pct is not None else 'percentual não calculável'}).{proporcao}"
    )
    return [AlertaRascunho("UFC", gravidade, texto, declaracao_a=ctx.principal, valor_a=repasse, valor_b=soma,
                           dif_assinada=dif, dif_abs=abs(dif), dif_pct=pct,
                           evidencias={"soma_municipal": soma, "municipios": ctx.extras.get("municipios_uf", 0), "origem": origem})]


def _par(ctx, params, gravidade):
    alertas = []
    if not ctx.exigivel:
        alertas.append(AlertaRascunho("PAR", gravidade, ctx.motivo_exigibilidade, declaracao_a=ctx.precedencia[0] if ctx.precedencia else None))
    for declaracao, fonte in ctx.fontes.items():
        parciais = [r for r in fonte.registros if r.vigente and r.periodo != "ANUAL"]
        if parciais:
            alertas.append(AlertaRascunho("PAR", gravidade, f"{sigla(declaracao)} informa período parcial ({parciais[0].periodo.lower()}); não é comparado com valores anuais.", declaracao_a=declaracao))
    return alertas


_FUNCOES = {
    "AUS": _aus, "INS": _ins, "DSI": _dsi, "DAX": _dax, "DUP": _dup, "ZER": _zer, "NEG": _neg, "ESC": _esc,
    "REP": _rep, "REE": _ree, "VAR": _var, "TOT": _tot, "RET": _ret, "NCP": _ncp, "FLF": _flf, "UFC": _ufc, "PAR": _par,
}


def diagnostico(ctx: Contexto, alertas: list[AlertaRascunho]) -> str:
    if ctx.excluido:
        return "EXCLUIDO"
    if not ctx.exigivel and (ctx.referencia is None or not ctx.referencia.medidas):
        return "NAO_EXIGIVEL"
    relevantes = [a for a in alertas if a.gravidade >= BAIXA and a.regra != "INS"]
    if relevantes:
        return "ALERTA"
    if any(a.regra == "INS" for a in alertas):
        return "SEM_VALIDACAO"
    return "CONFERIDO"


def prioridade(alertas: list[AlertaRascunho], maior_dif_abs: int | None) -> float:
    """Gravidade máxima primeiro; depois a magnitude da maior diferença (log10 em reais)."""
    gravidade = max((a.gravidade for a in alertas if a.regra not in ("NCP", "FLF")), default=0)
    magnitude = 0.0 if not maior_dif_abs else math.log10(1 + maior_dif_abs / 100)
    return round(gravidade * 100 + magnitude * 5, 4)
