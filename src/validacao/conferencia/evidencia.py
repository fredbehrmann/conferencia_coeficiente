"""Dados que levaram o caso a cada alerta, para conferência na tela de análise.

Cada alerta recebe uma tabela montada na hora da consulta, a partir das evidências gravadas
pela regra (entes, exercícios, registros) e dos casos e registros da mesma execução. Nada
aqui altera o diagnóstico: é a prova do que a regra encontrou.
"""

from __future__ import annotations

import json
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.comparacao import sigla
from validacao.conferencia.dinheiro import percentual, reais
from validacao.conferencia.execucao import expandir_valores
from validacao.conferencia.exportacao import DIAGNOSTICOS

COMPARABILIDADE_TEXTO = {
    "REFERENCIA": "referência", "SIM": "comparável", "RESSALVA": "comparável com ressalva",
    "NAO": "não comparável diretamente", "SEM_DADO": "sem valor",
}

LIMITE_LINHAS = 300
ESTADOS_TEXTO = {
    "VALOR": "valor", "ZERO": "zero declarado", "AUSENTE": "sem valor", "INVALIDO": "formato inválido",
    "NAO_APLICAVEL": "não aplicável",
}


def _valor_serie(item: dict | None) -> int | None:
    if not item or item.get("estado") not in ("VALOR", "ZERO"):
        return None
    return item.get("normalizado") if item.get("normalizado") is not None else item.get("original")


def _txt(valor: int | None) -> str:
    return "—" if valor is None else reais(valor)


def _procedencia(registro: cf.Registro | None, lote: cf.Lote | None) -> str:
    if registro is None:
        return ""
    p = json.loads(registro.procedencia or "{}")
    partes = [f"registro {registro.id}", f"v{registro.versao}"]
    if lote is not None:
        partes.append(f"lote {lote.id}")
    arquivo = p.get("arquivo") or (lote.arquivo.replace("\\", "/").rsplit("/", 1)[-1] if lote and lote.arquivo else "")
    if arquivo:
        partes.append(arquivo)
    if p.get("aba"):
        partes.append(f"aba {p['aba']}")
    if p.get("linha"):
        partes.append(f"linha {p['linha']}")
    if registro.data_extracao:
        partes.append(f"extraído em {registro.data_extracao:%d/%m/%Y}")
    return " · ".join(str(x) for x in partes)


def _registros(session: Session, ids) -> dict[int, tuple[cf.Registro, cf.Lote]]:
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    return {
        r.id: (r, l) for r, l in session.execute(
            select(cf.Registro, cf.Lote).join(cf.Lote, cf.Lote.id == cf.Registro.lote_id).where(cf.Registro.id.in_(ids))
        )
    }


def _tabela(titulo, colunas, linhas, nota=None, rodape=None):
    truncado = len(linhas) > LIMITE_LINHAS
    return {
        "titulo": titulo, "colunas": colunas, "linhas": linhas[:LIMITE_LINHAS], "nota": nota,
        "rodape": rodape or (f"Exibidas {LIMITE_LINHAS} de {len(linhas)} linhas." if truncado else None),
    }


def _linha(celulas, destaque=False, caso=None):
    return {"celulas": celulas, "destaque": destaque, "caso": caso}


# ---------------------------------------------------------------- repetição entre entes

