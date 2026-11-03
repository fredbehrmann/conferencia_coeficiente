import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from validacao.cadastro import existia_no_ano
from validacao.cargas.comum import codigo_inteiro, ler_planilha
from validacao.catalogo import POR_ID
from validacao.modelos import Carga, Ente, ValorObservado, Verificacao
from validacao.regras import RegraVersao, garantir_regra, parametros_de

FONTES = (
    "SICONFI",
    "MSC",
    "RREO",
    "SIOPE",
    "SIOPS",
    "TCE",
    "CONFAZ",
    "SEFAZ_UF",
    "TESOURO",
    "ANP",
    "ANM",
    "SIMPLES",
)
FONTES_ABERTURA = ("SIOPE", "TCE", "CONFAZ", "SEFAZ_UF", "SIMPLES")
FONTES_PARCIAIS = ("TESOURO", "ANP", "ANM")
PRIORIDADE = {"SIOPE": 0, "TCE": 1, "CONFAZ": 2, "SEFAZ_UF": 3, "SIMPLES": 4}
BLOCOS_DA_FONTE = {
    "SICONFI": ("ISS", "COTA_RECEBIDA", "ICMS", "COTA_REPASSADA", "COMPENSACAO", "ISS_SIMPLES"),
    "MSC": ("ISS", "COTA_RECEBIDA", "ICMS", "COMPENSACAO"),
    "RREO": ("ISS", "COTA_RECEBIDA", "ICMS", "COMPENSACAO"),
    "SIOPE": ("ISS", "COTA_RECEBIDA", "ICMS", "COTA_REPASSADA", "COMPENSACAO"),
    "SIOPS": ("ISS", "COTA_RECEBIDA", "ICMS", "COTA_REPASSADA", "COMPENSACAO"),
    "TCE": ("ISS", "COTA_RECEBIDA", "ICMS", "COTA_REPASSADA", "COMPENSACAO"),
    "CONFAZ": ("ICMS",),
    "SEFAZ_UF": ("COTA_RECEBIDA", "COTA_REPASSADA"),
    "TESOURO": ("COMPENSACAO",),
    "ANP": ("COMPENSACAO",),
    "ANM": ("COMPENSACAO",),
    "SIMPLES": ("ISS_SIMPLES",),
}

VERMELHAS = frozenset(
    {
        "SEM_ABERTURA",
        "VAZIO_COM_VALOR_EXTERNO",
        "NEGATIVO",
        "VALOR_REPETIDO",
        "SALTO_SERIE",
        "DIVERGE_ACIMA_5",
        "UF_NAO_FECHA",
    }
)

ORDEM_SITUACOES = (
    "FECHA",
    "DIVERGE_1_5",
    "DIVERGE_ACIMA_5",
    "SEM_ABERTURA",
    "VALOR_REPETIDO",
    "SALTO_SERIE",
    "VAZIO_COM_VALOR_EXTERNO",
    "NEGATIVO",
    "SEM_DECLARACAO",
    "ITENS_NAO_SOMAM",
    "FONTE_LIQUIDA_FUNDEB",
    "FORA_COBERTURA",
    "NAO_SE_APLICA",
    "UF_NAO_FECHA",
)

BLOCOS_POR_ESFERA = {
    "MUNICIPIO": ("ISS", "COTA_RECEBIDA", "COMPENSACAO", "ISS_SIMPLES"),
    "ESTADO": ("ICMS", "COTA_REPASSADA", "COMPENSACAO"),
    "DF": ("ISS", "ICMS", "COMPENSACAO", "ISS_SIMPLES"),
}
NAO_APLICA = {"DF": ("COTA_RECEBIDA", "COTA_REPASSADA")}

TOTAL_DO_BLOCO = {
    "ISS": "ISS_TOTAL",
    "COTA_RECEBIDA": "COTA_RECEBIDA_TOTAL",
    "ICMS": "ICMS_TOTAL",
    "COTA_REPASSADA": "COTA_REPASSADA",
    "COMPENSACAO": "COMPENSACAO",
    "ISS_SIMPLES": "ISS_SIMPLES",
}
BRUTO_DO_BLOCO = {
    "ISS": "ISS_BRUTO",
    "COTA_RECEBIDA": "COTA_RECEBIDA_BRUTO",
    "ICMS": "ICMS_BRUTO",
}
LOTE = 400


