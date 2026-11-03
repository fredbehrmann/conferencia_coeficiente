import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.apresentacao import montar_ficha
from validacao.cargas import carregar_pasta
from validacao.cargas.complementar import carregar_complementares
from validacao.cargas.siconfi_api import agregar_msc
from validacao.de_para import componente_da_natureza
from validacao.db import criar_engine, criar_tabelas
from validacao.modelos import Ente, RegraVersao, Verificacao
from validacao.motor import reprocessar
from validacao.regras import garantir_regra

PASTA = Path(__file__).resolve().parents[2]
CONFAZ = Path(__file__).resolve().parents[1] / "fontes" / "confaz_boletim.xlsx"
VARGINHA = 3170701
BOA_ESPERANCA = 5101837
SALVADOR = 2927408
BAHIA = 29
ISS_SALVADOR_2024 = 1979102342.31
ISS_ITENS = {
    "ISS_PRINCIPAL": 921627039.20,
    "ISS_MULTAS_JUROS": 20133988.96,
    "ISS_DA_PRINCIPAL": 37000696.00,
    "ISS_DA_MULTAS": 11900639.20,
}
COTA_VARGINHA = 2936967709.76
SIMPLES_SALVADOR = 1000.0
TRIBUNAL_BA = 1234.56


def test_de_para_separa_regime_e_ignora_familia_alheia():
    assert componente_da_natureza("11125311", 2021) is None
    assert componente_da_natureza("11125311", 2022) == "ICMS_PRINCIPAL"
    assert componente_da_natureza("11145111", 2022) == "ISS_PRINCIPAL"
    assert componente_da_natureza("17215001", 2022) == "COTA_RECEBIDA_BRUTO"
    assert componente_da_natureza("11130311", 2022) is None


def test_agregar_msc_reconstrui_varginha_e_descarta_previsao():
    linhas = [
        _msc("621200000", "11145111", ISS_ITENS["ISS_PRINCIPAL"]),
        _msc("621200000", "11145112", ISS_ITENS["ISS_MULTAS_JUROS"]),
        _msc("621200000", "11145113", ISS_ITENS["ISS_DA_PRINCIPAL"]),
        _msc("621200000", "11145114", ISS_ITENS["ISS_DA_MULTAS"]),
        _msc("621100000", "11145111", 999),
        _msc("621200000", "11130311", 50),
        _msc("621200000", "17215001", COTA_VARGINHA),
    ]
    itens = agregar_msc(linhas, 2022)
    for componente, valor in ISS_ITENS.items():
        assert itens[componente] == pytest.approx(valor)
    assert itens["ISS_TOTAL"] == pytest.approx(sum(ISS_ITENS.values()))
    assert itens["COTA_RECEBIDA_BRUTO"] == pytest.approx(COTA_VARGINHA)
    assert "ICMS_PRINCIPAL" not in itens