def _ree(session, caso, alerta, ev, info):
    decl = alerta.declaracao_a or caso.declaracao_principal
    outros = [int(e) for e in ev.get("entes") or []]
    if not outros:
        return None
    valor = alerta.valor_a
    casos = list(session.scalars(
        select(cf.Caso).where(
            cf.Caso.execucao_id == caso.execucao_id, cf.Caso.componente_id == caso.componente_id,
            cf.Caso.id_ente.in_(outros + [caso.id_ente]),
        )
    ))
    serie: dict[int, dict[int, int | None]] = {}
    por_chave: dict[tuple[int, int], cf.Caso] = {}
    for item in casos:
        serie.setdefault(item.id_ente, {})[item.exercicio] = _valor_serie(expandir_valores(item.valores).get(decl))
        por_chave[(item.id_ente, item.exercicio)] = item
    proprio = serie.get(caso.id_ente, {})
    linhas = []
    for id_ente in [caso.id_ente] + outros:
        atual = por_chave.get((id_ente, caso.exercicio))
        if atual is None:
            continue
        outros_anos = [
            str(ano) for ano, v in sorted(serie.get(id_ente, {}).items())
            if ano != caso.exercicio and v is not None and v != 0 and proprio.get(ano) == v
        ] if id_ente != caso.id_ente else []
        valores = expandir_valores(atual.valores).get(decl) or {}
        linhas.append(_linha([
            atual.nome_ente, atual.uf, str(id_ente), _txt(_valor_serie(valores)),
            ", ".join(outros_anos) or "—",
            "este caso" if id_ente == caso.id_ente else DIAGNOSTICOS.get(atual.diagnostico, atual.diagnostico),
        ], destaque=id_ente == caso.id_ente, caso=None if id_ente == caso.id_ente else atual.id))
    nota = (f"Entes cujo valor na {sigla(decl)} em {caso.exercicio} é idêntico, até o centavo, a {_txt(valor)}. "
            "A coluna \"Coincide também em\" mostra outros exercícios em que o mesmo par de entes repete o valor — "
            "repetição em vários anos reforça a hipótese de cópia de relatório.")
    return _tabela(
        f"Entes com o mesmo valor ({len(linhas) - 1})",
        [("Ente", ""), ("UF", ""), ("IBGE", "mono"), (f"{sigla(decl)} {caso.exercicio}", "num"), ("Coincide também em", ""), ("Situação do caso", "")],
        linhas, nota,
    )


# ---------------------------------------------------------------- série do próprio ente

def _serie_principal(info, decl):
    return {ano: _valor_serie(item) for ano, item in (info["matriz"].get(decl) or {}).items()}


def _procedencias_serie(session, info, decl, anos):
    ids = {ano: (info["matriz"].get(decl, {}).get(ano) or {}).get("registro") for ano in anos}
    regs = _registros(session, ids.values())
    return {ano: _procedencia(*regs[i]) if i in regs else "" for ano, i in ids.items()}


def _rep(session, caso, alerta, ev, info):
    decl = alerta.declaracao_a or caso.declaracao_principal
    serie = _serie_principal(info, decl)
    valor = serie.get(caso.exercicio)
    anos = sorted(serie)
    proc = _procedencias_serie(session, info, decl, anos)
    linhas = []
    for ano in anos:
        v = serie[ano]
        igual = v is not None and v == valor
        anterior = serie.get(ano - 1)
        variacao = "—" if v is None or not anterior else ("idêntico" if v == anterior else percentual(v / anterior - 1, 1, sinal=True))
        linhas.append(_linha([str(ano), _txt(v), variacao, "sim" if igual else "", proc.get(ano, "")], destaque=igual))
    repetidos = [str(a) for a in anos if serie[a] is not None and serie[a] == valor]
    return _tabela(
        f"Série da {sigla(decl)} — valor repetido em {', '.join(repetidos)}",
        [("Exercício", ""), ("Valor", "num"), ("Variação anual", "num"), ("Igual ao do caso", "centro"), ("Procedência", "pequeno")],
        linhas,
        "Linhas destacadas: exercícios com valor idêntico ao deste caso. Compare a procedência dos registros "
        "(arquivo, linha e data de extração) para verificar se o mesmo relatório foi enviado para exercícios diferentes.",
    )


