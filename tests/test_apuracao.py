import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.apuracao import apurar, comparar, congelar, exportar, garantir_formula, impedimentos
from validacao.catalogo import semear
from validacao.db import criar_engine, criar_tabelas
from validacao.decisoes import confirmar, exige_revisao
from validacao.modelos import Apuracao, Carga, CargaEvento, Decisao, Ente, Usuario, ValorObservado, Verificacao
from validacao.operacao import copiar_banco, registrar_evento
from validacao.regras import garantir_regra, parametros_de

SALVADOR = 2927408
OUTRO = 2900108
BAHIA = 29
DF = 53
AGORA = datetime(2026, 10, 29, 12, 0, 0)


@pytest.fixture(autouse=True)
def relatorios_temporarios(tmp_path, monkeypatch):
    """Evita que a exportação de teste grave na pasta real de relatórios."""
    monkeypatch.setattr("validacao.apuracao.PASTA_RELATORIOS", tmp_path / "relatorios")


@pytest.fixture
def banco(tmp_path):
    engine = criar_engine(tmp_path / "validacao.sqlite")
    criar_tabelas(engine)
    with Session(engine) as session:
        semear(session)
        session.add_all(
            [
                Ente(id_ente=SALVADOR, esfera="MUNICIPIO", uf="BA", nome="Salvador", capital=True, tribunal_competente="TCM-BA"),
                Ente(id_ente=OUTRO, esfera="MUNICIPIO", uf="BA", nome="Outro município", capital=False, tribunal_competente="TCM-BA"),
                Ente(id_ente=BAHIA, esfera="ESTADO", uf="BA", nome="Bahia", capital=False, tribunal_competente="TCE-BA"),
                Ente(id_ente=DF, esfera="DF", uf="DF", nome="Distrito Federal", capital=False, tribunal_competente="TCDF"),
            ]
        )
        analista = Usuario(nome="Ana", perfil="ANALISTA")
        revisor = Usuario(nome="Rui", perfil="REVISOR")
        session.add_all([analista, revisor])
        session.flush()
        cargas = {}
        for indice, fonte in enumerate(("SICONFI", "SIOPS", "SIOPE"), start=1):
            carga = Carga(
                fonte_id=fonte,
                arquivo_ou_endpoint=fonte,
                hash=f"hash-{fonte}",
                data_carga=AGORA,
                exercicios="2023,2024",
                usuario="teste",
                situacao="CONCLUIDA",
            )
            session.add(carga)
            session.flush()
            cargas[fonte] = carga.id
        for ano, componente, linha in (
            (2023, "ISS_TOTAL", 11),
            (2024, "ISS_TOTAL", 12),
            (2023, "COTA_RECEBIDA_TOTAL", 13),
            (2024, "COTA_RECEBIDA_TOTAL", 14),
        ):
            for fonte, carga_id in cargas.items():
                session.add(
                    ValorObservado(
                        carga_id=carga_id,
                        id_ente=SALVADOR,
                        ano=ano,
                        componente_id=componente,
                        valor=1,
                        natureza="Bruto",
                        linha_origem=linha,
                    )
                )
        regra = garantir_regra(session)
        parametros = parametros_de(regra)
        parametros["revisao_corte"] = 500
        regra.parametros = json.dumps(parametros)
        session.flush()
        blocos = [
            (SALVADOR, 2023, "ISS", None, "TIRAR_ANO", '["FECHA"]'),
            (SALVADOR, 2023, "COTA_RECEBIDA", None, "TIRAR_ANO", '["FECHA"]'),
            (SALVADOR, 2024, "ISS", 300, "USAR_SUGERIDA", '["FECHA"]'),
            (SALVADOR, 2024, "COTA_RECEBIDA", 100, "USAR_SUGERIDA", '["FECHA"]'),
            (OUTRO, 2023, "ISS", 80, "USAR_SUGERIDA", '["FECHA"]'),
            (OUTRO, 2023, "COTA_RECEBIDA", 20, "USAR_SUGERIDA", '["FECHA"]'),
            (OUTRO, 2024, "ISS", 80, "USAR_SUGERIDA", '["FECHA"]'),
            (OUTRO, 2024, "COTA_RECEBIDA", 20, "USAR_SUGERIDA", '["FECHA"]'),
            (OUTRO, 2024, "COMPENSACAO", None, None, '["DIVERGE_ACIMA_5"]'),
            (BAHIA, 2023, "ICMS", 500, "USAR_SUGERIDA", '["FECHA"]'),
            (BAHIA, 2024, "ICMS", 500, "USAR_SUGERIDA", '["FECHA"]'),
            (BAHIA, 2023, "COTA_REPASSADA", 100, "USAR_SUGERIDA", '["FECHA"]'),
            (BAHIA, 2024, "COTA_REPASSADA", 100, "USAR_SUGERIDA", '["FECHA"]'),
            (DF, 2023, "ISS", 40, "USAR_SUGERIDA", '["FECHA"]'),
            (DF, 2023, "ICMS", 60, "USAR_SUGERIDA", '["FECHA"]'),
            (DF, 2024, "ISS", 40, "USAR_SUGERIDA", '["FECHA"]'),
            (DF, 2024, "ICMS", 60, "USAR_SUGERIDA", '["FECHA"]'),
            (DF, 2024, "COTA_RECEBIDA", None, None, '["NAO_SE_APLICA"]'),
        ]
        usadas = json.dumps(cargas)
        for id_ente, ano, bloco, valor, acao, situacoes in blocos:
            session.add(
                Verificacao(
                    id_ente=id_ente,
                    ano=ano,
                    bloco=bloco,
                    regra_versao_id=regra.id,
                    valor_referencia=valor,
                    fonte_sugerida=None if acao != "USAR_SUGERIDA" else "SICONFI",
                    valor_sugerido=valor,
                    situacoes=situacoes,
                    diferencas="{}",
                )
            )
            if acao is None:
                continue
            session.add(
                Decisao(
                    id_ente=id_ente,
                    ano=ano,
                    bloco=bloco,
                    acao=acao,
                    fonte=None if acao == "TIRAR_ANO" else "SICONFI",
                    valor=valor,
                    usuario_id=analista.id,
                    data=AGORA,
                    cargas_usadas=usadas if id_ente == SALVADOR else json.dumps({"SICONFI": cargas["SICONFI"]}),
                    estado="VIGENTE",
                )
            )
        session.commit()
        return engine, analista.id, revisor.id