def reprocessar(session: Session, regra: RegraVersao | None = None) -> tuple[dict[str, int], int]:
    if regra is None:
        regra = garantir_regra(session)
    session.flush()
    params = parametros_de(regra)
    anos = [int(ano) for ano in params["anos"]]
    cargas = _cargas_vigentes(session)
    if "SICONFI" not in cargas:
        raise RuntimeError("Não há carga concluída do SICONFI.")

    print("Verificando...", flush=True)
    entes = {
        id_ente: (esfera, uf, instalacao, extincao)
        for id_ente, esfera, uf, instalacao, extincao in session.execute(
            select(Ente.id_ente, Ente.esfera, Ente.uf, Ente.ano_instalacao, Ente.ano_extincao)
        )
    }
    totais, itens, presentes = _ler_valores(session, cargas)
    universo = {ente for (ente, _ano) in presentes}
    serie = _serie_siconfi(totais)
    somas_uf = _somas_municipais(totais, entes)
    cobertura = _cobertura_tce(session, cargas.get("TCE"))

    linhas = []
    contagem = Counter()
    for id_ente in sorted(universo):
        esfera, uf, instalacao, extincao = entes[id_ente]
        blocos = BLOCOS_POR_ESFERA[esfera]
        for ano in anos:
            if not existia_no_ano(ano, instalacao, extincao):
                for bloco in blocos:
                    registro = _nao_se_aplica(id_ente, ano, bloco, regra.id)
                    linhas.append(registro)
                    contagem.update(json.loads(registro["situacoes"]))
                for bloco in NAO_APLICA.get(esfera, ()):
                    registro = _nao_se_aplica(id_ente, ano, bloco, regra.id)
                    linhas.append(registro)
                    contagem.update(json.loads(registro["situacoes"]))
                continue
            declarado = (id_ente, ano) in presentes
            for bloco in blocos:
                registro = _avaliar_bloco(
                    id_ente,
                    ano,
                    bloco,
                    esfera,
                    uf,
                    declarado,
                    totais,
                    itens,
                    serie,
                    somas_uf,
                    cobertura,
                    params,
                )
                registro["regra_versao_id"] = regra.id
                linhas.append(registro)
                contagem.update(json.loads(registro["situacoes"]))
            for bloco in NAO_APLICA.get(esfera, ()):
                registro = _nao_se_aplica(id_ente, ano, bloco, regra.id)
                linhas.append(registro)
                contagem.update(json.loads(registro["situacoes"]))

    session.execute(delete(Verificacao).where(Verificacao.regra_versao_id == regra.id))
    for inicio in range(0, len(linhas), LOTE):
        session.execute(insert(Verificacao), linhas[inicio : inicio + LOTE])
    return dict(contagem), len(linhas)


def texto_resumo(contagem: dict[str, int], linhas: int) -> str:
    partes = [f"{codigo}: {contagem.get(codigo, 0)}" for codigo in ORDEM_SITUACOES]
    return f"Verificação: {linhas} blocos\n" + "\n".join(partes)


def _cargas_vigentes(session: Session) -> dict[str, int]:
    linhas = session.execute(
        select(Carga.fonte_id, func.max(Carga.id))
        .where(Carga.situacao == "CONCLUIDA")
        .group_by(Carga.fonte_id)
    ).all()
    return dict(linhas)


def _ler_valores(session, cargas):
    totais = {fonte: {} for fonte in FONTES}
    itens = {fonte: defaultdict(dict) for fonte in FONTES}
    presentes = set()
    consulta = session.execute(
        select(
            Carga.fonte_id,
            ValorObservado.id_ente,
            ValorObservado.ano,
            ValorObservado.componente_id,
            ValorObservado.valor,
            ValorObservado.natureza,
        )
        .join(Carga, Carga.id == ValorObservado.carga_id)
        .where(Carga.id.in_(cargas.values()))
    )
    for fonte, id_ente, ano, componente, valor, natureza in consulta:
        if fonte not in totais:
            continue
        definicao = POR_ID.get(componente)
        if definicao is None:
            continue
        bloco = definicao.bloco
        if componente == TOTAL_DO_BLOCO.get(bloco):
            totais[fonte][(id_ente, ano, bloco)] = (valor, natureza)
        elif definicao.papel in ("SOMA", "SUBTRAI"):
            itens[fonte][(id_ente, ano, bloco)][componente] = valor
        if fonte == "SICONFI":
            presentes.add((id_ente, ano))
    return totais, itens, presentes


def _serie_siconfi(totais) -> dict:
    serie = defaultdict(dict)
    for (id_ente, ano, bloco), (valor, _natureza) in totais["SICONFI"].items():
        serie[(id_ente, bloco)][ano] = valor
    return serie