def _var(session, caso, alerta, ev, info):
    decl = alerta.declaracao_a or caso.declaracao_principal
    anos = sorted({a for d in info["matriz"] for a in info["matriz"][d]})
    fontes = [decl] + [d for d in info["matriz"] if d != decl]
    series = {d: _serie_principal(info, d) for d in fontes}
    corroboram = set(ev.get("corroboram") or [])
    colunas = [("Exercício", "")]
    for d in fontes:
        colunas += [(sigla(d), "num"), (f"Δ {sigla(d)}", "num")]
    linhas = []
    for ano in anos:
        celulas = [str(ano)]
        for d in fontes:
            v, ant = series[d].get(ano), series[d].get(ano - 1)
            celulas += [_txt(v), "—" if v is None or not ant or ant <= 0 or v <= 0 else f"{v / ant:.2f}×".replace(".", ",")]
        linhas.append(_linha(celulas, destaque=ano == caso.exercicio))
    razao = ev.get("razao")
    nota = (f"Razão encontrada na {sigla(decl)}: {razao:.2f}×".replace(".", ",") if razao else "") + (
        f". Corroboram o movimento: {', '.join(sorted(corroboram))}." if corroboram else ". Nenhuma fonte comparável repete o movimento.")
    return _tabela("Série por fonte e razão em relação ao exercício anterior", colunas, linhas, nota.lstrip(". "))


def _esc(session, caso, alerta, ev, info):
    decl = alerta.declaracao_a or caso.declaracao_principal
    if alerta.declaracao_b:
        return _par(session, caso, alerta, ev, info, extra=f"Potência encontrada: 10^{ev.get('potencia')}.")
    serie = _serie_principal(info, decl)
    mediana = ev.get("mediana_serie") or (median([v for a, v in serie.items() if a != caso.exercicio and v]) if serie else None)
    linhas = [
        _linha([str(a), _txt(v), "—" if not v or not mediana else f"{v / mediana:.2f}×".replace(".", ",")], destaque=a == caso.exercicio)
        for a, v in sorted(serie.items())
    ]
    return _tabela(f"Série da {sigla(decl)} contra a mediana dos demais exercícios ({_txt(int(mediana)) if mediana else '—'})",
                   [("Exercício", ""), ("Valor", "num"), ("Razão à mediana", "num")], linhas)


# ---------------------------------------------------------------- pares e valores do exercício

def _como(alerta, declaracao, item, usado, derivacao) -> str:
    if derivacao:
        return derivacao
    if usado is not None and item.get("original") is not None and usado != item.get("original"):
        if alerta.regra == "FLF" and declaracao == alerta.declaracao_a:
            return "valor declarado deduzido do FUNDEB (ver nota)"
        return item.get("derivacao") or "ajustado para a parcela comparada"
    return item.get("derivacao") or "valor declarado"


def _par(session, caso, alerta, ev, info, extra=None):
    valores = info["valores"]
    linhas = []
    usados = {alerta.declaracao_a: alerta.valor_a, alerta.declaracao_b: alerta.valor_b}
    derivacoes = {alerta.declaracao_a: ev.get("derivacao_ref"), alerta.declaracao_b: ev.get("derivacao_fonte")}
    for d in [alerta.declaracao_a, alerta.declaracao_b]:
        if not d:
            continue
        v = valores.get(d) or {}
        linhas.append(_linha([
            sigla(d), "referência" if d == alerta.declaracao_a else "fonte comparada",
            ESTADOS_TEXTO.get(v.get("estado"), v.get("estado") or "sem registro"), _txt(v.get("original")),
            _txt(usados.get(d) if usados.get(d) is not None else v.get("normalizado")),
            _como(alerta, d, v, usados.get(d), derivacoes.get(d)),
        ], destaque=d == alerta.declaracao_b))
    partes = []
    parcela = (valores.get(alerta.declaracao_b) or {}).get("parcela")
    if parcela:
        partes.append(f"Parcela comparada: {parcela}.")
    if alerta.dif_abs is not None:
        partes.append(f"Diferença assinada (fonte − referência) {_txt(alerta.dif_assinada)} "
                      f"({percentual(alerta.dif_pct, 2, sinal=True) if alerta.dif_pct is not None else 'percentual não calculável'}).")
    motivo = (valores.get(alerta.declaracao_b) or {}).get("motivo")
    if motivo and alerta.regra in ("NCP", "FLF"):
        partes.append(motivo)
    if ev.get("concordam"):
        partes.append(f"Concordam com a referência dentro dos limites: {', '.join(ev['concordam'])}.")
    if extra:
        partes.append(extra)
    return _tabela("Valores comparados",
                   [("Fonte", ""), ("Papel", ""), ("Estado", ""), ("Valor original", "num"), ("Valor usado na comparação", "num"), ("Como foi obtido", "pequeno")],
                   linhas, " ".join(partes) or None)


