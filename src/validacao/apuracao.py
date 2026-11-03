import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from validacao.db import PASTA_RELATORIOS
from validacao.modelos import (
    Apuracao,
    BaseLinha,
    BaseVersao,
    Carga,
    Decisao,
    Ente,
    FormulaVersao,
    ValorObservado,
    Verificacao,
)
from validacao.motor import TOTAL_DO_BLOCO, VERMELHAS
from validacao.regras import garantir_regra, parametros_de

BLOCOS_INICIAIS = {
    "MUNICIPIO": ["ISS", "COTA_RECEBIDA"],
    "ESTADO": ["ICMS"],
    "DF": ["ISS", "ICMS"],
}
FORMULA_INICIAL = {
    "anos": [2019, 2020, 2021, 2022, 2023, 2024, 2025],
    "media": "aritmetica",
    "fatores": {},
    "agregacao": "por_esfera",
    "fecha_em": 1,
    "casas": 10,
    "blocos": BLOCOS_INICIAIS,
}


class CongelamentoRecusado(ValueError):
    def __init__(self, pendentes: int, vermelhos: int, sem_revisao: int):
        self.pendentes = pendentes
        self.vermelhos = vermelhos
        self.sem_revisao = sem_revisao
        super().__init__(
            f"A base não fecha: {pendentes} blocos sem decisão, "
            f"{vermelhos} em vermelho sem decisão, {sem_revisao} sem a revisão exigida."
        )


def impedimentos(session: Session) -> dict[str, int]:
    regra = garantir_regra(session)
    corte = parametros_de(regra).get("revisao_corte")
    vigente = (
        select(Decisao.id)
        .where(
            Decisao.id_ente == Verificacao.id_ente,
            Decisao.ano == Verificacao.ano,
            Decisao.bloco == Verificacao.bloco,
            Decisao.estado == "VIGENTE",
        )
        .exists()
    )
    aplicavel = (
        Verificacao.regra_versao_id == regra.id,
        ~Verificacao.situacoes.contains('"NAO_SE_APLICA"'),
    )
    pendentes = session.scalar(
        select(func.count()).select_from(Verificacao).where(*aplicavel, ~vigente)
    ) or 0
    vermelhos = session.scalar(
        select(func.count())
        .select_from(Verificacao)
        .where(*aplicavel, ~vigente, or_(*(Verificacao.situacoes.contains(f'"{codigo}"') for codigo in VERMELHAS)))
    ) or 0
    condicao = (Ente.esfera.in_(("ESTADO", "DF"))) | (Ente.capital.is_(True))
    if corte is not None:
        condicao = condicao | (func.abs(Decisao.valor) >= float(corte))
    sem_revisao = session.scalar(
        select(func.count())
        .select_from(Decisao)
        .join(Ente, Ente.id_ente == Decisao.id_ente)
        .join(
            Verificacao,
            and_(
                Verificacao.id_ente == Decisao.id_ente,
                Verificacao.ano == Decisao.ano,
                Verificacao.bloco == Decisao.bloco,
                Verificacao.regra_versao_id == regra.id,
            ),
        )
        .where(
            Decisao.estado == "VIGENTE",
            Decisao.revisor_id.is_(None),
            ~Verificacao.situacoes.contains('"NAO_SE_APLICA"'),
            condicao,
        )
    ) or 0
    return {"pendentes": pendentes, "vermelhos": vermelhos, "sem_revisao": sem_revisao}