def test_telas_de_revisao_base_e_painel(banco):
    from validacao.web import criar_app

    engine, _analista, _revisor = banco
    with TestClient(criar_app(Path(engine.url.database))) as cliente:
        cliente.post("/login", data={"nome": "Ana", "perfil": "ANALISTA"}, follow_redirects=False)
        for caminho in ("/revisao", "/bases", "/painel"):
            resposta = cliente.get(caminho)
            assert resposta.status_code == 200, caminho
        ficha = cliente.get(f"/ente/{SALVADOR}")
        assert ficha.status_code == 200
        assert "Confirmação exigida" in ficha.text


def test_congelar_recusa_pendente_vermelho_e_sem_revisao(banco):
    engine, _analista, _revisor = banco
    with Session(engine) as session:
        contagem = impedimentos(session)
        assert contagem["pendentes"] == 1
        assert contagem["vermelhos"] == 1
        assert contagem["sem_revisao"] > 0
        with pytest.raises(Exception, match="não fecha"):
            congelar(session, _analista)


def test_corte_e_capital_exigem_revisao_de_outro_usuario(banco):
    engine, analista, revisor = banco
    with Session(engine) as session:
        _decidir_compensacao(session, analista, 1000)
        session.commit()
        contagem = impedimentos(session)
        assert contagem["pendentes"] == 0
        assert contagem["vermelhos"] == 0
        assert contagem["sem_revisao"] > 0
        outro = session.get(Ente, OUTRO)
        corte = parametros_de(garantir_regra(session))["revisao_corte"]
        assert exige_revisao(outro, 1000, corte)
        assert not exige_revisao(outro, 80, corte)
        with pytest.raises(Exception, match="não fecha"):
            congelar(session, analista)
        with pytest.raises(Exception, match="própria decisão"):
            compensacao = session.scalar(
                select(Decisao).where(Decisao.id_ente == OUTRO, Decisao.bloco == "COMPENSACAO", Decisao.estado == "VIGENTE")
            )
            confirmar(session, compensacao.id, analista)