@pytest.fixture(scope="module")
def banco(tmp_path_factory):
    destino = tmp_path_factory.mktemp("banco") / "validacao.sqlite"
    engine = criar_engine(destino)
    criar_tabelas(engine)
    carregar_pasta(engine, PASTA)
    with Session(engine) as session:
        garantir_regra(session)
        session.commit()
        reprocessar(session)
        session.commit()
        baianos = list(
            session.scalars(
                select(Ente.id_ente).where(Ente.esfera == "MUNICIPIO", Ente.uf == "BA")
            )
        )
    pasta = tmp_path_factory.mktemp("fontes")
    shutil.copy(CONFAZ, pasta / "confaz_boletim.xlsx")
    (pasta / "ibge_municipios.json").write_text(
        json.dumps([{"id": BOA_ESPERANCA}]),
        encoding="utf-8",
    )
    for nome in ("msc", "rreo", "entrega", "sefaz", "tribunais"):
        (pasta / nome).mkdir()
    (pasta / "msc" / f"{VARGINHA}_2022.json").write_text(
        json.dumps(
            {
                **ISS_ITENS,
                "ISS_TOTAL": sum(ISS_ITENS.values()),
                "COTA_RECEBIDA_BRUTO": COTA_VARGINHA,
                "COTA_RECEBIDA_TOTAL": COTA_VARGINHA,
            }
        ),
        encoding="utf-8",
    )
    (pasta / "rreo" / f"{VARGINHA}_2022.json").write_text("{}", encoding="utf-8")
    (pasta / "entrega" / f"{VARGINHA}_2022.json").write_text(
        json.dumps({"entregue": False}),
        encoding="utf-8",
    )
    (pasta / "sefaz" / "ba.csv").write_text(
        "uf,id_ente,ano,cota_repassada,cota_recebida\nBA,29,2024,1500.00,\n",
        encoding="utf-8",
    )
    (pasta / "iss_simples.csv").write_text(
        f"id_ente,ano,valor,origem\n{SALVADOR},2024,{SIMPLES_SALVADOR},BB\n",
        encoding="utf-8",
    )
    (pasta / "sefaz" / "modelo.csv").write_text(
        "uf,id_ente,ano,cota_repassada,cota_recebida\n",
        encoding="utf-8",
    )
    linhas = ["id_ente,ano,componente_id,valor,natureza"]
    linhas.extend(f"{ente},2024,ISS_TOTAL,{TRIBUNAL_BA},Bruto" for ente in baianos)
    (pasta / "tribunais" / "tcm_ba.csv").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    (pasta / "tribunais" / "modelo.csv").write_text(
        "id_ente,ano,componente_id,valor,natureza\n",
        encoding="utf-8",
    )
    carregar_complementares(engine, pasta, somente=[(VARGINHA, 2022)])
    with Session(engine) as session:
        reprocessar(session)
        session.commit()
    return engine


def test_varginha_abre_iss_e_informa_cota_sem_abertura(banco):
    with Session(banco) as session:
        iss = _linha(session, VARGINHA, 2022, "ISS")
        cota = _linha(session, VARGINHA, 2022, "COTA_RECEBIDA")
        assert "SEM_ABERTURA" not in _situacoes(iss)
        assert "SEM_ABERTURA" in _situacoes(cota)
        ficha = montar_ficha(session, VARGINHA)
    iss_bloco = _bloco(ficha, 2022, "ISS")
    cota_bloco = _bloco(ficha, 2022, "COTA_RECEBIDA")
    por_fonte = {fonte["id"]: fonte for fonte in iss_bloco["fontes"]}
    assert por_fonte["MSC"]["independencia"] == "mesmo declarante"
    assert por_fonte["SICONFI"]["frase"] == "não entregou a DCA"
    vistos = {item["item"]: item["valor"] for item in iss_bloco["itens"] if item["fonte"] == "MSC"}
    assert vistos["Principal"] == pytest.approx(ISS_ITENS["ISS_PRINCIPAL"])
    assert vistos["Multas e juros"] == pytest.approx(ISS_ITENS["ISS_MULTAS_JUROS"])
    assert vistos["Dívida ativa"] == pytest.approx(ISS_ITENS["ISS_DA_PRINCIPAL"])
    assert vistos["Multas e juros da dívida ativa"] == pytest.approx(ISS_ITENS["ISS_DA_MULTAS"])
    msc_cota = next(fonte for fonte in cota_bloco["fontes"] if fonte["id"] == "MSC")
    assert msc_cota["frase"] == "a MSC também não traz abertura"


def test_confaz_compara_as_27_ufs(banco):
    with Session(banco) as session:
        linhas = session.scalars(
            select(Verificacao).where(Verificacao.bloco == "ICMS", Verificacao.ano.in_((2024, 2025)))
        )
        pares = set()
        for linha in linhas:
            info = json.loads(linha.diferencas).get("CONFAZ")
            if info and info.get("valor") is not None:
                pares.add((linha.id_ente, linha.ano))
    entes = {ente for ente, _ano in pares}
    assert len(entes) == 27
    assert {(ente, ano) for ente in entes for ano in (2024, 2025)} <= pares