def _zer(session, caso, alerta, ev, info):
    valores = info["valores"]
    positivos = ev.get("positivos") or {}
    linhas = []
    for d, v in valores.items():
        if v.get("estado") not in ("VALOR", "ZERO"):
            continue
        linhas.append(_linha([sigla(d), ESTADOS_TEXTO.get(v["estado"], v["estado"]), _txt(v.get("original")),
                              "acima do mínimo" if d in positivos else ""], destaque=v["estado"] == "ZERO"))
    return _tabela("Zero declarado e valores positivos no exercício", [("Fonte", ""), ("Estado", ""), ("Valor", "num"), ("Positivo considerado", "")], linhas)


def _ufc(session, caso, alerta, ev, info):
    municipais = list(session.scalars(
        select(cf.Caso).where(
            cf.Caso.execucao_id == caso.execucao_id, cf.Caso.componente_id == "COTA_ICMS_MUN",
            cf.Caso.uf == caso.uf, cf.Caso.exercicio == caso.exercicio,
        ).order_by(cf.Caso.valor_principal.desc().nulls_last())
    ))
    soma = sum(m.valor_principal or 0 for m in municipais)
    linhas = []
    for m in municipais:
        participacao = "—" if not soma or m.valor_principal is None else percentual(m.valor_principal / soma, 2)
        linhas.append(_linha([m.nome_ente, str(m.id_ente), sigla(m.declaracao_principal) if m.declaracao_principal else "—",
                              _txt(m.valor_principal), participacao, DIAGNOSTICOS.get(m.diagnostico, m.diagnostico)], caso=m.id))
    sem_valor = sum(1 for m in municipais if m.valor_principal is None)
    nota = (f"Repasse lançado pelo estado ({ev.get('origem', '')}): {_txt(alerta.valor_a)}. Soma das cotas municipais: {_txt(ev.get('soma_municipal', soma))}"
            f" em {len(municipais) - sem_valor} município(s) com valor" + (f"; {sem_valor} sem valor de referência." if sem_valor else "."))
    return _tabela(f"Cotas-partes declaradas pelos municípios de {caso.uf} em {caso.exercicio}",
                   [("Município", ""), ("IBGE", "mono"), ("Fonte", ""), ("Cota-parte", "num"), ("Participação", "num"), ("Situação do caso", "")],
                   linhas, nota)


# ---------------------------------------------------------------- registros

def _tabela_registros(titulo, pares, destaque_ids=(), nota=None):
    linhas = [
        _linha([sigla(r.declaracao_id), r.campo, r.parcela or "—", ESTADOS_TEXTO.get(r.estado, r.estado), _txt(r.valor_centavos),
                f"v{r.versao}" + ("" if r.vigente else " (substituída)"), _procedencia(r, l)], destaque=r.id in destaque_ids)
        for r, l in pares
    ]
    return _tabela(titulo, [("Fonte", ""), ("Campo", "mono"), ("Parcela", ""), ("Estado", ""), ("Valor", "num"), ("Versão", ""), ("Procedência", "pequeno")], linhas, nota)


def _dup(session, caso, alerta, ev, info):
    ids = set(int(i) for i in ev.get("registros") or [])
    campos = {r.campo for r, _ in _registros(session, ids).values()}
    pares = [(r, l) for r, l in info["registros"] if r.declaracao_id == alerta.declaracao_a and r.campo in campos]
    return _tabela_registros("Registros do mesmo campo no lote", pares, ids,
                             "Linhas destacadas: ocorrências repetidas. Vale o primeiro registro lido; os demais estão preservados.")


