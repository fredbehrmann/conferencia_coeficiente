"""Modelo 3 · Funil de validação externa.

Reproduz a metodologia da Nota Técnica "Metodologia dos Dados" (v1.24, 26-11-11) e das minutas de
cota-parte do ICMS (v1.00) e de ISS (v1.01) do Grupo dos Coeficientes, com as bases que a aplicação
possui. Cada caso percorre as etapas em ordem; a primeira cujo critério é atendido fixa o valor:

* Etapa 1a · DCA validada: a DCA coincide (± R$ 10,00) com a fonte externa (tribunal de contas).
* Etapa 1b · DCA corrigida: a DCA só coincide depois de corrigido um erro identificável; adota-se
  a fonte externa.
* Etapa 2a · demonstrativo validado: RREO, SIOPS ou SIOPE (nessa ordem) coincide com a fonte externa;
  adota-se o demonstrativo.
* Etapa 2b · demonstrativo corrigido: o demonstrativo coincide depois de corrigido; adota-se a fonte
  externa.
* Etapa 3a · demonstrativos concordantes: DCA igual a dois demonstrativos que coincidem entre si (sem
  DCA, dois demonstrativos iguais); adota-se a DCA (sem DCA, o primeiro do par).
* Etapa 3b · concordância após correção de um dos valores; adota-se o valor não afetado pelo erro.
* Etapa 4 · fonte externa residual.
* Pendente: nenhum critério se aplica.

Fontes de validação (minutas, art. X+1):

* cota-parte: Sefaz (1ª extração, coluna "inf Estados"), Banco do Brasil e tribunal de contas (pelo bruto), nessa
  ordem de precedência quando mais de uma coincide (Etapas 1b e 2b) e na fonte residual (Etapa 4). A ordem foi
  definida pelo grupo em 19/11/2026 e inverte a das minutas v1.00 (art. X+3, § 3º, e art. X+6) e da Nota Técnica
  v1.24. A 2ª extração das Sefaz não é usada;
* ISS: só o tribunal de contas.

Pontos da nota aplicados:

* o grupo do tribunal publicado líquido do Fundeb (UF × exercício × natureza do valor) é detectado pela razão
  mediana tribunal ÷ Sefaz entre 0,79 e 0,83; o grupo sem nenhum valor da Sefaz usa o SIOPS;
* compensação financeira da LC 194/2022 registrada pelo Município na DCA (COTA_COMPEN_FIN), quando diferente
  de zero: E4b (valor + compensação = fonte; adota-se o valor, que exclui a compensação) nas Etapas 1b, 2b e 3b;
  E4a (valor − compensação) só na 3b, como na nota;
* verificações (marcam o caso sem alterar o valor): cota-parte V1, V2, V3, V4 (sem marcar a duplicidade que a
  Sefaz também informa), V5 e V6, contra a Sefaz; ISS V1 e V6 contra o tribunal, V4, V5 e V7.

Ainda não aplicado: a dedução do art. X+7 (falta a informação, pelos Estados, da compensação transferida).
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations

TOL_FUNIL = 1_000  # R$ 10,00, em centavos
DEMONSTRATIVOS = ("SICONFI_RREO", "SIOPS", "SIOPE")
EXTERNAS = {"COTA_ICMS_MUN": ("SEFAZ", "BB", "TCE")}  # precedência definida pelo grupo; demais componentes: só o tribunal
EXTERNAS_PADRAO = ("TCE",)
FAIXA_TCE_LIQUIDO = (0.79, 0.83)
FATOR_FUNDEB = Decimal("0.8")
FATOR_FUNDEB_SOMADO = Decimal("1.2")
FATORES_UNIDADE = (10, 100, 1_000)
COMPONENTES_ISS_TCE = ("PRINCIPAL", "MULTAS_JUROS", "DA_PRINCIPAL", "DA_MULTAS")
ROTULO_COMPONENTE_TCE = {"PRINCIPAL": "principal", "MULTAS_JUROS": "multas e juros",
                         "DA_PRINCIPAL": "dívida ativa", "DA_MULTAS": "multas e juros da dívida ativa"}
DUPLICIDADE_VALOR_MINIMO = 1_000_000  # R$ 10 mil
SIGLA = {"SICONFI_DCA": "DCA", "SICONFI_RREO": "RREO", "SIOPS": "SIOPS", "SIOPE": "SIOPE", "TCE": "TCE", "BB": "BB",
         "SEFAZ": "Sefaz"}
LIMITE_VERIFICACAO = 0.10   # V1, V2, V3 e V6: diferença ou desvio acima de 10%
MINIMO_EXERCICIOS_TIPICA = 3  # razão típica (V3, V6) e mediana da série (V7): ao menos três exercícios
FATOR_SALTO_V7 = 2

ROTULO_ETAPA = {
    "F1A": "Etapa 1a · DCA validada pela fonte externa",
    "F1B": "Etapa 1b · DCA corrigida",
    "F2A": "Etapa 2a · demonstrativo do ente validado",
    "F2B": "Etapa 2b · demonstrativo corrigido",
    "F3A": "Etapa 3a · demonstrativos concordantes",
    "F3B": "Etapa 3b · concordância após correção",
    "F4": "Etapa 4 · fonte externa (residual)",
    "FP": "Pendente",
}
CONFIRMACAO_EXTERNA = {"F1A", "F1B", "F2A", "F2B"}


@dataclass
class ContextoFunil:
    # (id_ente, exercício, componente) -> {parcela: centavos} do tribunal de contas (ISS)
    parcelas_tce: dict = field(default_factory=dict)
    # (uf, exercício, natureza) -> razão mediana tribunal ÷ Sefaz (ou ÷ SIOPS), para os grupos convertidos ao bruto
    grupos_tce_liquido: dict = field(default_factory=dict)
    # (uf, exercício, natureza) -> base da razão: "Sefaz" ou "SIOPS"
    base_grupo_tce: dict = field(default_factory=dict)
    # (id_ente, exercício) -> compensação da LC 194 registrada pelo Município na DCA (centavos, ≠ 0)
    compensacoes: dict = field(default_factory=dict)


def _centavos(valor: Decimal) -> int:
    return int(valor.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _coincide(a: int, b: int, tolerancia: int = TOL_FUNIL) -> bool:
    return abs(a - b) <= tolerancia


def _reais(centavos: int) -> str:
    return "R$ " + f"{centavos / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _registrado(item: dict | None) -> int | None:
    """Valor tal como registrado; vazio ou zero não valida (nota, item 3.1)."""
    if not item or item.get("estado") != "VALOR" or item.get("original") is None:
        return None
    valor = int(item["original"])
    return valor if valor > 0 else None


def _natureza_tce(item: dict) -> str:
    return str(item.get("conceito") or "INDEFINIDO")


def montar_contexto(casos, parcelas_tce: dict, compensacoes: dict | None = None) -> ContextoFunil:
    """Grupos do tribunal com cota-parte publicada líquida do Fundeb, componentes do ISS no tribunal e
    compensações da LC 194 registradas na DCA.

    O grupo é líquido quando a razão mediana tribunal ÷ Sefaz fica entre 0,79 e 0,83 (Nota Técnica: a Sefaz, e
    não a DCA, para não depender da base validada). Grupo sem nenhum valor da Sefaz usa o SIOPS."""
    razoes: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: {"Sefaz": [], "SIOPS": []})
    for caso in casos:
        if caso.componente != "COTA_ICMS_MUN":
            continue
        tce = _registrado(caso.valores.get("TCE"))
        if not tce:
            continue
        chave = (caso.uf, caso.exercicio, _natureza_tce(caso.valores["TCE"]))
        sefaz = _registrado(caso.valores.get("SEFAZ"))
        siops = _registrado(caso.valores.get("SIOPS"))
        if sefaz:
            razoes[chave]["Sefaz"].append(tce / sefaz)
        if siops:
            razoes[chave]["SIOPS"].append(tce / siops)
    grupos, bases = {}, {}
    for chave, por_base in razoes.items():
        base = "Sefaz" if por_base["Sefaz"] else "SIOPS"
        if not por_base[base]:
            continue
        mediana = statistics.median(por_base[base])
        if FAIXA_TCE_LIQUIDO[0] <= mediana <= FAIXA_TCE_LIQUIDO[1]:
            grupos[chave] = mediana
            bases[chave] = base
    compensacoes = {k: int(v) for k, v in (compensacoes or {}).items() if v}
    return ContextoFunil(parcelas_tce=parcelas_tce, grupos_tce_liquido=grupos, base_grupo_tce=bases,
                         compensacoes=compensacoes)


def _externas(caso, contexto: ContextoFunil) -> tuple[list[tuple[str, int]], str]:
    saida, nota = [], ""
    for fonte in EXTERNAS.get(caso.componente, EXTERNAS_PADRAO):
        item = caso.valores.get(fonte)
        valor = _registrado(item)
        if valor is None:
            continue
        if fonte == "TCE" and caso.componente == "COTA_ICMS_MUN":
            grupo = (caso.uf, caso.exercicio, _natureza_tce(item))
            if grupo in contexto.grupos_tce_liquido:
                bruto = _centavos(Decimal(valor) / FATOR_FUNDEB)
                razao = f"{contexto.grupos_tce_liquido[grupo]:.3f}".replace(".", ",")
                base = contexto.base_grupo_tce.get(grupo, "Sefaz")
                nota = (f" O TCE-{caso.uf} publica a cota-parte de {caso.exercicio} líquida do Fundeb (razão "
                        f"mediana TCE ÷ {base} de {razao}); {_reais(valor)} convertido para o bruto, {_reais(bruto)}.")
                valor = bruto
        # No ISS, o total gravado do tribunal já é principal + multas e juros + dívida ativa − outras
        # deduções (conceito da nota); o valor "normalizado" da validação desconta as deduções de novo.
        saida.append((fonte, valor))
    return saida, nota


def _correcoes(componente: str, valor: int, permitir_unidade: bool = True,
               permitir_fundeb: bool = True, compensacao: int = 0,
               compensacao_subtrair: bool = False) -> list[tuple[str, str, int, int]]:
    """Erros identificáveis por ajuste mecânico do valor: (código, descrição, valor corrigido, tolerância).

    Ordem da nota: E1, E2, E3a a E3f e E4 (E4a só quando ``compensacao_subtrair``, na Etapa 3b)."""
    saida = []
    if componente == "COTA_ICMS_MUN" and permitir_fundeb:
        saida.append(("E1", "registro líquido do Fundeb (÷ 0,8)", _centavos(Decimal(valor) / FATOR_FUNDEB), TOL_FUNIL))
        saida.append(("E2", "Fundeb somado em vez de subtraído (÷ 1,2)",
                      _centavos(Decimal(valor) / FATOR_FUNDEB_SOMADO), TOL_FUNIL))
    if permitir_unidade:
        for fator in FATORES_UNIDADE:
            saida.append(("E3", f"erro de unidade (÷ {fator:,})".replace(",", "."), _centavos(Decimal(valor) / fator),
                          TOL_FUNIL))
            saida.append(("E3", f"erro de unidade (× {fator:,})".replace(",", "."), valor * fator,
                          max(TOL_FUNIL, 100 * fator)))
    if componente == "COTA_ICMS_MUN" and compensacao:
        if compensacao_subtrair:
            saida.append(("E4a", f"compensação da LC 194 somada à cota-parte (− {_reais(compensacao)})",
                          valor - compensacao, TOL_FUNIL))
        saida.append(("E4b", f"base agregada à compensação da LC 194 registrada à parte (+ {_reais(compensacao)})",
                      valor + compensacao, TOL_FUNIL))
    return saida


def _compensacao(caso, contexto: ContextoFunil) -> int:
    if caso.componente != "COTA_ICMS_MUN":
        return 0
    return contexto.compensacoes.get((caso.id_ente, caso.exercicio), 0)


def _erro_iss_tce(caso, valor: int, tce: int, contexto: ContextoFunil) -> tuple[str, str] | None:
    """Erros próprios do ISS contra o tribunal: deduções não registradas (E6) e composição incompleta (E5)."""
    parcelas = contexto.parcelas_tce.get((caso.id_ente, caso.exercicio, caso.componente)) or {}
    deducoes = abs(int(parcelas.get("OUTRAS_DEDUCOES") or 0))
    if deducoes and _coincide(valor, tce + deducoes):
        return "E6", f"deduções da receita não abatidas ({_reais(deducoes)})"
    presentes = [p for p in COMPONENTES_ISS_TCE if parcelas.get(p)]
    if len(presentes) >= 2:
        for tamanho in range(1, len(presentes)):
            for combinacao in combinations(presentes, tamanho):
                if _coincide(valor, sum(int(parcelas[p]) for p in combinacao)):
                    nomes = " + ".join(ROTULO_COMPONENTE_TCE[p] for p in combinacao)
                    return "E5", f"composição incompleta (só {nomes})"
    return None


def _coincide_apos_erro(caso, valor: int, externas: list[tuple[str, int]],
                        contexto: ContextoFunil) -> tuple[str, int, str, str] | None:
    """Primeiro erro identificável que torna ``valor`` coincidente com uma fonte externa.

    Devolve (fonte, valor da fonte, descrição, código). Nos erros E1 a E3 e nos do ISS, o valor está errado e
    se adota a fonte; no E4b, o valor está certo (a fonte agrega a compensação) e se adota o próprio valor."""
    if caso.componente in ("ISS_MUN", "ISS_DF"):
        for fonte, externo in externas:
            if fonte == "TCE":
                achado = _erro_iss_tce(caso, valor, externo, contexto)
                if achado:
                    return fonte, externo, f"{achado[0]}: {achado[1]}", achado[0]
    for codigo, descricao, corrigido, tolerancia in _correcoes(caso.componente, valor,
                                                               compensacao=_compensacao(caso, contexto)):
        for fonte, externo in externas:
            if _coincide(corrigido, externo, tolerancia):
                return fonte, externo, f"{codigo}: {descricao}", codigo
    return None


def _resultado(original, valor, fonte, regra, justificativa, formula="", marcadores=None) -> dict:
    status = "PENDENTE" if regra == "FP" else "AUTOMATICA"
    valor_cenario = valor if valor is not None else (original or 0)
    aprovado = valor_cenario if status == "AUTOMATICA" else (original or 0)
    return {
        "valor_original": original, "valor_proposto": valor, "valor_cenario": valor_cenario,
        "valor_aprovado": aprovado, "diferenca_aprovada": aprovado - (original or 0),
        "fonte": fonte, "formula": formula, "regra": regra, "etapa": ROTULO_ETAPA[regra], "status": status,
        "justificativa": justificativa, "aplicavel": True, "diferenca": valor_cenario - (original or 0),
        "confirmacao_externa": regra in CONFIRMACAO_EXTERNA, "marcadores": list(marcadores or []),
    }


def aplicar_funil(caso, contexto: ContextoFunil, fora_universo: str = "") -> dict:
    original = caso.valor_principal
    if fora_universo:
        resultado = _resultado(original, original, "—", "FP", fora_universo)
        resultado.update(regra="UNIVERSO", etapa="Fora do universo", status="EXCLUIDO", aplicavel=False,
                         valor_aprovado=original or 0, diferenca_aprovada=0, diferenca=0)
        return resultado

    dca = _registrado(caso.valores.get("SICONFI_DCA"))
    demonstrativos = [(f, v) for f in DEMONSTRATIVOS if (v := _registrado(caso.valores.get(f))) is not None]
    externas, nota_tce = _externas(caso, contexto)
    ext_txt = " + ".join(SIGLA.get(f, f) for f, _ in externas)

    # Etapa 1 · DCA confirmada por fonte externa
    if dca is not None and externas:
        for fonte, externo in externas:
            if _coincide(dca, externo):
                return _resultado(original, dca, "DCA", "F1A",
                                  f"DCA de {_reais(dca)} coincide (± R$ 10,00) com o {SIGLA[fonte]} ({_reais(externo)})."
                                  + nota_tce)
        achado = _coincide_apos_erro(caso, dca, externas, contexto)
        if achado and achado[3] == "E4b":
            fonte, externo, erro, _codigo = achado
            return _resultado(original, dca, "DCA", "F1B",
                              f"DCA de {_reais(dca)} somada à compensação da LC 194 que o Município registrou à parte "
                              f"coincide com o {SIGLA[fonte]} ({_reais(externo)}), que informa o valor agregado ({erro}); "
                              "a DCA está certa e é adotada: a compensação não integra a cota-parte." + nota_tce,
                              formula=erro)
        if achado:
            fonte, externo, erro, _codigo = achado
            return _resultado(original, externo, SIGLA[fonte], "F1B",
                              f"DCA de {_reais(dca)} não coincide com o {SIGLA[fonte]}, mas coincide depois de corrigido o "
                              f"erro identificável ({erro}); adotado o valor do {SIGLA[fonte]}, {_reais(externo)}."
                              + nota_tce, formula=erro)

    # Etapa 2 · demonstrativos do ente confirmados por fonte externa
    if externas:
        for fonte_d, valor_d in demonstrativos:
            for fonte, externo in externas:
                if _coincide(valor_d, externo):
                    return _resultado(original, valor_d, SIGLA[fonte_d], "F2A",
                                      f"{SIGLA[fonte_d]} de {_reais(valor_d)} coincide com o {SIGLA[fonte]} ({_reais(externo)}); "
                                      f"adotado o {SIGLA[fonte_d]}{_texto_dca(dca)}." + nota_tce)
        for fonte_d, valor_d in demonstrativos:
            achado = _coincide_apos_erro(caso, valor_d, externas, contexto)
            if achado and achado[3] == "E4b":
                fonte, externo, erro, _codigo = achado
                return _resultado(original, valor_d, SIGLA[fonte_d], "F2B",
                                  f"{SIGLA[fonte_d]} de {_reais(valor_d)} somado à compensação da LC 194 registrada à "
                                  f"parte coincide com o {SIGLA[fonte]} ({_reais(externo)}), que agrega a compensação "
                                  f"({erro}); adotado o {SIGLA[fonte_d]}{_texto_dca(dca)}." + nota_tce,
                                  formula=f"{SIGLA[fonte_d]} · {erro}")
            if achado:
                fonte, externo, erro, _codigo = achado
                return _resultado(original, externo, SIGLA[fonte], "F2B",
                                  f"{SIGLA[fonte_d]} de {_reais(valor_d)} coincide com o {SIGLA[fonte]} depois de corrigido o "
                                  f"erro identificável ({erro}); adotado o valor do {SIGLA[fonte]}, {_reais(externo)}"
                                  f"{_texto_dca(dca)}." + nota_tce, formula=f"{SIGLA[fonte_d]} · {erro}")

    # Etapa 3 · consistência entre os demonstrativos do ente (sem confirmação externa)
    achado = _concordancia(caso, dca, demonstrativos, _compensacao(caso, contexto))
    if achado:
        regra, valor, fonte, texto, formula = achado
        aviso = (" Sem confirmação externa." if not externas else
                 f" Diverge da fonte externa ({ext_txt}).")
        return _resultado(original, valor, fonte, regra, texto + aviso + nota_tce, formula=formula)

    # Etapa 4 · fonte externa residual
    if externas:
        fonte, externo = externas[0]
        return _resultado(original, externo, SIGLA[fonte], "F4",
                          f"Nenhuma etapa anterior se aplica; adotado o valor do {SIGLA[fonte]} ({_reais(externo)}) como "
                          f"fonte única, sem confirmação{_texto_dca(dca)}." + nota_tce)

    motivo = ("Sem fonte externa e sem três declarações concordantes do ente."
              if dca is not None else "Sem DCA, sem fonte externa e sem dois demonstrativos concordantes.")
    return _resultado(original, None, "—", "FP", motivo + " A metodologia não resolve o caso.")


def _texto_dca(dca: int | None) -> str:
    return " (DCA vazia ou zerada)" if dca is None else f" (DCA de {_reais(dca)} não validada)"


def _concordancia(caso, dca: int | None, demonstrativos: list[tuple[str, int]], compensacao: int = 0):
    valores = dict(demonstrativos)
    pares = [(a, b) for a, b in combinations(DEMONSTRATIVOS, 2) if a in valores and b in valores]
    # 3a · valores como registrados
    if dca is not None:
        for a, b in pares:
            if _coincide(dca, valores[a]) and _coincide(dca, valores[b]) and _coincide(valores[a], valores[b]):
                return ("F3A", dca, "DCA",
                        f"DCA de {_reais(dca)} coincide com {SIGLA[a]} e {SIGLA[b]}, que coincidem entre si; adotada a DCA.",
                        f"DCA = {SIGLA[a]} = {SIGLA[b]}")
    else:
        for a, b in pares:
            if _coincide(valores[a], valores[b]):
                return ("F3A", valores[a], SIGLA[a],
                        f"DCA vazia ou zerada; {SIGLA[a]} e {SIGLA[b]} coincidem ({_reais(valores[a])}); adotado o "
                        f"{SIGLA[a]}, primeiro do par.", f"{SIGLA[a]} = {SIGLA[b]}")
    # 3b · após corrigir um dos valores do grupo
    so_unidade = caso.componente not in ("COTA_ICMS_MUN",)
    grupos = ([("SICONFI_DCA", a, b) for a, b in pares] if dca is not None else [(a, b) for a, b in pares])
    todos = dict(valores)
    if dca is not None:
        todos["SICONFI_DCA"] = dca
    for grupo in grupos:
        for corrigida in grupo:
            for codigo, descricao, corrigido, tolerancia in _correcoes(
                caso.componente, todos[corrigida], permitir_fundeb=not so_unidade,
                compensacao=compensacao, compensacao_subtrair=True,
            ):
                vals = {f: (corrigido if f == corrigida else todos[f]) for f in grupo}
                if all(_coincide(vals[x], vals[y], tolerancia if corrigida in (x, y) else TOL_FUNIL)
                       for x, y in combinations(grupo, 2)):
                    if codigo == "E4b":
                        # os demais agregam a compensação; o valor certo é o que a exclui (o "corrigido" original)
                        intacta = corrigida
                    else:
                        intacta = next(f for f in grupo if f != corrigida)
                        if "SICONFI_DCA" in grupo and corrigida != "SICONFI_DCA":
                            intacta = "SICONFI_DCA"
                    texto_grupo = " = ".join(SIGLA[f] for f in grupo)
                    if codigo == "E4b":
                        adotado = (f"os demais agregam a compensação; adotado o valor que a exclui, do {SIGLA[intacta]} "
                                   f"({_reais(todos[intacta])}).")
                    else:
                        adotado = f"adotado o valor não afetado pelo erro, do {SIGLA[intacta]} ({_reais(todos[intacta])})."
                    return ("F3B", todos[intacta], SIGLA[intacta],
                            f"{texto_grupo} coincidem depois de corrigido o {SIGLA[corrigida]} ({codigo}: {descricao}); "
                            + adotado, f"{texto_grupo} · {SIGLA[corrigida]} {codigo}")
    return None


def _valor_marcado(linha: dict, chave: str) -> int | None:
    resultado = linha[chave]
    valor = resultado.get("valor_proposto")
    if not resultado["aplicavel"] or valor is None or valor <= 0:
        return None
    return int(valor)


def _marcar(linha: dict, chave: str, texto: str) -> None:
    linha[chave]["marcadores"].append(texto)


def _pct(numero: float) -> str:
    return f"{numero:.1%}".replace(".", ",")


def marcar_duplicidades(linhas: list[dict], chave: str = "funil") -> dict[str, int]:
    """V4 (mesmo valor em outro município da UF no exercício) e V5 (mesmo valor do município em outro
    exercício). Marcam o caso; não alteram o valor nem o status.

    Na cota-parte, a V4 não marca a duplicidade que a Sefaz também informa (Estados que atribuem o mesmo índice a
    vários Municípios); no ISS, marca sempre (nota, verificações do ISS)."""
    por_uf: dict[tuple, list[dict]] = defaultdict(list)
    por_ente: dict[tuple, list[dict]] = defaultdict(list)
    for linha in linhas:
        caso = linha["caso"]
        valor = _valor_marcado(linha, chave)
        if valor is None or valor < DUPLICIDADE_VALOR_MINIMO:
            continue
        if caso.esfera == "MUNICIPIO":
            por_uf[(caso.uf, caso.exercicio, caso.componente, valor)].append(linha)
        por_ente[(caso.id_ente, caso.componente, valor)].append(linha)
    v4 = v5 = 0
    for (_uf, _ano, componente, _valor), grupo in por_uf.items():
        if len({l["caso"].id_ente for l in grupo}) <= 1:
            continue
        if componente == "COTA_ICMS_MUN":
            sefaz = {_registrado(l["caso"].valores.get("SEFAZ")) for l in grupo}
            if len(sefaz) == 1 and None not in sefaz:
                continue  # a Sefaz também informa o mesmo valor: duplicidade legítima
        for linha in grupo:
            outros = sorted({l["caso"].nome_ente for l in grupo if l is not linha})
            _marcar(linha, chave, "V4 · valor idêntico ao de " + ", ".join(outros[:3]))
            v4 += 1
    for grupo in por_ente.values():
        if len({l["caso"].exercicio for l in grupo}) > 1:
            for linha in grupo:
                anos = sorted({l["caso"].exercicio for l in grupo if l is not linha})
                _marcar(linha, chave, "V5 · valor idêntico ao de " + ", ".join(map(str, anos)))
                v5 += 1
    return {"v4": v4, "v5": v5}


def marcar_verificacoes(linhas: list[dict], chave: str = "funil") -> dict[str, int]:
    """Verificações de consistência do valor adotado (Nota Técnica). Marcam o caso; não alteram o valor.

    Cota-parte, contra a Sefaz (valor que o Estado informa ter repassado):
    V1 diferença acima de 10%; V2 participação na soma dos Municípios da UF × a mesma participação na Sefaz;
    V3 razão das participações × sua razão típica (mediana do Município); V6 razão valor ÷ Sefaz × sua razão típica.
    ISS, contra o tribunal de contas: V1 e V6; e V7, salto na série própria (≥ 2× ou ≤ ½ da mediana do Município).
    V4 e V5: duplicidades (``marcar_duplicidades``)."""
    contagem = {f"v{n}": 0 for n in range(1, 8)}
    contagem.update(marcar_duplicidades(linhas, chave))
    referencia_de = {"COTA_ICMS_MUN": "SEFAZ", "ISS_MUN": "TCE"}
    # V1 e razões por Município
    razoes: dict[tuple, dict[int, float]] = defaultdict(dict)
    participacao: dict[tuple, list[tuple[dict, int, int]]] = defaultdict(list)
    serie: dict[tuple, dict[int, int]] = defaultdict(dict)
    por_chave: dict[tuple, dict] = {}
    for linha in linhas:
        caso = linha["caso"]
        if caso.esfera != "MUNICIPIO":
            continue
        valor = _valor_marcado(linha, chave)
        if valor is None:
            continue
        por_chave[(caso.id_ente, caso.componente, caso.exercicio)] = linha
        if caso.componente == "ISS_MUN":
            serie[(caso.id_ente, caso.componente)][caso.exercicio] = valor
        fonte = referencia_de.get(caso.componente)
        externo = _registrado(caso.valores.get(fonte)) if fonte else None
        if externo is None:
            continue
        diferenca = abs(valor - externo) / externo
        if diferenca > LIMITE_VERIFICACAO:
            _marcar(linha, chave, f"V1 · {_pct(diferenca)} de diferença para o {SIGLA[fonte]} ({_reais(externo)})")
            contagem["v1"] += 1
        razoes[(caso.id_ente, caso.componente)][caso.exercicio] = valor / externo
        if caso.componente == "COTA_ICMS_MUN":
            participacao[(caso.uf, caso.exercicio)].append((linha, valor, externo))
    # V6 · razão valor ÷ referência × razão típica do Município
    for (id_ente, componente), por_ano in razoes.items():
        if len(por_ano) < MINIMO_EXERCICIOS_TIPICA:
            continue
        tipica = statistics.median(por_ano.values())
        if tipica <= 0:
            continue
        fonte = SIGLA[referencia_de[componente]]
        for ano, razao in por_ano.items():
            desvio = abs(razao / tipica - 1)
            if desvio > LIMITE_VERIFICACAO:
                _marcar(por_chave[(id_ente, componente, ano)], chave,
                        f"V6 · razão valor ÷ {fonte} de {razao:.2f} contra a típica de {tipica:.2f} (desvio de "
                        f"{_pct(desvio)})".replace(".", ","))
                contagem["v6"] += 1
    # V2 e V3 · participação na UF (só cota-parte)
    razoes_participacao: dict[int, dict[int, float]] = defaultdict(dict)
    for (_uf, ano), itens in participacao.items():
        soma_valor = sum(v for _l, v, _e in itens)
        soma_externo = sum(e for _l, _v, e in itens)
        if not soma_valor or not soma_externo:
            continue
        for linha, valor, externo in itens:
            implicita, na_sefaz = valor / soma_valor, externo / soma_externo
            if not na_sefaz:
                continue
            razao = implicita / na_sefaz
            if abs(razao - 1) > LIMITE_VERIFICACAO:
                _marcar(linha, chave, f"V2 · participação na UF de {implicita:.4%} contra {na_sefaz:.4%} na Sefaz"
                        .replace(".", ","))
                contagem["v2"] += 1
            razoes_participacao[linha["caso"].id_ente][ano] = razao
    for id_ente, por_ano in razoes_participacao.items():
        if len(por_ano) < MINIMO_EXERCICIOS_TIPICA:
            continue
        tipica = statistics.median(por_ano.values())
        for ano, razao in por_ano.items():
            desvio = abs(razao / tipica - 1) if tipica else 0
            if desvio > LIMITE_VERIFICACAO:
                _marcar(por_chave[(id_ente, "COTA_ICMS_MUN", ano)], chave,
                        f"V3 · desvio de {_pct(desvio)} da razão típica de participação na UF")
                contagem["v3"] += 1
    # V7 · salto na série própria (ISS)
    for (id_ente, componente), por_ano in serie.items():
        if len(por_ano) < MINIMO_EXERCICIOS_TIPICA:
            continue
        mediana = statistics.median(por_ano.values())
        for ano, valor in por_ano.items():
            if mediana and (valor >= FATOR_SALTO_V7 * mediana or valor * FATOR_SALTO_V7 <= mediana):
                _marcar(por_chave[(id_ente, componente, ano)], chave,
                        f"V7 · {valor / mediana:.2f}× a mediana da série do Município ({_reais(int(mediana))})"
                        .replace(".", ",", 1))
                contagem["v7"] += 1
    return contagem
