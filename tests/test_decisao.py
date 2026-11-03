import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from validacao.cargas import carregar_pasta
from validacao.db import criar_engine, criar_tabelas
from validacao.decisoes import reabrir_alterados, situacoes_de
from validacao.modelos import Carga, Decisao, Verificacao
from validacao.motor import VERMELHAS, reprocessar
from validacao.regras import garantir_regra
from validacao.web import criar_app

PASTA = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cliente(tmp_path_factory):
    destino = tmp_path_factory.mktemp("banco") / "validacao.sqlite"
    engine = criar_engine(destino)
    criar_tabelas(engine)
    carregar_pasta(engine, PASTA)
    with Session(engine) as session:
        regra = garantir_regra(session)
        session.commit()
        reprocessar(session, regra)
        session.commit()
    app = criar_app(destino)
    with TestClient(app) as cliente_http:
        cliente_http.engine = engine
        yield cliente_http


def test_lote_nao_libera_situacao_vermelha(cliente):
    entrar(cliente, "Ana", "ANALISTA")
    resposta = cliente.post("/fila/aceitar", data={}, follow_redirects=False)
    assert resposta.status_code == 303
    with Session(cliente.engine) as session:
        linhas = session.execute(
            select(Decisao, Verificacao.situacoes)
            .join(
                Verificacao,
                (Verificacao.id_ente == Decisao.id_ente)
                & (Verificacao.ano == Decisao.ano)
                & (Verificacao.bloco == Decisao.bloco),
            )
            .where(Decisao.estado == "VIGENTE")
        ).all()
        assert linhas
        for _decisao, texto in linhas:
            assert not (situacoes_de(texto) & VERMELHAS)


def test_varginha_2022_nao_oferece_o_siconfi(cliente):
    entrar(cliente, "Ana", "ANALISTA")
    pagina = cliente.get("/ente/3170701")
    assert pagina.status_code == 200
    assert "O ISS de Varginha em 2022 entra no cálculo?" in pagina.text
    assert "Usar o SIOPE" in pagina.text
    assert "Trocar para a fonte escolhida" in pagina.text
    assert "Tirar o ano" in pagina.text
    assert re.search(r"<button[^>]*disabled[^>]*>\s*Usar o SICONFI\s*</button>", pagina.text)
    recusa = cliente.post(
        "/ente/3170701/decidir",
        data={"ano": "2022", "bloco": "ISS", "acao": "TROCAR_FONTE", "fonte": "SICONFI"},
        follow_redirects=True,
    )
    assert "sem abertura" in recusa.text
    with Session(cliente.engine) as session:
        vigente = session.scalar(
            select(Decisao).where(
                Decisao.id_ente == 3170701,
                Decisao.ano == 2022,
                Decisao.bloco == "ISS",
                Decisao.estado == "VIGENTE",
            )
        )
    assert vigente is None


def test_resende_2022_decide_blocos_por_fontes_diferentes(cliente):
    entrar(cliente, "Ana", "ANALISTA")
    for bloco in ("ISS", "COTA_RECEBIDA"):
        resposta = cliente.post(
            "/ente/3304201/decidir",
            data={"ano": "2022", "bloco": bloco, "acao": "USAR_SUGERIDA"},
            follow_redirects=False,
        )
        assert resposta.status_code == 303
    with Session(cliente.engine) as session:
        fontes = {
            bloco: session.scalar(
                select(Decisao.fonte).where(
                    Decisao.id_ente == 3304201,
                    Decisao.ano == 2022,
                    Decisao.bloco == bloco,
                    Decisao.estado == "VIGENTE",
                )
            )
            for bloco in ("ISS", "COTA_RECEBIDA")
        }
    assert fontes["ISS"] == "SIOPE"
    assert fontes["COTA_RECEBIDA"] == "SICONFI"
    assert fontes["ISS"] != fontes["COTA_RECEBIDA"]


def test_carga_nova_reabre_somente_o_bloco_alterado(cliente):
    entrar(cliente, "Ana", "ANALISTA")
    for bloco in ("ISS", "COTA_RECEBIDA"):
        cliente.post(
            "/ente/2927408/decidir",
            data={"ano": "2024", "bloco": bloco, "acao": "USAR_SUGERIDA"},
            follow_redirects=False,
        )
    with Session(cliente.engine) as session:
        assert reabrir_alterados(session) == 0
        antiga = session.scalar(
            select(func.max(Carga.id)).where(Carga.fonte_id == "SICONFI", Carga.situacao == "CONCLUIDA")
        )
        session.add(
            Carga(
                fonte_id="SICONFI",
                arquivo_ou_endpoint="siconfi-alterado.xlsx",
                hash="c" * 64,
                data_carga=session.scalar(select(Carga.data_carga).where(Carga.id == antiga)),
                exercicios="2019,2020,2021,2022,2023,2024,2025",
                usuario="teste",
                situacao="CONCLUIDA",
            )
        )
        session.flush()
        nova = session.scalar(select(func.max(Carga.id)))
        session.execute(
            text(
                """
                INSERT INTO valor_observado
                    (carga_id, id_ente, ano, componente_id, valor, natureza, conta_origem, linha_origem)
                SELECT :nova, id_ente, ano, componente_id, valor, natureza, conta_origem, linha_origem
                FROM valor_observado
                WHERE carga_id = :antiga
                  AND componente_id IN (
                    'ISS_TOTAL', 'COTA_RECEBIDA_TOTAL', 'ICMS_TOTAL', 'COTA_REPASSADA', 'COMPENSACAO'
                  )
                """
            ),
            {"nova": nova, "antiga": antiga},
        )
        alterados = session.execute(
            text(
                """
                UPDATE valor_observado
                SET valor = 1
                WHERE carga_id = :nova AND id_ente = 2927408 AND ano = 2024
                  AND componente_id = 'ISS_TOTAL'
                """
            ),
            {"nova": nova},
        )
        assert alterados.rowcount == 1
        reabertos = reabrir_alterados(session)
        session.commit()
        iss = _estado(session, 2927408, 2024, "ISS")
        cota = _estado(session, 2927408, 2024, "COTA_RECEBIDA")
    assert reabertos == 1
    assert iss == "REABERTA"
    assert cota == "VIGENTE"


def test_perfis_separam_as_acoes(cliente):
    entrar(cliente, "Ana", "ANALISTA")
    assert cliente.get("/regras").status_code == 403
    entrar(cliente, "Rita", "REVISOR")
    assert cliente.post(
        "/ente/3170701/decidir",
        data={"ano": "2022", "bloco": "ISS", "acao": "USAR_SUGERIDA"},
    ).status_code == 403
    entrar(cliente, "Otto", "ADMINISTRADOR")
    assert cliente.post(
        "/ente/3170701/decidir",
        data={"ano": "2022", "bloco": "ISS", "acao": "USAR_SUGERIDA"},
    ).status_code == 403
    assert cliente.get("/regras").status_code == 200


def entrar(cliente, nome, perfil):
    resposta = cliente.post("/login", data={"nome": nome, "perfil": perfil}, follow_redirects=False)
    assert resposta.status_code == 303


def _estado(session, id_ente, ano, bloco):
    return session.scalar(
        select(Decisao.estado).where(
            Decisao.id_ente == id_ente,
            Decisao.ano == ano,
            Decisao.bloco == bloco,
            Decisao.estado.in_(("VIGENTE", "REABERTA")),
        )
    )