def test_apuracao_repete_memoria_e_soma(banco, tmp_path):
    engine, analista, revisor = banco
    with Session(engine) as session:
        _decidir_compensacao(session, analista, 1000)
        _confirmar_exigidas(session, revisor)
        session.commit()
        versao = congelar(session, analista)
        de_novo = congelar(session, analista)
        assert de_novo.id == versao.id
        formula = garantir_formula(session)
        primeira = apurar(session, versao.id, formula.id)
        session.commit()
        segunda = apurar(session, versao.id, formula.id)
        assert [item.id for item in primeira] == [item.id for item in segunda]
        assert [item.coeficiente for item in primeira] == [item.coeficiente for item in segunda]
        salvador = session.scalar(
            select(Apuracao).where(Apuracao.base_versao_id == versao.id, Apuracao.id_ente == SALVADOR)
        )
        memoria = json.loads(salvador.memoria)
        pares = {(ano["ano"], bloco["bloco"]) for ano in memoria["anos"] for bloco in ano["blocos"]}
        assert pares == {(2023, "ISS"), (2023, "COTA_RECEBIDA"), (2024, "ISS"), (2024, "COTA_RECEBIDA")}
        fontes = {
            carga["fonte"]
            for ano in memoria["anos"]
            for bloco in ano["blocos"]
            for carga in bloco["cargas"]
        }
        assert fontes == {"SICONFI", "SIOPS", "SIOPE"}
        assert any(carga["linha_origem"] == 12 for ano in memoria["anos"] for bloco in ano["blocos"] for carga in bloco["cargas"])
        somas = {}
        for apuracao, ente in session.execute(
            select(Apuracao, Ente).join(Ente, Ente.id_ente == Apuracao.id_ente).where(Apuracao.base_versao_id == versao.id)
        ):
            somas[ente.esfera] = somas.get(ente.esfera, 0) + apuracao.coeficiente
        for total in somas.values():
            assert total == pytest.approx(1)
        iss = session.scalar(
            select(Decisao).where(
                Decisao.id_ente == SALVADOR, Decisao.ano == 2024, Decisao.bloco == "ISS", Decisao.estado == "VIGENTE"
            )
        )
        iss.estado = "SUBSTITUIDA"
        session.flush()
        nova = Decisao(
            id_ente=SALVADOR,
            ano=2024,
            bloco="ISS",
            acao="VALOR_DIGITADO",
            valor=600,
            motivo="Ajuste",
            usuario_id=analista,
            data=AGORA,
            cargas_usadas=iss.cargas_usadas,
            estado="VIGENTE",
        )
        session.add(nova)
        session.flush()
        confirmar(session, nova.id, revisor)
        segunda_base = congelar(session, analista)
        assert segunda_base.id != versao.id
        mudancas = comparar(session, versao.id, segunda_base.id, formula.id)
        salvador_mudou = next(item for item in mudancas if item["id_ente"] == SALVADOR)
        assert any(
            item["ano"] == 2024 and item["bloco"] == "ISS" and item["decisao_a"] == iss.id and item["decisao_b"] == nova.id
            for item in salvador_mudou["decisoes"]
        )
        csv_path = exportar(session, versao.id, formula.id, "csv")
        assert versao.hash in csv_path.read_text(encoding="utf-8")
        xlsx_path = exportar(session, versao.id, formula.id, "xlsx")
        livro = load_workbook(xlsx_path)
        assert livro["Versao"]["B1"].value == versao.hash
        copia = copiar_banco(engine.url.database)
        assert copia.is_file()
        registrar_evento(session, "SICONFI", "ERRO", "falha de teste")
        session.commit()
        assert session.scalar(select(CargaEvento).where(CargaEvento.situacao == "ERRO")) is not None


def _decidir_compensacao(session, analista, valor):
    verificacao = session.scalar(
        select(Verificacao).where(Verificacao.id_ente == OUTRO, Verificacao.bloco == "COMPENSACAO")
    )
    verificacao.situacoes = '["FECHA"]'
    session.add(
        Decisao(
            id_ente=OUTRO,
            ano=2024,
            bloco="COMPENSACAO",
            acao="VALOR_DIGITADO",
            valor=valor,
            motivo="Valor informado",
            usuario_id=analista,
            data=AGORA,
            cargas_usadas="{}",
            estado="VIGENTE",
        )
    )


def _confirmar_exigidas(session, revisor):
    corte = parametros_de(garantir_regra(session))["revisao_corte"]
    for decisao, ente in session.execute(
        select(Decisao, Ente).join(Ente, Ente.id_ente == Decisao.id_ente).where(Decisao.estado == "VIGENTE")
    ):
        if exige_revisao(ente, decisao.valor, corte):
            confirmar(session, decisao.id, revisor)