def _somas_municipais(totais, entes) -> dict:
    somas = defaultdict(float)
    for (id_ente, ano, bloco), (valor, _natureza) in totais["SICONFI"].items():
        if bloco != "COTA_RECEBIDA":
            continue
        esfera = entes[id_ente][0]
        uf = entes[id_ente][1]
        if esfera != "MUNICIPIO":
            continue
        somas[(uf, ano)] += valor
    return somas


def _cobertura_tce(session: Session, carga_id: int | None) -> set[int]:
    codigos = _codigos_da_planilha_tce(session)
    if carga_id is None:
        return codigos
    caminho = session.scalar(select(Carga.arquivo_ou_endpoint).where(Carga.id == carga_id))
    destino = Path(caminho) if caminho else None
    if destino is not None and destino.suffix.lower() == ".xlsx":
        return codigos
    presentes = set(
        session.scalars(select(ValorObservado.id_ente).where(ValorObservado.carga_id == carga_id).distinct())
    )
    return codigos | presentes


def _codigos_da_planilha_tce(session: Session) -> set[int]:
    caminhos = session.scalars(
        select(Carga.arquivo_ou_endpoint).where(Carga.fonte_id == "TCE", Carga.situacao == "CONCLUIDA")
    )
    for caminho in caminhos:
        destino = Path(caminho)
        if destino.suffix.lower() != ".xlsx" or not destino.is_file():
            continue
        print("Lendo cobertura do TCE...", flush=True)
        quadro = ler_planilha(destino, sheet_name="ISS", header=3, usecols=["Cód. IBGE"])
        return {
            codigo
            for valor in quadro["Cód. IBGE"]
            if (codigo := codigo_inteiro(valor)) is not None
        }
    return set()


def _avaliar_bloco(
    id_ente,
    ano,
    bloco,
    esfera,
    uf,
    declarado,
    totais,
    itens,
    serie,
    somas_uf,
    cobertura,
    params,
) -> dict:
    valores = {}
    naturezas = {}
    fontes_do_bloco = [fonte for fonte in FONTES if bloco in BLOCOS_DA_FONTE[fonte]]
    for fonte in FONTES:
        if fonte not in fontes_do_bloco:
            valores[fonte] = None
            naturezas[fonte] = None
            continue
        encontrado = totais[fonte].get((id_ente, ano, bloco))
        if encontrado is None:
            valores[fonte] = None
            naturezas[fonte] = None
        else:
            valores[fonte], naturezas[fonte] = encontrado
    referencia = valores["SICONFI"]
    if bloco == "ISS" and params.get("iss_simples_soma") and referencia is not None:
        simples = totais["SIMPLES"].get((id_ente, ano, "ISS_SIMPLES"))
        if simples is not None:
            referencia = referencia + simples[0]
    if bloco == "COMPENSACAO":
        parciais = [valores[fonte] for fonte in FONTES_PARCIAIS if valores[fonte] is not None]
        if parciais:
            valores["EXTERNAS"] = sum(parciais)
    partes = {fonte: itens[fonte].get((id_ente, ano, bloco), {}) for fonte in FONTES}
    situacoes = set()

    if not declarado:
        situacoes.add("SEM_DECLARACAO")
    if referencia is not None and referencia < -0.005:
        situacoes.add("NEGATIVO")
    if (referencia is None or abs(referencia) < 0.005) and any(
        valor is not None and valor > 0.005 for fonte, valor in valores.items() if fonte != "SICONFI"
    ):
        situacoes.add("VAZIO_COM_VALOR_EXTERNO")
    if (
        _so_bruto(partes["SICONFI"], bloco)
        and referencia is not None
        and not _confirmado(referencia, valores, params)
        and not _msc_abre(partes["MSC"], bloco, referencia, params)
    ):
        situacoes.add("SEM_ABERTURA")
    if _repetido(serie, id_ente, bloco, ano, referencia):
        situacoes.add("VALOR_REPETIDO")
    if _salto(serie, id_ente, bloco, ano, referencia, params):
        situacoes.add("SALTO_SERIE")

    faixa = _pior_faixa(referencia, valores, params)
    if faixa == "acima":
        situacoes.add("DIVERGE_ACIMA_5")
    elif faixa == "1_5":
        situacoes.add("DIVERGE_1_5")
    if _itens_nao_somam(valores, partes, params):
        situacoes.add("ITENS_NAO_SOMAM")
    if _liquido_fundeb(referencia, valores, params):
        situacoes.add("FONTE_LIQUIDA_FUNDEB")
    if id_ente not in cobertura:
        situacoes.add("FORA_COBERTURA")
    if esfera == "ESTADO" and bloco == "COTA_REPASSADA" and referencia is not None:
        if _uf_nao_fecha(referencia, somas_uf.get((uf, ano), 0.0), params):
            situacoes.add("UF_NAO_FECHA")

    fecha = _fecha(referencia, valores, partes, params)
    if fecha:
        situacoes.add("FECHA")
    fonte_sugerida, valor_sugerido = _sugerir(referencia, valores, partes, params, fecha)
    return {
        "id_ente": id_ente,
        "ano": ano,
        "bloco": bloco,
        "valor_referencia": referencia,
        "fonte_sugerida": fonte_sugerida,
        "valor_sugerido": valor_sugerido,
        "situacoes": json.dumps(_ordenar(situacoes), ensure_ascii=False),
        "diferencas": json.dumps(
            _diferencas(referencia, valores, naturezas, fontes_do_bloco), ensure_ascii=False
        ),
    }


