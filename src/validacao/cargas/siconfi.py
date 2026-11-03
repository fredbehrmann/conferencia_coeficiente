from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from validacao.cadastro import esfera_de, registrar_codigos, tribunal_de, uf_de
from validacao.catalogo import permitidos
from validacao.cargas.comum import (
    abrir_carga,
    acrescentar,
    carga_concluida,
    codigo_inteiro,
    data_do_arquivo,
    gravar_valores,
    hash_arquivo,
    numero,
    texto,
)
from validacao.modelos import Ente

ARQUIVO = "SICONFI_DCA_Receitas_2019_2025.xlsx"

BRUTO = {
    "ICMS_BRUTO": "ICMS_BRUTO",
    "ICMS_DEDUCOES_CONST": "ICMS_DEDUCAO_CONST",
    "ICMS_FUNDEB": "ICMS_FUNDEB",
    "ICMS_DEDUCOES_OUTRAS": "ICMS_OUTRAS_DEDUCOES",
    "ICMS_FECOP_BRUTO": "ICMS_FECOP",
    "ICMS_FECOP_DEDUCOES_CONST": "ICMS_FECOP_DEDUCAO_CONST",
    "ICMS_FECOP_FUNDEB": "ICMS_FECOP_FUNDEB",
    "ICMS_FECOP_DEDUCOES_OUTRAS": "ICMS_FECOP_OUTRAS_DEDUCOES",
    "ISS_BRUTO": "ISS_BRUTO",
    "ISS_DEDUCOES_CONST": "ISS_DEDUCAO_CONST",
    "ISS_FUNDEB": "ISS_FUNDEB",
    "ISS_DEDUCOES_OUTRAS": "ISS_OUTRAS_DEDUCOES",
    "ISS_FECOP_BRUTO": "ISS_FECOP",
    "ISS_FECOP_DEDUCOES_CONST": "ISS_FECOP_DEDUCAO_CONST",
    "ISS_FECOP_FUNDEB": "ISS_FECOP_FUNDEB",
    "ISS_FECOP_DEDUCOES_OUTRAS": "ISS_FECOP_OUTRAS_DEDUCOES",
    "COTA_RECEBIDA_BRUTO": "COTA_RECEBIDA_BRUTO",
    "COTA_RECEBIDA_DEDUCOES_CONST": "COTA_RECEBIDA_DEDUCAO_CONST",
    "COTA_RECEBIDA_FUNDEB": "COTA_RECEBIDA_FUNDEB",
    "COTA_RECEBIDA_DEDUCOES_OUTRAS": "COTA_RECEBIDA_OUTRAS_DEDUCOES",
    "COMPENSACOES_FINANCEIRAS_BRUTO": "COMPENSACAO_BRUTO",
    "COMPENSACOES_FINANCEIRAS_DEDUCOES_CONST": "COMPENSACAO_DEDUCAO_CONST",
    "COMPENSACOES_FINANCEIRAS_FUNDEB": "COMPENSACAO_FUNDEB",
    "COMPENSACOES_FINANCEIRAS_DEDUCOES_OUTRAS": "COMPENSACAO_OUTRAS_DEDUCOES",
    "COTA_COMPENSACOES_FIN_BRUTO": "COMPENSACAO_RECEBIDA_BRUTO",
    "COTA_COMPENSACOES_FIN_DEDUCOES_CONST": "COMPENSACAO_RECEBIDA_DEDUCAO_CONST",
    "COTA_COMPENSACOES_FIN_FUNDEB": "COMPENSACAO_RECEBIDA_FUNDEB",
    "COTA_COMPENSACOES_FIN_DEDUCOES_OUTRAS": "COMPENSACAO_RECEBIDA_OUTRAS_DEDUCOES",
}

PROCESSADO = {
    "ISS": "ISS_TOTAL",
    "COTA_MUN": "COTA_RECEBIDA_TOTAL",
    "ICMS": "ICMS_TOTAL",
    "COTA_EST": "COTA_REPASSADA",
}