def test_boa_esperanca_nao_recebe_sem_declaracao_antes_de_existir(banco):
    with Session(banco) as session:
        ente = session.get(Ente, BOA_ESPERANCA)
        assert ente is not None
        assert ente.ano_instalacao == 2025
        anterior = _linha(session, BOA_ESPERANCA, 2024, "ISS")
    assert "SEM_DECLARACAO" not in _situacoes(anterior)
    assert "NAO_SE_APLICA" in _situacoes(anterior)


def test_bahia_sai_de_fora_de_cobertura_e_mostra_tribunal(banco):
    with Session(banco) as session:
        baianos = list(
            session.scalars(select(Ente.id_ente).where(Ente.esfera == "MUNICIPIO", Ente.uf == "BA"))
        )
        linhas = session.scalars(
            select(Verificacao).where(
                Verificacao.id_ente.in_(baianos),
                Verificacao.ano == 2024,
                Verificacao.bloco == "ISS",
            )
        ).all()
        salvador = _linha(session, SALVADOR, 2024, "ISS")
    assert linhas
    assert len(linhas) == len(baianos)
    assert all("FORA_COBERTURA" not in _situacoes(linha) for linha in linhas)
    assert json.loads(salvador.diferencas)["TCE"]["valor"] == pytest.approx(TRIBUNAL_BA)


def test_ficha_rotula_independencia(banco):
    with Session(banco) as session:
        ficha = montar_ficha(session, BAHIA)
    icms = {fonte["id"]: fonte for fonte in _bloco(ficha, 2024, "ICMS")["fontes"]}
    cota = {fonte["id"]: fonte for fonte in _bloco(ficha, 2024, "COTA_REPASSADA")["fontes"]}
    assert icms["CONFAZ"]["independencia"] == "terceiro"
    assert cota["SEFAZ_UF"]["independencia"] == "outro sistema"
    assert cota["SEFAZ_UF"]["valor"] == pytest.approx(1500)


def test_simples_desligado_nao_altera_o_iss_de_salvador(banco):
    with Session(banco) as session:
        iss = _linha(session, SALVADOR, 2024, "ISS")
        simples = _linha(session, SALVADOR, 2024, "ISS_SIMPLES")
        assert iss.valor_referencia == pytest.approx(ISS_SALVADOR_2024)
        assert json.loads(simples.diferencas)["SIMPLES"]["valor"] == pytest.approx(SIMPLES_SALVADOR)
        regra = session.get(RegraVersao, iss.regra_versao_id)
        parametros = json.loads(regra.parametros)
        parametros["iss_simples_soma"] = True
        regra.parametros = json.dumps(parametros)
        session.commit()
        reprocessar(session, regra)
        session.commit()
        ligado = _linha(session, SALVADOR, 2024, "ISS")
        assert ligado.valor_referencia == pytest.approx(ISS_SALVADOR_2024 + SIMPLES_SALVADOR)
        parametros["iss_simples_soma"] = False
        regra.parametros = json.dumps(parametros)
        session.commit()
        reprocessar(session, regra)
        session.commit()
        restaurado = _linha(session, SALVADOR, 2024, "ISS")
        assert restaurado.valor_referencia == pytest.approx(ISS_SALVADOR_2024)


def _msc(conta: str, natureza: str, valor: float) -> dict:
    return {
        "conta_contabil": conta,
        "natureza_receita": natureza,
        "valor": valor,
        "natureza_conta": "D",
    }


def _bloco(ficha: dict, ano: int, codigo: str) -> dict:
    secao = next(item for item in ficha["secoes"] if item["ano"] == ano)
    return next(item for item in secao["blocos"] if item["codigo"] == codigo)


def _linha(session: Session, id_ente: int, ano: int, bloco: str) -> Verificacao:
    regra = garantir_regra(session)
    linha = session.scalar(
        select(Verificacao).where(
            Verificacao.regra_versao_id == regra.id,
            Verificacao.id_ente == id_ente,
            Verificacao.ano == ano,
            Verificacao.bloco == bloco,
        )
    )
    assert linha is not None
    return linha


def _situacoes(linha: Verificacao) -> set[str]:
    return set(json.loads(linha.situacoes))