def _nao_se_aplica(id_ente, ano, bloco, regra_id) -> dict:
    vazio = {fonte: None for fonte in FONTES}
    return {
        "id_ente": id_ente,
        "ano": ano,
        "bloco": bloco,
        "regra_versao_id": regra_id,
        "valor_referencia": None,
        "fonte_sugerida": None,
        "valor_sugerido": None,
        "situacoes": json.dumps(["NAO_SE_APLICA"]),
        "diferencas": json.dumps(_diferencas(None, vazio, vazio), ensure_ascii=False),
    }


def _msc_abre(partes: dict, bloco: str, referencia: float, params) -> bool:
    if not partes or _so_bruto(partes, bloco):
        return False
    calculado = _soma_itens(partes)
    return calculado is not None and _sao_iguais(calculado, referencia, params)


def _so_bruto(partes: dict, bloco: str) -> bool:
    bruto = BRUTO_DO_BLOCO.get(bloco)
    for componente in partes:
        papel = POR_ID[componente].papel
        if papel not in ("SOMA", "SUBTRAI"):
            continue
        if componente == bruto:
            continue
        return False
    return True


def _confirmado(referencia, valores, params) -> bool:
    for fonte in FONTES_ABERTURA:
        outro = valores[fonte]
        if outro is not None and _dentro_do_aceite(referencia, outro, params):
            return True
    return False


def _repetido(serie, id_ente, bloco, ano, referencia) -> bool:
    if referencia is None:
        return False
    anterior = serie.get((id_ente, bloco), {}).get(ano - 1)
    if anterior is None:
        return False
    return round(referencia, 2) == round(anterior, 2)


def _salto(serie, id_ente, bloco, ano, referencia, params) -> bool:
    if referencia is None:
        return False
    outros = [valor for exercicio, valor in serie.get((id_ente, bloco), {}).items() if exercicio != ano]
    if not outros:
        return False
    mediana = _mediana(outros)
    if abs(mediana) < 0.005:
        return abs(referencia) > 0.005
    razao = referencia / mediana
    return razao < params["salto_min"] or razao > params["salto_max"]


def _mediana(valores: list[float]) -> float:
    ordenados = sorted(valores)
    meio = len(ordenados) // 2
    if len(ordenados) % 2:
        return ordenados[meio]
    return (ordenados[meio - 1] + ordenados[meio]) / 2


def _pior_faixa(referencia, valores, params) -> str | None:
    if referencia is None:
        return None
    pior = None
    consideradas = [fonte for fonte in FONTES if fonte not in FONTES_PARCIAIS]
    if valores.get("EXTERNAS") is not None:
        consideradas.append("EXTERNAS")
    for fonte in consideradas:
        if fonte == "SICONFI" or valores.get(fonte) is None:
            continue
        faixa = _faixa(referencia, valores[fonte], params)
        if faixa == "acima":
            return "acima"
        if faixa == "1_5":
            pior = "1_5"
    return pior


def _faixa(referencia, valor, params) -> str:
    absoluta, relativa = _par(referencia, valor)
    if absoluta <= params["igualdade_absoluta"] or relativa <= params["aceite_sugerido"]:
        return "ok"
    if relativa <= params["fila_obrigatoria"]:
        return "1_5"
    return "acima"