def congelar(session: Session, responsavel_id: int) -> BaseVersao:
    contagem = impedimentos(session)
    if any(contagem.values()):
        raise CongelamentoRecusado(contagem["pendentes"], contagem["vermelhos"], contagem["sem_revisao"])
    regra = garantir_regra(session)
    linhas = []
    for verificacao, ente, decisao in _universo(session, regra.id):
        if decisao is None:
            raise CongelamentoRecusado(1, 0, 0)
        linhas.append(
            {
                "id_ente": verificacao.id_ente,
                "esfera": ente.esfera,
                "ano": verificacao.ano,
                "bloco": verificacao.bloco,
                "valor": None if decisao.valor is None else f"{decisao.valor:.2f}",
                "fonte": decisao.fonte,
                "decisao_id": decisao.id,
            }
        )
    linhas.sort(key=lambda item: (item["id_ente"], item["ano"], item["bloco"]))
    resumo = _hash_base(regra.id, linhas)
    existente = session.scalar(select(BaseVersao).where(BaseVersao.hash == resumo))
    if existente is not None:
        return existente
    versao = BaseVersao(
        regra_versao_id=regra.id,
        data_congelamento=datetime.now().replace(microsecond=0),
        hash=resumo,
        responsavel_id=responsavel_id,
    )
    session.add(versao)
    session.flush()
    for item in linhas:
        session.add(
            BaseLinha(
                base_versao_id=versao.id,
                id_ente=item["id_ente"],
                esfera=item["esfera"],
                ano=item["ano"],
                bloco=item["bloco"],
                valor=None if item["valor"] is None else float(item["valor"]),
                fonte=item["fonte"],
                decisao_id=item["decisao_id"],
            )
        )
    session.flush()
    return versao


def garantir_formula(session: Session) -> FormulaVersao:
    existente = session.scalar(
        select(FormulaVersao)
        .where(FormulaVersao.vigente_desde.is_not(None))
        .order_by(FormulaVersao.id.desc())
        .limit(1)
    )
    if existente is not None:
        return existente
    formula = FormulaVersao(
        vigente_desde=datetime.now().replace(microsecond=0),
        descricao="Média aritmética por esfera",
        parametros=json.dumps(FORMULA_INICIAL, ensure_ascii=False),
    )
    session.add(formula)
    session.flush()
    return formula


def gravar_formula(session: Session, descricao: str, parametros: dict) -> FormulaVersao:
    _validar_parametros(parametros)
    formula = FormulaVersao(
        vigente_desde=datetime.now().replace(microsecond=0),
        descricao=descricao.strip() or "Fórmula",
        parametros=json.dumps(parametros, ensure_ascii=False),
    )
    session.add(formula)
    session.flush()
    return formula


def parametros_formula(formula: FormulaVersao) -> dict:
    dados = json.loads(formula.parametros)
    dados.setdefault("fatores", {})
    dados.setdefault("casas", 10)
    dados.setdefault("fecha_em", 1)
    return dados


