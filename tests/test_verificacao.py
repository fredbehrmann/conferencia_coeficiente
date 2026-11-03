import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from validacao.cargas import carregar_pasta
from validacao.db import criar_engine, criar_tabelas
from validacao.modelos import Carga, Ente, RegraVersao, ValorObservado, Verificacao
from validacao.motor import reprocessar
from validacao.referencia import iguais
from validacao.regras import garantir_regra, parametros_de

PASTA = Path(__file__).resolve().parents[2]
ANOS = (2019, 2020, 2021, 2022, 2023, 2024, 2025)
NEGATIVOS = {
    (4104204, 2020, "ISS"),
    (4313086, 2022, "ISS"),
    (4322509, 2022, "ISS"),
    (4312138, 2023, "ISS"),
    (4309001, 2024, "COMPENSACAO"),
    (2514107, 2025, "COMPENSACAO"),
}
TAXAS = (
    ("ISS", "SIOPE", 86.3),
    ("ISS", "SIOPS", 79.0),
    ("COTA_RECEBIDA", "SIOPE", 91.1),
    ("COTA_RECEBIDA", "SIOPS", 88.6),
    ("ISS", "TCE", 93.5),
)


@pytest.fixture(scope="module")
def banco(tmp_path_factory):
    destino = tmp_path_factory.mktemp("banco") / "validacao.sqlite"
    engine = criar_engine(destino)
    criar_tabelas(engine)
    carregar_pasta(engine, PASTA)
    with Session(engine) as session:
        regra = garantir_regra(session)
        session.commit()
        reprocessar(session, regra)
        session.commit()
        regra_id = regra.id
    return engine, regra_id


