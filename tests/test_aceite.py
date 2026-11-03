from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from validacao.cadastro import mapa_de_codigos
from validacao.cargas import carregar_pasta
from validacao.cargas.comum import ler_planilha
from validacao.catalogo import COMPONENTES
from validacao.db import criar_engine, criar_tabelas
from validacao.modelos import Carga, Componente, Ente, RegraVersao, ValorObservado
from validacao.referencia import calcular_iss, gerar_relatorio, metas_atingidas

PASTA = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def banco(tmp_path_factory):
    destino = tmp_path_factory.mktemp("banco") / "validacao.sqlite"
    engine = criar_engine(destino)
    criar_tabelas(engine)
    carregar_pasta(engine, PASTA)
    return engine


def test_formula_do_iss_de_salvador_2024():
    valor = calcular_iss({"ISS_BRUTO": 2170374138.62, "ISS_OUTRAS_DEDUCOES": 191271796.31})
    assert abs(valor - 1979102342.31) < 0.01


def test_siconfi_nao_tem_pares_duplicados(banco):
    with Session(banco) as session:
        carga = session.scalar(select(Carga).where(Carga.fonte_id == "SICONFI"))
        pares = session.scalar(
            select(func.count()).select_from(
                select(ValorObservado.id_ente, ValorObservado.ano)
                .where(ValorObservado.carga_id == carga.id)
                .distinct()
                .subquery()
            )
        )
    assert pares == 38964


def test_cadastro_e_codigos_externos(banco):
    with Session(banco) as session:
        municipios = session.scalar(select(func.count()).select_from(Ente).where(Ente.esfera == "MUNICIPIO"))
        estados = session.scalar(select(func.count()).select_from(Ente).where(Ente.esfera == "ESTADO"))
        distritos = session.scalar(select(func.count()).select_from(Ente).where(Ente.esfera == "DF"))
        regras = session.scalar(select(func.count()).select_from(RegraVersao))
        salvador = session.get(Ente, 2927408)
        componentes = session.scalar(select(func.count()).select_from(Componente))
        siops = mapa_de_codigos(session, "SIOPS")
        siope = mapa_de_codigos(session, "SIOPE")
    assert municipios >= 5569
    assert estados == 26
    assert distritos == 1
    assert regras == 0
    assert componentes == len(COMPONENTES)
    assert salvador.capital
    assert salvador.tribunal_competente == "TCM-BA"
    assert _codigos_municipais("SIOPS - ICMS e ISS.xlsx", "Municipios") <= set(siops)
    assert siops["530010"] == 53
    assert len(_codigos_municipais("SIOPS - ICMS e ISS.xlsx", "Municipios")) == 5570
    assert _codigos_municipais("Dados SIOPE - ICMS e ISS.xlsx", "Municipios") <= set(siope)
    assert len(_codigos_municipais("Dados SIOPE - ICMS e ISS.xlsx", "Municipios")) == 5568


def test_siope_nao_tem_valor_dos_municipios_ausentes(banco):
    with Session(banco) as session:
        carga = session.scalar(select(Carga).where(Carga.fonte_id == "SIOPE"))
        for id_ente in (2410504, 5101837):
            quantidade = session.scalar(
                select(func.count())
                .select_from(ValorObservado)
                .where(ValorObservado.carga_id == carga.id, ValorObservado.id_ente == id_ente)
            )
            assert session.get(Ente, id_ente) is not None
            assert quantidade == 0


def test_tce_liga_3504_municipios_e_preserva_natureza(banco):
    quadro = ler_planilha(PASTA / "TCE Arrecadacao Municipios - 2019_2025.xlsx", sheet_name="ISS", header=3)
    codigos = {int(codigo) for codigo in quadro["Cód. IBGE"].dropna()}
    assert len(codigos) == 3504
    with Session(banco) as session:
        tce = mapa_de_codigos(session, "TCE")
        carga = session.scalar(select(Carga).where(Carga.fonte_id == "TCE"))
        natureza = session.scalar(
            select(ValorObservado.natureza).where(
                ValorObservado.carga_id == carga.id,
                ValorObservado.id_ente == 2300101,
                ValorObservado.ano == 2019,
                ValorObservado.componente_id == "ISS_TOTAL",
            )
        )
    assert codigos <= {int(codigo) for codigo in tce}
    assert natureza == "Bruto publicado"


def test_df_nao_gera_cota_e_resende_separa_zero_de_branco(banco):
    with Session(banco) as session:
        carga = session.scalar(select(Carga).where(Carga.fonte_id == "SICONFI"))
        cotas_df = session.scalar(
            select(func.count())
            .select_from(ValorObservado)
            .where(
                ValorObservado.carga_id == carga.id,
                ValorObservado.id_ente == 53,
                ValorObservado.componente_id.in_(("COTA_RECEBIDA_TOTAL", "COTA_REPASSADA")),
            )
        )
        iss_resende = session.scalar(
            select(ValorObservado.valor).where(
                ValorObservado.carga_id == carga.id,
                ValorObservado.id_ente == 3304201,
                ValorObservado.ano == 2022,
                ValorObservado.componente_id == "ISS_TOTAL",
            )
        )
        bruto_resende = session.scalar(
            select(func.count())
            .select_from(ValorObservado)
            .where(
                ValorObservado.carga_id == carga.id,
                ValorObservado.id_ente == 3304201,
                ValorObservado.ano == 2022,
                ValorObservado.componente_id == "ISS_BRUTO",
            )
        )
    assert cotas_df == 0
    assert iss_resende == 0
    assert bruto_resende == 0


def test_recarregar_o_mesmo_arquivo_nao_duplica(banco):
    antes = _contagens(banco)
    carregar_pasta(banco, PASTA)
    assert _contagens(banco) == antes


def test_reproducao_do_valor_de_referencia(banco, tmp_path):
    with Session(banco) as session:
        contagem = gerar_relatorio(session, tmp_path / "residuos_siconfi.csv")
    assert metas_atingidas(contagem)
    texto = (tmp_path / "residuos_siconfi.csv").read_text(encoding="utf-8-sig")
    assert texto.startswith("id_ente,ano,bloco,valor_calculado,valor_declarado,diferenca")


def _codigos_municipais(arquivo, aba):
    quadro = pd.read_excel(PASTA / arquivo, sheet_name=aba, usecols=["COD_IBGE"])
    return {str(int(codigo)) for codigo in quadro["COD_IBGE"].dropna().unique()}


def _contagens(engine):
    with Session(engine) as session:
        cargas = session.scalar(select(func.count()).select_from(Carga))
        valores = session.scalar(select(func.count()).select_from(ValorObservado))
    return cargas, valores