def apurar(session: Session, base_id: int, formula_id: int | None = None) -> list[Apuracao]:
    formula = session.get(FormulaVersao, formula_id) if formula_id else garantir_formula(session)
    if formula is None:
        raise ValueError("Fórmula não encontrada.")
    existentes = session.scalars(
        select(Apuracao)
        .where(Apuracao.base_versao_id == base_id, Apuracao.formula_versao_id == formula.id)
        .order_by(Apuracao.id_ente)
    ).all()
    if existentes:
        return list(existentes)
    base = session.get(BaseVersao, base_id)
    if base is None:
        raise ValueError("Base congelada não encontrada.")
    params = parametros_formula(formula)
    _validar_parametros(params)
    linhas = session.scalars(select(BaseLinha).where(BaseLinha.base_versao_id == base_id)).all()
    ids = {linha.decisao_id for linha in linhas}
    decisoes = {}
    if ids:
        decisoes = {
            decisao.id: decisao
            for decisao in session.scalars(select(Decisao).where(Decisao.id.in_(ids)))
        }
    entes = _preparar_ente(session, linhas, decisoes, params)
    por_esfera: dict[str, list[dict]] = {}
    for item in entes.values():
        por_esfera.setdefault(item["esfera"], []).append(item)
    gravadas = []
    for esfera, grupo in por_esfera.items():
        coeficientes = _coeficientes(grupo, params)
        for item in grupo:
            registro = Apuracao(
                base_versao_id=base_id,
                formula_versao_id=formula.id,
                id_ente=item["id_ente"],
                coeficiente=float(coeficientes[item["id_ente"]]),
                memoria=json.dumps(
                    {
                        "anos": item["anos"],
                        "base_ente": float(item["base"]),
                        "base_esfera": float(item["base_esfera"]),
                        "coeficiente": float(coeficientes[item["id_ente"]]),
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(registro)
            gravadas.append(registro)
    session.flush()
    gravadas.sort(key=lambda item: item.id_ente)
    return gravadas


def comparar(session: Session, base_a: int, base_b: int, formula_id: int) -> list[dict]:
    apurar(session, base_a, formula_id)
    apurar(session, base_b, formula_id)
    coef_a = _coeficientes_gravados(session, base_a, formula_id)
    coef_b = _coeficientes_gravados(session, base_b, formula_id)
    linhas_a = _linhas_por_ente(session, base_a)
    linhas_b = _linhas_por_ente(session, base_b)
    mudancas = []
    for id_ente in sorted(set(coef_a) | set(coef_b)):
        if coef_a.get(id_ente) == coef_b.get(id_ente):
            continue
        mudancas.append(
            {
                "id_ente": id_ente,
                "coeficiente_a": coef_a.get(id_ente),
                "coeficiente_b": coef_b.get(id_ente),
                "decisoes": _decisoes_diferentes(linhas_a.get(id_ente, {}), linhas_b.get(id_ente, {})),
            }
        )
    return mudancas


def exportar(session: Session, base_id: int, formula_id: int, formato: str) -> Path:
    base = session.get(BaseVersao, base_id)
    if base is None:
        raise ValueError("Base congelada não encontrada.")
    apurar(session, base_id, formula_id)
    linhas = session.execute(
        select(BaseLinha, Ente.nome)
        .join(Ente, Ente.id_ente == BaseLinha.id_ente)
        .where(BaseLinha.base_versao_id == base_id)
        .order_by(BaseLinha.id_ente, BaseLinha.ano, BaseLinha.bloco)
    ).all()
    coeficientes = session.execute(
        select(Apuracao, Ente.nome, Ente.esfera)
        .join(Ente, Ente.id_ente == Apuracao.id_ente)
        .where(Apuracao.base_versao_id == base_id, Apuracao.formula_versao_id == formula_id)
        .order_by(Ente.esfera, Ente.nome)
    ).all()
    PASTA_RELATORIOS.mkdir(parents=True, exist_ok=True)
    destino = PASTA_RELATORIOS / f"base-{base_id}-formula-{formula_id}.{formato}"
    base_linhas = [
        {
            "versao": base.id,
            "hash": base.hash,
            "id_ente": linha.id_ente,
            "nome": nome,
            "esfera": linha.esfera,
            "ano": linha.ano,
            "bloco": linha.bloco,
            "valor": linha.valor,
            "fonte": linha.fonte,
            "decisao_id": linha.decisao_id,
        }
        for linha, nome in linhas
    ]
    coef_linhas = [
        {
            "versao": base.id,
            "hash": base.hash,
            "formula": formula_id,
            "id_ente": apuracao.id_ente,
            "nome": nome,
            "esfera": esfera,
            "coeficiente": apuracao.coeficiente,
        }
        for apuracao, nome, esfera in coeficientes
    ]
    if formato == "csv":
        _csv(destino, base_linhas, coef_linhas)
    elif formato == "xlsx":
        _xlsx(destino, base.hash, base_linhas, coef_linhas)
    else:
        raise ValueError("Formato desconhecido.")
    return destino


def _universo(session: Session, regra_id: int):
    return session.execute(
        select(Verificacao, Ente, Decisao)
        .join(Ente, Ente.id_ente == Verificacao.id_ente)
        .outerjoin(
            Decisao,
            and_(
                Decisao.id_ente == Verificacao.id_ente,
                Decisao.ano == Verificacao.ano,
                Decisao.bloco == Verificacao.bloco,
                Decisao.estado == "VIGENTE",
            ),
        )
        .where(
            Verificacao.regra_versao_id == regra_id,
            ~Verificacao.situacoes.contains('"NAO_SE_APLICA"'),
        )
        .order_by(Verificacao.id_ente, Verificacao.ano, Verificacao.bloco)
    ).all()


def _hash_base(regra_id: int, linhas: list[dict]) -> str:
    payload = json.dumps(
        {"regra_versao_id": regra_id, "linhas": linhas},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _validar_parametros(params: dict) -> None:
    if params.get("media") != "aritmetica":
        raise ValueError("A fórmula só interpreta a média aritmética.")
    if params.get("agregacao") != "por_esfera":
        raise ValueError("A fórmula só interpreta a agregação por esfera.")
    if "blocos" not in params or "anos" not in params:
        raise ValueError("A fórmula precisa de anos e blocos.")


def _preparar_ente(session, linhas, decisoes, params) -> dict[int, dict]:
    anos = {int(ano) for ano in params["anos"]}
    fatores = {int(ano): Decimal(str(fator)) for ano, fator in params.get("fatores", {}).items()}
    agrupado: dict[int, dict] = {}
    for linha in linhas:
        if linha.ano not in anos or linha.bloco not in params["blocos"].get(linha.esfera, []):
            continue
        ente = agrupado.setdefault(
            linha.id_ente,
            {"id_ente": linha.id_ente, "esfera": linha.esfera, "por_ano": {}},
        )
        blocos = ente["por_ano"].setdefault(linha.ano, [])
        decisao = decisoes[linha.decisao_id]
        blocos.append(
            {
                "bloco": linha.bloco,
                "valor": linha.valor,
                "decisao_id": linha.decisao_id,
                "fonte": linha.fonte,
                "cargas": _rastros(session, decisao, linha),
            }
        )
    saida = {}
    for id_ente, ente in agrupado.items():
        memoria_anos = []
        somas = []
        for ano in sorted(ente["por_ano"]):
            blocos = ente["por_ano"][ano]
            presentes = [Decimal(str(item["valor"])) for item in blocos if item["valor"] is not None]
            valor_ano = None if not presentes else sum(presentes, Decimal(0)) * fatores.get(ano, Decimal(1))
            if valor_ano is not None:
                somas.append(valor_ano)
            memoria_anos.append(
                {
                    "ano": ano,
                    "valor": None if valor_ano is None else float(valor_ano),
                    "blocos": blocos,
                }
            )
        base = Decimal(0) if not somas else sum(somas, Decimal(0)) / Decimal(len(somas))
        saida[id_ente] = {
            "id_ente": id_ente,
            "esfera": ente["esfera"],
            "anos": memoria_anos,
            "base": base,
        }
    return saida


def _rastros(session: Session, decisao: Decisao, linha: BaseLinha) -> list[dict]:
    usadas = json.loads(decisao.cargas_usadas or "{}")
    if isinstance(usadas, list):
        usadas = {}
    total = TOTAL_DO_BLOCO.get(linha.bloco)
    rastros = []
    for fonte, carga_id in sorted(usadas.items()):
        observado = None
        if total is not None:
            observado = session.scalar(
                select(ValorObservado).where(
                    ValorObservado.carga_id == int(carga_id),
                    ValorObservado.id_ente == linha.id_ente,
                    ValorObservado.ano == linha.ano,
                    ValorObservado.componente_id == total,
                )
            )
        carga = session.get(Carga, int(carga_id))
        rastros.append(
            {
                "fonte": carga.fonte_id if carga is not None else fonte,
                "carga_id": int(carga_id),
                "linha_origem": None if observado is None else observado.linha_origem,
                "componente_id": total,
            }
        )
    return rastros


def _coeficientes(grupo: list[dict], params: dict) -> dict[int, Decimal]:
    casas = int(params["casas"])
    fecha_em = Decimal(str(params["fecha_em"]))
    total = sum((item["base"] for item in grupo), Decimal(0))
    for item in grupo:
        item["base_esfera"] = total
    if total == 0:
        return {item["id_ente"]: Decimal(0) for item in grupo}
    brutos = [(item["id_ente"], item["base"] / total * fecha_em) for item in grupo]
    return _maior_resto(brutos, fecha_em, casas)


def _maior_resto(brutos: list[tuple[int, Decimal]], fecha_em: Decimal, casas: int) -> dict[int, Decimal]:
    unidade = Decimal(1).scaleb(-casas)
    pisos = {}
    restos = []
    soma = Decimal(0)
    for id_ente, bruto in brutos:
        piso = bruto.quantize(unidade, rounding=ROUND_DOWN)
        pisos[id_ente] = piso
        soma += piso
        restos.append((bruto - piso, id_ente))
    faltam = int(((fecha_em - soma) / unidade).to_integral_value(rounding=ROUND_DOWN))
    restos.sort(key=lambda item: (-item[0], item[1]))
    for indice in range(max(faltam, 0)):
        pisos[restos[indice % len(restos)][1]] += unidade
    return pisos


def _coeficientes_gravados(session: Session, base_id: int, formula_id: int) -> dict[int, float]:
    linhas = session.execute(
        select(Apuracao.id_ente, Apuracao.coeficiente).where(
            Apuracao.base_versao_id == base_id,
            Apuracao.formula_versao_id == formula_id,
        )
    ).all()
    return {int(ente): float(coeficiente) for ente, coeficiente in linhas}


def _linhas_por_ente(session: Session, base_id: int) -> dict[int, dict]:
    saida: dict[int, dict] = {}
    for linha in session.scalars(select(BaseLinha).where(BaseLinha.base_versao_id == base_id)):
        saida.setdefault(linha.id_ente, {})[(linha.ano, linha.bloco)] = linha
    return saida


def _decisoes_diferentes(linhas_a: dict, linhas_b: dict) -> list[dict]:
    chaves = sorted(set(linhas_a) | set(linhas_b))
    diferencas = []
    for chave in chaves:
        esquerda = linhas_a.get(chave)
        direita = linhas_b.get(chave)
        id_a = None if esquerda is None else esquerda.decisao_id
        id_b = None if direita is None else direita.decisao_id
        if id_a == id_b:
            continue
        diferencas.append({"ano": chave[0], "bloco": chave[1], "decisao_a": id_a, "decisao_b": id_b})
    return diferencas


def _csv(destino: Path, base_linhas: list[dict], coef_linhas: list[dict]) -> None:
    with destino.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=list(base_linhas[0]) if base_linhas else ["versao", "hash"])
        escritor.writeheader()
        escritor.writerows(base_linhas)
        arquivo.write("\n")
        if coef_linhas:
            coef = csv.DictWriter(arquivo, fieldnames=list(coef_linhas[0]))
            coef.writeheader()
            coef.writerows(coef_linhas)


def _xlsx(destino: Path, resumo: str, base_linhas: list[dict], coef_linhas: list[dict]) -> None:
    livro = Workbook()
    aba = livro.active
    aba.title = "Base"
    _aba(aba, base_linhas)
    coef = livro.create_sheet("Coeficientes")
    _aba(coef, coef_linhas)
    capa = livro.create_sheet("Versao", 0)
    capa.append(["hash", resumo])
    livro.save(destino)


def _aba(aba, linhas: list[dict]) -> None:
    if not linhas:
        return
    aba.append(list(linhas[0]))
    for linha in linhas:
        aba.append(list(linha.values()))