def test_regra_inicial_deixa_fundeb_e_tira_compensacao_e_simples(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        params = parametros_de(session.get(RegraVersao, regra_id))
    assert params["fundeb_permanece"] is True
    assert params["compensacao_soma"] is False
    assert params["iss_simples_soma"] is False
    assert params["aceite_sugerido"] == 0.01
    assert params["fila_obrigatoria"] == 0.05
    assert params["anos"] == list(ANOS)


def test_salvador_fecha_nos_sete_anos(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        for ano in ANOS:
            for bloco in ("ISS", "COTA_RECEBIDA"):
                linha = _linha(session, regra_id, 2927408, ano, bloco)
                situacoes = _situacoes(linha)
                assert "FECHA" in situacoes
                assert "FORA_COBERTURA" in situacoes
                assert linha.fonte_sugerida == "SICONFI"


def test_varginha_2022_e_2023(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        for ano in (2022, 2023):
            for bloco in ("ISS", "COTA_RECEBIDA"):
                linha = _linha(session, regra_id, 3170701, ano, bloco)
                situacoes = _situacoes(linha)
                assert "SEM_ABERTURA" in situacoes
                assert "DIVERGE_ACIMA_5" in situacoes
                assert "SALTO_SERIE" in situacoes
                if ano == 2023:
                    assert "VALOR_REPETIDO" in situacoes
                else:
                    assert "VALOR_REPETIDO" not in situacoes
        iss_2022 = _linha(session, regra_id, 3170701, 2022, "ISS")
        iss_2023 = _linha(session, regra_id, 3170701, 2023, "ISS")
        cota_2022 = _linha(session, regra_id, 3170701, 2022, "COTA_RECEBIDA")
        cota_2023 = _linha(session, regra_id, 3170701, 2023, "COTA_RECEBIDA")
    assert iss_2022.fonte_sugerida == "SIOPE"
    assert iss_2022.valor_sugerido == pytest.approx(61916384.44, abs=0.02)
    assert iss_2023.fonte_sugerida == "SIOPE"
    assert iss_2023.valor_sugerido == pytest.approx(68331719.19, abs=0.02)
    assert cota_2022.fonte_sugerida == "TCE"
    assert cota_2022.valor_sugerido == pytest.approx(183560481.86, abs=0.02)
    assert cota_2023.fonte_sugerida == "TCE"
    assert cota_2023.valor_sugerido == pytest.approx(192929797.90, abs=0.02)


def test_resende_2022_sugere_o_siope(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        linha = _linha(session, regra_id, 3304201, 2022, "ISS")
    assert "VAZIO_COM_VALOR_EXTERNO" in _situacoes(linha)
    assert linha.fonte_sugerida == "SIOPE"
    assert linha.valor_sugerido == pytest.approx(88637581.76, abs=0.02)


def test_iss_zero_em_2022_convive_com_cota_positiva(banco):
    engine, _regra_id = banco
    with Session(engine) as session:
        carga_id = session.scalar(
            select(func.max(Carga.id)).where(Carga.fonte_id == "SICONFI", Carga.situacao == "CONCLUIDA")
        )
        zeros = session.execute(
            select(ValorObservado.id_ente)
            .join(Ente, Ente.id_ente == ValorObservado.id_ente)
            .where(
                ValorObservado.carga_id == carga_id,
                Ente.esfera == "MUNICIPIO",
                ValorObservado.ano == 2022,
                ValorObservado.componente_id == "ISS_TOTAL",
                ValorObservado.valor == 0,
            )
        ).all()
        entes = [id_ente for (id_ente,) in zeros]
        positivas = session.scalar(
            select(func.count())
            .select_from(ValorObservado)
            .where(
                ValorObservado.carga_id == carga_id,
                ValorObservado.ano == 2022,
                ValorObservado.componente_id == "COTA_RECEBIDA_TOTAL",
                ValorObservado.id_ente.in_(entes),
                ValorObservado.valor > 0,
            )
        )
    assert len(entes) == 111
    assert positivas == 110


def test_2025_tem_78_municipios_sem_declaracao(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        linhas = session.scalars(
            select(Verificacao).where(
                Verificacao.regra_versao_id == regra_id,
                Verificacao.ano == 2025,
                Verificacao.bloco == "ISS",
                Verificacao.situacoes.like('%"SEM_DECLARACAO"%'),
            )
        )
        faltantes = {linha.id_ente for linha in linhas}
    assert len(faltantes) == 78


def test_negativos_sao_as_seis_linhas_conhecidas(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        linhas = session.scalars(
            select(Verificacao).where(
                Verificacao.regra_versao_id == regra_id,
                Verificacao.situacoes.like('%"NEGATIVO"%'),
            )
        )
        achados = {(linha.id_ente, linha.ano, linha.bloco) for linha in linhas}
    assert achados == NEGATIVOS


def test_amazonas_2023_e_dez_ufs_fora_de_5_por_cento(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        pares = session.scalars(
            select(Verificacao)
            .join(Ente, Ente.id_ente == Verificacao.id_ente)
            .where(
                Verificacao.regra_versao_id == regra_id,
                Verificacao.bloco == "COTA_REPASSADA",
                Ente.esfera == "ESTADO",
            )
        ).all()
        fora = [linha for linha in pares if "UF_NAO_FECHA" in _situacoes(linha)]
        amazonas = _linha(session, regra_id, 13, 2023, "COTA_REPASSADA")
    assert len(pares) == 182
    assert len(fora) == 10
    assert "UF_NAO_FECHA" in _situacoes(amazonas)


def test_cota_liquida_de_fundeb_em_sao_paulo_e_piaui(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        params = parametros_de(session.get(RegraVersao, regra_id))
        linhas = session.execute(
            select(Verificacao, Ente.uf)
            .join(Ente, Ente.id_ente == Verificacao.id_ente)
            .where(
                Verificacao.regra_versao_id == regra_id,
                Verificacao.bloco == "COTA_RECEBIDA",
                Ente.esfera == "MUNICIPIO",
                Ente.uf.in_(("SP", "PI")),
            )
        )
        estatistica = {uf: [0, 0] for uf in ("SP", "PI")}
        for linha, uf in linhas:
            diferencas = json.loads(linha.diferencas)
            siconfi = diferencas["SICONFI"]
            tce = diferencas["TCE"]
            if siconfi is None or tce is None or siconfi["valor"] <= 0:
                continue
            estatistica[uf][1] += 1
            razao = tce["valor"] / siconfi["valor"]
            if params["fundeb_liquido_min"] <= razao <= params["fundeb_liquido_max"]:
                estatistica[uf][0] += 1
                assert "FONTE_LIQUIDA_FUNDEB" in _situacoes(linha)
    assert round(100 * estatistica["SP"][0] / estatistica["SP"][1], 1) == 95.6
    assert round(100 * estatistica["PI"][0] / estatistica["PI"][1], 1) == 81.1


def test_df_nao_tem_cota(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        for ano in ANOS:
            for bloco in ("COTA_RECEBIDA", "COTA_REPASSADA"):
                linha = _linha(session, regra_id, 53, ano, bloco)
                assert _situacoes(linha) == {"NAO_SE_APLICA"}
                assert linha.fonte_sugerida is None
                assert linha.valor_referencia is None


def test_taxas_de_igualdade(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        linhas = session.scalars(
            select(Verificacao).where(
                Verificacao.regra_versao_id == regra_id,
                Verificacao.bloco.in_([bloco for bloco, _fonte, _taxa in TAXAS]),
            )
        )
        pares = {(bloco, fonte): [0, 0] for bloco, fonte, _taxa in TAXAS}
        for linha in linhas:
            diferencas = json.loads(linha.diferencas)
            referencia = diferencas["SICONFI"]
            if referencia is None:
                continue
            for bloco, fonte, _taxa in TAXAS:
                if linha.bloco != bloco:
                    continue
                outro = diferencas[fonte]
                if outro is None:
                    continue
                pares[(bloco, fonte)][1] += 1
                if iguais(referencia["valor"], outro["valor"]):
                    pares[(bloco, fonte)][0] += 1
    for bloco, fonte, taxa in TAXAS:
        acertos, total = pares[(bloco, fonte)]
        assert total > 0
        assert round(100 * acertos / total, 1) == taxa


def test_reprocessar_nao_apaga_outra_versao(banco):
    engine, regra_id = banco
    with Session(engine) as session:
        antes = _quantidade(session, regra_id)
        regra = session.get(RegraVersao, regra_id)
        reprocessar(session, regra)
        session.commit()
        assert _quantidade(session, regra_id) == antes
        segunda = RegraVersao(vigente_desde=regra.vigente_desde, parametros=regra.parametros)
        session.add(segunda)
        session.flush()
        _contagem, linhas = reprocessar(session, segunda)
        session.commit()
        assert _quantidade(session, regra_id) == antes
        assert _quantidade(session, segunda.id) == antes
        assert linhas == antes


def _linha(session, regra_id, id_ente, ano, bloco):
    linha = session.scalar(
        select(Verificacao).where(
            Verificacao.regra_versao_id == regra_id,
            Verificacao.id_ente == id_ente,
            Verificacao.ano == ano,
            Verificacao.bloco == bloco,
        )
    )
    assert linha is not None
    return linha


def _situacoes(linha) -> set[str]:
    return set(json.loads(linha.situacoes))


def _quantidade(session, regra_id) -> int:
    return session.scalar(
        select(func.count()).select_from(Verificacao).where(Verificacao.regra_versao_id == regra_id)
    )
