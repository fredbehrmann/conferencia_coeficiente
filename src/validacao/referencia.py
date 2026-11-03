import csv
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from validacao.db import PASTA_RELATORIOS
from validacao.modelos import Carga, Ente, ValorObservado

ARQUIVO_RESIDUOS = PASTA_RELATORIOS / "residuos_siconfi.csv"

METAS = {
    "ISS": {"total": 38775, "minimo": 0.991},
    "COTA_RECEBIDA": {"total": 38775, "minimo": 1.0},
    "ICMS_ESTADO": {"total": 182, "minimo": 1.0},
}


def calcular_iss(partes: dict) -> float:
    return (
        partes.get("ISS_BRUTO", 0.0)
        + partes.get("ISS_FECOP", 0.0)
        - abs(partes.get("ISS_OUTRAS_DEDUCOES", 0.0))
        - abs(partes.get("ISS_FECOP_OUTRAS_DEDUCOES", 0.0))
    )


def calcular_cota_recebida(partes: dict) -> float:
    return partes.get("COTA_RECEBIDA_BRUTO", 0.0) - abs(partes.get("COTA_RECEBIDA_OUTRAS_DEDUCOES", 0.0))


def calcular_icms(partes: dict) -> float:
    return (
        partes.get("ICMS_BRUTO", 0.0)
        + partes.get("ICMS_FECOP", 0.0)
        - abs(partes.get("ICMS_OUTRAS_DEDUCOES", 0.0))
        - abs(partes.get("ICMS_FECOP_OUTRAS_DEDUCOES", 0.0))
    )


def calcular_cota_repassada(partes: dict) -> float | None:
    if "ICMS_DEDUCAO_CONST" not in partes:
        return None
    return partes["ICMS_DEDUCAO_CONST"]


def iguais(calculado, declarado) -> bool:
    if calculado is None or declarado is None:
        return False
    diferenca = abs(calculado - declarado)
    base = max(abs(calculado), abs(declarado))
    return diferenca <= 1.0 or diferenca <= 0.0001 * base


def avaliar(session: Session) -> tuple[dict, list[dict]]:
    carga_id = session.scalar(
        select(func.max(Carga.id)).where(Carga.fonte_id == "SICONFI", Carga.situacao == "CONCLUIDA")
    )
    if carga_id is None:
        raise RuntimeError("Não há carga concluída do SICONFI.")

    linhas = session.execute(
        select(
            ValorObservado.id_ente,
            ValorObservado.ano,
            ValorObservado.componente_id,
            ValorObservado.valor,
            Ente.esfera,
        )
        .join(Ente, Ente.id_ente == ValorObservado.id_ente)
        .where(ValorObservado.carga_id == carga_id)
    ).all()

    grupos = defaultdict(dict)
    esferas = {}
    for id_ente, ano, componente, valor, esfera in linhas:
        grupos[(id_ente, ano)][componente] = valor
        esferas[id_ente] = esfera

    regras = (
        ("ISS", {"MUNICIPIO"}, "ISS_TOTAL", calcular_iss),
        ("COTA_RECEBIDA", {"MUNICIPIO"}, "COTA_RECEBIDA_TOTAL", calcular_cota_recebida),
        ("ICMS", {"ESTADO", "DF"}, "ICMS_TOTAL", calcular_icms),
        ("COTA_REPASSADA", {"ESTADO"}, "COTA_REPASSADA", calcular_cota_repassada),
    )
    contagem = defaultdict(lambda: {"total": 0, "iguais": 0})
    residuos = []
    for (id_ente, ano), partes in grupos.items():
        esfera = esferas[id_ente]
        for bloco, esferas_bloco, total_id, formula in regras:
            if esfera not in esferas_bloco or total_id not in partes:
                continue
            declarado = partes[total_id]
            calculado = formula(partes)
            chave = f"{bloco}_DF" if bloco == "ICMS" and esfera == "DF" else bloco
            if bloco == "ICMS" and esfera == "ESTADO":
                chave = "ICMS_ESTADO"
            contagem[chave]["total"] += 1
            if iguais(calculado, declarado):
                contagem[chave]["iguais"] += 1
                continue
            residuos.append(
                {
                    "id_ente": id_ente,
                    "ano": ano,
                    "bloco": bloco,
                    "valor_calculado": calculado,
                    "valor_declarado": declarado,
                    "diferenca": None if calculado is None else calculado - declarado,
                }
            )
    residuos.sort(key=lambda item: (item["bloco"], item["id_ente"], item["ano"]))
    return dict(contagem), residuos


def gerar_relatorio(session: Session, destino: Path | None = None) -> dict:
    contagem, residuos = avaliar(session)
    caminho = Path(destino) if destino is not None else ARQUIVO_RESIDUOS
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(
            ["id_ente", "ano", "bloco", "valor_calculado", "valor_declarado", "diferenca"]
        )
        for item in residuos:
            escritor.writerow(
                [
                    item["id_ente"],
                    item["ano"],
                    item["bloco"],
                    _numero(item["valor_calculado"]),
                    _numero(item["valor_declarado"]),
                    _numero(item["diferenca"]),
                ]
            )
    contagem["linhas_residuo"] = len(residuos)
    contagem["arquivo"] = str(caminho)
    return contagem


def metas_atingidas(contagem: dict) -> bool:
    for chave, meta in METAS.items():
        bloco = contagem.get(chave)
        if bloco is None or bloco["total"] != meta["total"]:
            return False
        if meta["minimo"] >= 1:
            if bloco["iguais"] != bloco["total"]:
                return False
        elif bloco["iguais"] / bloco["total"] < meta["minimo"]:
            return False
    return True


def texto_resumo(contagem: dict) -> str:
    linhas = []
    for chave in ("ISS", "COTA_RECEBIDA", "ICMS_ESTADO", "ICMS_DF", "COTA_REPASSADA"):
        bloco = contagem.get(chave)
        if not bloco or not isinstance(bloco, dict) or "total" not in bloco:
            continue
        taxa = 0 if bloco["total"] == 0 else 100 * bloco["iguais"] / bloco["total"]
        linhas.append(f"{chave}: {bloco['iguais']}/{bloco['total']} ({taxa:.2f}%)")
    linhas.append(f"Resíduos: {contagem.get('linhas_residuo', 0)} em {contagem.get('arquivo', '')}")
    return "\n".join(linhas)


def _numero(valor):
    if valor is None:
        return ""
    return f"{valor:.6f}"