def _itens_nao_somam(valores, partes, params) -> bool:
    for fonte in FONTES:
        total = valores[fonte]
        calculado = _soma_itens(partes[fonte])
        if total is None or calculado is None:
            continue
        if not _sao_iguais(calculado, total, params):
            return True
    return False


def _soma_itens(partes: dict) -> float | None:
    if not partes:
        return None
    total = 0.0
    algum = False
    for componente, valor in partes.items():
        papel = POR_ID[componente].papel
        if papel == "SOMA":
            total += valor
            algum = True
        elif papel == "SUBTRAI":
            total -= abs(valor)
            algum = True
    if not algum:
        return None
    return total


def _liquido_fundeb(referencia, valores, params) -> bool:
    if referencia is None or referencia <= 0:
        return False
    minimo = params["fundeb_liquido_min"]
    maximo = params["fundeb_liquido_max"]
    for fonte in FONTES:
        if fonte in ("SICONFI", "MSC", "RREO") or valores[fonte] is None or valores[fonte] <= 0:
            continue
        razao = valores[fonte] / referencia
        if minimo <= razao <= maximo:
            return True
    return False


def _uf_nao_fecha(estadual, soma, params) -> bool:
    return _faixa(estadual, soma, params) == "acima"


def _fecha(referencia, valores, partes, params) -> bool:
    if referencia is None:
        return False
    abertura = list(FONTES_ABERTURA)
    if valores.get("EXTERNAS") is not None:
        abertura.append("EXTERNAS")
    for fonte in abertura:
        outro = valores.get(fonte)
        if outro is None or not _dentro_do_aceite(referencia, outro, params):
            continue
        calculado = _soma_itens(partes.get(fonte) or {})
        if calculado is None or _sao_iguais(calculado, outro, params):
            return True
    return False


def _sugerir(referencia, valores, partes, params, fecha):
    if fecha:
        return "SICONFI", referencia
    presentes = {fonte: valor for fonte, valor in valores.items() if valor is not None}
    candidatos = []
    for fonte in FONTES_ABERTURA:
        if fonte not in presentes:
            continue
        if any(
            _dentro_do_aceite(presentes[fonte], outro, params)
            for nome, outro in presentes.items()
            if nome != fonte
        ):
            candidatos.append(fonte)
    candidatos = [fonte for fonte in candidatos if not _liquida_de_outra(fonte, candidatos, presentes, params)]
    if not candidatos:
        return None, None

    def chave(fonte):
        valor = presentes[fonte]
        distancias = [
            abs(valor - presentes[outra])
            for outra in presentes
            if outra != fonte and _dentro_do_aceite(valor, presentes[outra], params)
        ]
        return (min(distancias), PRIORIDADE[fonte])

    escolhida = min(candidatos, key=chave)
    return escolhida, presentes[escolhida]


def _liquida_de_outra(fonte, candidatos, presentes, params) -> bool:
    valor = presentes[fonte]
    if valor <= 0:
        return False
    minimo = params["fundeb_liquido_min"]
    maximo = params["fundeb_liquido_max"]
    for outra in candidatos:
        if outra == fonte:
            continue
        base = presentes[outra]
        if base <= 0:
            continue
        razao = valor / base
        if minimo <= razao <= maximo:
            return True
    return False


def _dentro_do_aceite(esquerda, direita, params) -> bool:
    absoluta, relativa = _par(esquerda, direita)
    return absoluta <= params["igualdade_absoluta"] or relativa <= params["aceite_sugerido"]


def _sao_iguais(esquerda, direita, params) -> bool:
    absoluta, relativa = _par(esquerda, direita)
    return absoluta <= params["igualdade_absoluta"] or relativa <= params["igualdade_relativa"]


def _par(esquerda, direita) -> tuple[float, float]:
    absoluta = abs(esquerda - direita)
    base = max(abs(esquerda), abs(direita))
    relativa = 0.0 if base == 0 else absoluta / base
    return absoluta, relativa


def _diferencas(referencia, valores, naturezas, fontes=None) -> dict:
    saida = {}
    for fonte in fontes or FONTES:
        valor = valores[fonte]
        if valor is None:
            saida[fonte] = None
            continue
        if referencia is None:
            absoluta, relativa = None, None
        else:
            absoluta, relativa = _par(referencia, valor)
        saida[fonte] = {
            "valor": valor,
            "diferenca_absoluta": absoluta,
            "diferenca_relativa": relativa,
            "natureza": naturezas[fonte],
        }
    return saida


def _ordenar(situacoes: set[str]) -> list[str]:
    return [codigo for codigo in ORDEM_SITUACOES if codigo in situacoes]