def carregar(session: Session, caminho: Path) -> dict:
    hash_hex = hash_arquivo(caminho)
    if carga_concluida(session, hash_hex) is not None:
        return {"fonte": "SICONFI", "situacao": "JA_CARREGADA", "valores": 0}

    print("Lendo SICONFI...", flush=True)
    processado = pd.read_excel(caminho, sheet_name="SICONFI")
    bruto = pd.read_excel(caminho, sheet_name="SICONFI_BRUTO")
    entes = _entes(session, processado)
    registrar_codigos(session, entes)

    mapa = {}
    exercicios = set()
    _acumular(mapa, exercicios, processado.to_dict("records"), PROCESSADO, 2, True)
    _acumular(mapa, exercicios, bruto.to_dict("records"), BRUTO, 2, False)
    carga = abrir_carga(session, "SICONFI", caminho, hash_hex, data_do_arquivo(caminho), exercicios)
    quantidade = gravar_valores(session, carga.id, mapa)
    return {
        "fonte": "SICONFI",
        "situacao": "CARREGADA",
        "valores": quantidade,
        "entes_ano": len({(chave[0], chave[1]) for chave in mapa}),
    }


def _acumular(mapa, exercicios, registros, colunas, linha_inicial, com_compensacao):
    for deslocamento, registro in enumerate(registros):
        id_ente = codigo_inteiro(registro.get("COD_IBGE"))
        ano = codigo_inteiro(registro.get("ANO"))
        if id_ente is None or ano is None:
            continue
        esfera = esfera_de(registro.get("ESFERA"))
        exercicios.add(ano)
        liberados = permitidos(esfera)
        linha = linha_inicial + deslocamento
        for origem, componente in colunas.items():
            acrescentar(
                mapa,
                id_ente,
                ano,
                componente,
                numero(registro.get(origem)),
                None,
                origem,
                linha,
                liberados,
            )
        if not com_compensacao:
            continue
        origem = "COTA_COMPEN_FIN" if esfera == "MUNICIPIO" else "COMPEN_FIN"
        acrescentar(
            mapa,
            id_ente,
            ano,
            "COMPENSACAO",
            numero(registro.get(origem)),
            None,
            origem,
            linha,
            liberados,
        )
        if esfera == "ESTADO":
            acrescentar(
                mapa,
                id_ente,
                ano,
                "COMPENSACAO_REPASSADA",
                numero(registro.get("COTA_COMPEN_FIN")),
                None,
                "COTA_COMPEN_FIN",
                linha,
                liberados,
            )


def _entes(session: Session, processado: pd.DataFrame) -> list[Ente]:
    mais_recente: dict[int, tuple[int, str, str]] = {}
    for registro in processado.to_dict("records"):
        id_ente = codigo_inteiro(registro.get("COD_IBGE"))
        ano = codigo_inteiro(registro.get("ANO"))
        if id_ente is None or ano is None:
            continue
        esfera = esfera_de(registro.get("ESFERA"))
        nome = texto(registro.get("ENTE")) or f"Ente {id_ente}"
        atual = mais_recente.get(id_ente)
        if atual is None or ano >= atual[0]:
            mais_recente[id_ente] = (ano, nome, esfera)

    existentes = set(session.scalars(select_ids()))
    novos = []
    for id_ente, (_ano, nome, esfera) in mais_recente.items():
        if id_ente in existentes:
            continue
        uf = uf_de(esfera, id_ente)
        ente = Ente(
            id_ente=id_ente,
            esfera=esfera,
            uf=uf,
            nome=nome,
            capital=id_ente in _capitais(),
            tribunal_competente=tribunal_de(esfera, uf, id_ente),
        )
        session.add(ente)
        novos.append(ente)
    session.flush()
    return novos


def select_ids():
    from sqlalchemy import select

    return select(Ente.id_ente)


def _capitais():
    from validacao.cadastro import CAPITAIS

    return CAPITAIS