def _ret(session, caso, alerta, ev, info):
    pares = [(r, l) for r, l in info["registros"] if r.declaracao_id == alerta.declaracao_a and r.parcela in ("TOTAL", "BRUTO", None)]
    pares = pares or [(r, l) for r, l in info["registros"] if r.declaracao_id == alerta.declaracao_a]
    return _tabela_registros(f"Versões dos registros da {sigla(alerta.declaracao_a)}", pares,
                             {r.id for r, _ in pares if r.vigente}, "Linha destacada: versão vigente.")


def _neg(session, caso, alerta, ev, info):
    ids = {int(ev["registro"])} if ev.get("registro") else set()
    pares = [(r, l) for r, l in info["registros"] if r.declaracao_id == alerta.declaracao_a and r.vigente]
    return _tabela_registros(f"Registros vigentes da {sigla(alerta.declaracao_a)}", pares, ids)


def _tot(session, caso, alerta, ev, info):
    pares = [(r, l) for r, l in info["registros"] if r.declaracao_id == alerta.declaracao_a and r.vigente]
    return _tabela_registros(f"Total e parcelas da {sigla(alerta.declaracao_a)}", pares,
                             {r.id for r, _ in pares if r.parcela == "TOTAL"},
                             f"Composição usada: {ev.get('composicao', '—')} = {_txt(alerta.valor_b)}; total declarado {_txt(alerta.valor_a)}.")


def _aus(session, caso, alerta, ev, info):
    linhas = []
    if alerta.declaracao_a and alerta.declaracao_a not in info["valores"]:
        linhas.append(_linha([sigla(alerta.declaracao_a), "sem registro (fonte principal)", "—"], destaque=True))
    for d, v in info["valores"].items():
        linhas.append(_linha([sigla(d), ESTADOS_TEXTO.get(v.get("estado"), v.get("estado") or "—"), _txt(v.get("original"))],
                             destaque=d == alerta.declaracao_a))
    if not linhas:
        return _tabela("Fontes no exercício", [("Fonte", "")], [], "Nenhuma fonte tem registro neste exercício.")
    return _tabela("O que cada fonte informa neste exercício", [("Fonte", ""), ("Estado", ""), ("Valor", "num")], linhas)


def _ins(session, caso, alerta, ev, info):
    linhas = [
        _linha([sigla(d), COMPARABILIDADE_TEXTO.get(v.get("comparabilidade"), v.get("comparabilidade") or "—"), v.get("motivo") or "", _txt(v.get("original"))],
               destaque=d in (ev.get("comparaveis") or []))
        for d, v in info["valores"].items()
    ]
    return _tabela("Comparabilidade das fontes", [("Fonte", ""), ("Comparabilidade", ""), ("Motivo", "pequeno"), ("Valor", "num")], linhas,
                   "Linhas destacadas: fontes independentes comparáveis (contam para o mínimo).")


GERADORES = {
    "REE": _ree, "REP": _rep, "VAR": _var, "ESC": _esc, "DSI": _par, "DAX": _par, "FLF": _par, "NCP": _par,
    "ZER": _zer, "UFC": _ufc, "DUP": _dup, "RET": _ret, "NEG": _neg, "TOT": _tot, "AUS": _aus, "INS": _ins,
}


def dados_alertas(session: Session, caso: cf.Caso, info: dict) -> dict[int, dict]:
    """Tabela de evidência por alerta (id do alerta → tabela). Falhas viram aviso, nunca erro de tela."""
    resultado = {}
    for alerta in info["alertas"]:
        gerador = GERADORES.get(alerta.regra)
        if gerador is None:
            continue
        try:
            ev = json.loads(alerta.evidencias or "{}")
            tabela = gerador(session, caso, alerta, ev, info)
        except Exception as erro:  # a evidência é auxiliar: não pode derrubar o painel
            tabela = _tabela("Dados do alerta", [], [], f"Não foi possível montar os dados deste alerta: {erro}")
        if tabela is not None:
            resultado[alerta.id] = tabela
    return resultado
