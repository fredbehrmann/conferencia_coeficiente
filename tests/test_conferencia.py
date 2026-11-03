"""Cenários de aceitação da conferência (dados fictícios) e testes das regras.

Parâmetros de teste explícitos: os padrões da configuração inicial —
divergência com base auxiliar: tolerância relativa 1%, absoluta R$ 1.000,00,
modo "ambas", gravidade alta acima de 20%; variação atípica: 3,0×.
"""

import io
from datetime import datetime
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from validacao.conferencia import importacao
from validacao.conferencia import modelos as cf
from validacao.conferencia.comparacao import RegistroV, comparar, montar_fonte
from validacao.conferencia.consultas import caso_por_chave
from validacao.conferencia.decisao import DecisaoRecusada, decisao_vigente, registrar, situacao_do_caso
from validacao.conferencia.demo import construir, csv_de, dados_iniciais, importar_demo
from validacao.conferencia.dinheiro import diferenca, para_centavos, percentual, reais
from validacao.conferencia.execucao import executar, exigibilidade, expandir_valores
from validacao.conferencia.regras import excede
from validacao.conferencia.web import criar_app
from validacao.db import criar_engine


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    destino = tmp_path_factory.mktemp("demo") / "demo.sqlite"
    relatorio = construir(destino)
    return destino, relatorio


SENHA_TESTE = "Xq7!mR2#vL9pK4wz"


def _entrar(cliente, destino, nome, perfil="ANALISTA", volta="/"):
    """Cria (se preciso) a conta de teste da pessoa e entra com ela."""
    from validacao.conferencia import acesso as contas
    from validacao.modelos import Usuario

    from validacao.db import sem_acento

    login = "t." + re.sub(r"[^a-z0-9]+", ".", sem_acento(nome)).strip(".")
    engine = criar_engine(destino)
    with Session(engine) as session:
        if session.scalar(select(cf.Acesso).where(cf.Acesso.login == login)) is None:
            contas.criar_conta(session, login, nome, perfil, SENHA_TESTE, criado_por="teste", trocar_senha=False)
        else:
            pessoa = session.scalar(select(Usuario).where(Usuario.nome == nome))
            pessoa.perfil = perfil
            session.commit()
    engine.dispose()
    return cliente.post("/login", data={"login": login, "senha": SENHA_TESTE, "volta": volta})


def _caso(destino, ente, ano=2023):
    engine = criar_engine(destino)
    session = Session(engine)
    return session, caso_por_chave(session, ente, ano, "ISS_MUN")


# --------------------------------------------------------------- unidades

def test_centavos_e_formatacao():
    assert para_centavos("10.020.000,00") == 1_002_000_000
    assert para_centavos("1234.56") == 123456
    assert para_centavos(1234.565) == 123457
    assert para_centavos("") is None
    assert para_centavos("(1.000,00)") == -100000
    assert para_centavos("12", escala=1000) == 1_200_000
    assert reais(1_002_000_000) == "R$ 10.020.000,00"
    assert percentual(0.002) == "0,2%"
    assert percentual(None) == "Não calculável"


def test_diferencas_conforme_especificacao():
    d = diferenca(1_600_000_000, 1_000_000_000)
    assert d == {"assinada": 600_000_000, "absoluta": 600_000_000, "percentual": 0.6}
    zero = diferenca(300_000_000, 0)
    assert zero["absoluta"] == 300_000_000 and zero["percentual"] is None


def test_tolerancia_igualdade_com_limite_nao_alerta():
    # exatamente 1% e R$ 1.000: não excede (estritamente maior)
    assert not excede(100_000, 0.01, 1000.0, 0.01, "AMBAS")
    assert excede(100_001, 0.0101, 1000.0, 0.01, "AMBAS")
    assert not excede(2_000_000, 0.002, 1000.0, 0.01, "AMBAS")
    assert excede(2_000_000, 0.002, 1000.0, 0.01, "QUALQUER")
    assert excede(500, None, 1.0, 0.01, "AMBAS")  # percentual não calculável: decide o absoluto


def test_comparabilidade_bruto_liquido():
    dca = montar_fonte("SICONFI_DCA", [RegistroV(1, "SICONFI_DCA", "ISS_TOTAL", "TOTAL", "LIQUIDO", 800, "VALOR")])
    tce = montar_fonte("TCE", [RegistroV(2, "TCE", "ISS_TOTAL", "TOTAL", "BRUTO", 840, "VALOR")])
    assert comparar(dca, tce, "LIQUIDO").situacao == "NAO"
    tce_ded = montar_fonte("TCE", [
        RegistroV(3, "TCE", "ISS_TOTAL", "TOTAL", "BRUTO", 840, "VALOR"),
        RegistroV(4, "TCE", "ISS_OUTRAS_DEDUCOES", "OUTRAS_DEDUCOES", "DEDUCAO", 40, "VALOR"),
    ])
    comp = comparar(dca, tce_ded, "LIQUIDO")
    assert comp.situacao == "SIM" and comp.valor_fonte == 800 and "deduções" in comp.derivacao_fonte


def test_exigibilidade_2026():
    from datetime import datetime

    exigivel, motivo = exigibilidade(2026, datetime(2026, 10, 29))
    assert not exigivel and "30/04/2027" in motivo
    assert exigibilidade(2025, datetime(2026, 10, 29))[0]


# ----------------------------------------------------- cenários de aceitação

def test_cenario_1_divergencia_isolada(demo):
    destino, _ = demo
    session, caso = _caso(destino, 9900101)
    valores = expandir_valores(caso.valores)
    assert caso.valor_principal == 1_000_000_000
    assert round(valores["SIOPS"]["percentual"], 4) == 0.002
    assert round(valores["SIOPE"]["percentual"], 4) == 0.6
    assert valores["SIOPE"]["alertas"] == ["DAX"]
    assert valores["SIOPS"].get("alertas", []) == []
    alertas = session.scalars(select(cf.Alerta).where(cf.Alerta.caso_id == caso.id, cf.Alerta.regra == "DAX")).all()
    assert len(alertas) == 1 and alertas[0].declaracao_b == "SIOPE" and alertas[0].dif_abs == 600_000_000
    assert "R$ 16 milhões" in alertas[0].mensagem and "R$ 10 milhões" in alertas[0].mensagem and "60,0%" in alertas[0].mensagem
    session.close()


def test_cenario_2_ausencia_na_principal(demo):
    destino, _ = demo
    session, caso = _caso(destino, 9900102)
    assert caso.valor_principal is None and caso.valor_proposto is None
    assert "|AUS|" in caso.tipos
    assert "metodologia" in caso.motivo_proposta
    valores = expandir_valores(caso.valores)
    assert valores["SICONFI_DCA"]["estado"] == "AUSENTE"  # célula vazia, não zero
    assert valores["SIOPS"]["original"] == 510_000_000
    session.close()


def test_cenario_3_referencia_zero(demo):
    destino, _ = demo
    session, caso = _caso(destino, 9900103)
    alerta = session.scalar(select(cf.Alerta).where(cf.Alerta.caso_id == caso.id, cf.Alerta.regra == "ZER"))
    assert alerta is not None and alerta.dif_pct is None and alerta.dif_abs == 300_000_000
    assert "não é calculável" in alerta.mensagem
    assert expandir_valores(caso.valores)["SICONFI_DCA"]["estado"] == "ZERO"
    session.close()


def test_cenario_4_conceitos_distintos(demo):
    destino, _ = demo
    session, caso = _caso(destino, 9900104)
    valores = expandir_valores(caso.valores)
    assert valores["TCE"]["comparabilidade"] == "NAO"
    assert "bruto" in valores["TCE"]["motivo"]
    tipos = set(filter(None, caso.tipos.split("|")))
    assert "NCP" in tipos and "DAX" not in tipos  # incompatibilidade identificada, sem conclusão de erro
    assert caso.diagnostico == "SEM_VALIDACAO"
    session.close()


def test_cenario_5_alta_corroborada(demo):
    destino, _ = demo
    session, caso = _caso(destino, 9900105)
    alerta = session.scalar(select(cf.Alerta).where(cf.Alerta.caso_id == caso.id, cf.Alerta.regra == "VAR"))
    assert alerta is not None and alerta.gravidade == 2
    assert "corroborado por" in alerta.mensagem
    assert caso.valor_principal == 650_000_000 and caso.valor_proposto == 650_000_000  # preservado
    session.close()


def test_cenario_6_retificacao_apos_decisao(demo):
    destino, relatorio = demo
    assert relatorio["retificacao"]["novas_versoes"] == 1
    session, caso = _caso(destino, 9900106)
    decisao = decisao_vigente(session, 9900106, 2023, "ISS_MUN")
    assert decisao.valor_adotado == 700_000_000  # versão analisada
    assert caso.valor_principal == 770_000_000  # novo valor apresentado
    assert situacao_do_caso(caso, decisao) == "REANALISE"
    analisados = json.loads(decisao.registros_analisados)
    assert analisados["SICONFI_DCA"]["versao"] == 1
    registros = session.scalars(select(cf.Registro).where(cf.Registro.id_ente == 9900106, cf.Registro.exercicio == 2023,
                                                          cf.Registro.declaracao_id == "SICONFI_DCA", cf.Registro.componente_id == "ISS_MUN")).all()
    assert sorted((r.versao, r.vigente) for r in registros) == [(1, False), (2, True)]
    assert "|RET|" in caso.tipos
    session.close()


def test_cenario_7_reimportacao_identica(demo):
    destino, relatorio = demo
    assert relatorio["reimportacao"]["situacao"] == "JA_IMPORTADO"
    engine = criar_engine(destino)
    with Session(engine) as session:
        antes = session.scalar(select(func.count()).select_from(cf.Registro))
        lotes_antes = session.scalar(select(func.count()).select_from(cf.Lote))
        decisoes_antes = session.scalar(select(func.count()).select_from(cf.DecisaoCaso))
        r = importar_demo(session, "SIOPS", csv_de(dados_iniciais()["SIOPS"]), "FICTICIO_SIOPS.csv")
        session.commit()
        assert r["situacao"] == "JA_IMPORTADO"
        assert session.scalar(select(func.count()).select_from(cf.Registro)) == antes
        assert session.scalar(select(func.count()).select_from(cf.Lote)) == lotes_antes
        assert session.scalar(select(func.count()).select_from(cf.DecisaoCaso)) == decisoes_antes
        # mesmo conteúdo com outro nome/cabeçalho: valores iguais aos vigentes não duplicam
        outro = csv_de(dados_iniciais()["SIOPS"]).replace(b"ISS", b"ISS")
        r2 = importacao.importar(session, importacao.ler("outro_nome.csv", outro + b"\n"), importacao.Mapeamento(
            declaracao="SIOPS", componente="ISS_MUN", aba="CSV", linha_cabecalho=1, coluna_ente="COD_IBGE",
            coluna_exercicio="ANO", exercicio_fixo=None,
            campos=[importacao.CampoValor("ISS", "ISS_TOTAL", "TOTAL", "LIQUIDO")], ficticio=True), "teste")
        session.commit()
        assert r2["novos"] == 0 and r2["novas_versoes"] == 0 and r2["repetidos"] > 0
        assert session.scalar(select(func.count()).select_from(cf.Registro)) == antes


def test_cenario_8_decisao_persistente(demo, tmp_path):
    destino, _ = demo
    app = criar_app(destino)
    with TestClient(app) as cliente:
        _entrar(cliente, destino, "Teste", "ANALISTA", "/")
        session, caso = _caso(destino, 9900103)
        session.close()
        resposta = cliente.post(f"/caso/{caso.id}/decidir", data={"acao": "ADOTAR_FONTE", "justificativa": ""})
        assert resposta.status_code == 400 and "justificativa" in resposta.json()["erro"]
        registro = expandir_valores(caso.valores)["SIOPS"]["registro"]
        resposta = cliente.post(f"/caso/{caso.id}/decidir", data={
            "acao": "ADOTAR_FONTE", "registro_id": str(registro),
            "justificativa": "DCA declarou zero por erro de preenchimento; SIOPS e SIOPE coincidem (teste).",
            "evidencias": "Documento fictício",
        })
        assert resposta.json()["ok"]
    # "recarregar a aplicação": nova instância, nova conexão
    app2 = criar_app(destino)
    with TestClient(app2) as cliente:
        _entrar(cliente, destino, "Outra pessoa", "REVISOR", "/")
        pagina = cliente.get(f"/caso/{caso.id}?parcial=1").text
        assert "DCA declarou zero por erro de preenchimento" in pagina
        assert "Teste" in pagina and "Valor ajustado" in pagina
    conexao = sqlite3.connect(destino)
    linha = conexao.execute("select valor_adotado, declaracao_adotada, usuario from cf_decisao where id_ente=9900103 and vigente=1").fetchone()
    assert linha == (300_000_000, "SIOPS", "Teste")


def test_parametro_novo_nao_apaga_decisoes(demo):
    destino, _ = demo
    engine = criar_engine(destino)
    with Session(engine) as session:
        antes = session.scalar(select(func.count()).select_from(cf.DecisaoCaso))
        caso_antes = caso_por_chave(session, 9900101, 2023, "ISS_MUN")
        situacao_antes = situacao_do_caso(caso_antes, decisao_vigente(session, 9900101, 2023, "ISS_MUN"))
        conjunto = session.scalar(select(cf.ConjuntoRegras).where(cf.ConjuntoRegras.vigente.is_(True)))
        config = json.loads(conjunto.parametros)
        config["regras"]["DAX"]["parametros"]["tolerancia_relativa"] = 0.7
        conjunto.vigente = False
        session.add(cf.ConjuntoRegras(criado_em=conjunto.criado_em, criado_por="teste", parametros=json.dumps(config), vigente=True))
        session.commit()
    executar(engine, "teste")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(cf.DecisaoCaso)) == antes
        caso = caso_por_chave(session, 9900101, 2023, "ISS_MUN")
        assert "DAX" not in caso.tipos  # 60% < 70%
        decisao = decisao_vigente(session, 9900101, 2023, "ISS_MUN")
        # mudar parâmetro não muda a situação da análise (só novos dados mudam)
        assert decisao is not None and situacao_do_caso(caso, decisao) == situacao_antes


def test_interface_central_filtros_e_exportacao(demo):
    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        assert cliente.get("/", follow_redirects=False).status_code == 303
        _entrar(cliente, destino, "Leitor", "ANALISTA", "/")
        pagina = cliente.get("/").text
        assert "DADOS FICTÍCIOS" in pagina
        assert "Fictício C" in pagina and "Fictício G" not in pagina  # somente casos com alerta
        todos = cliente.get("/?enviado=1&q=9900107").text
        assert "Fictício G" in todos
        filtrado = cliente.get("/?enviado=1&somente_alerta=1&tipo=ZER").text
        assert "Fictício C" in filtrado and "Fictício B" not in filtrado
        csv = cliente.get("/resultado/exportar?formato=csv&enviado=1&somente_alerta=1").content.decode("utf-8-sig")
        assert csv.splitlines()[0].startswith("execucao;data_corte;codigo_ibge")
        assert "9900103" in csv
        pacote = zipfile.ZipFile(io.BytesIO(cliente.get("/resultado/pacote?enviado=1&q=9900106").content))
        assert {"resultado.csv", "alertas.csv", "decisoes.csv", "registros.csv", "parametros.json", "execucao.json"} <= set(pacote.namelist())
        assert "não" in pacote.read("registros.csv").decode("utf-8-sig")  # versão 1 não vigente presente
        for rota in ("/resultado", "/importar", "/dados", "/cobertura", "/regras", "/fontes"):
            assert cliente.get(rota).status_code == 200, rota


def test_importacao_pela_tela_com_previa_e_erros(demo):
    destino, _ = demo
    conteudo = "codigo;exercicio;valor declarado\n9900107;2024;1.234.567,89\n1234567;2024;10\n9900108;2024;abc\n9900108;2024;5\n".encode("utf-8")
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Importador", "ANALISTA", "/")
        tela = cliente.post("/importar/arquivo", files={"arquivo": ("balanco_teste.csv", conteudo, "text/csv")}).text
        token = re.search(r'name="token" value="([0-9a-f]+)"', tela).group(1)
        assert "valor declarado" in tela
        resposta = cliente.post("/importar/mapear", data={
            "token": token, "aba": "CSV", "linha_cabecalho": "1", "acao": "confirmar", "declaracao": "BALANCO",
            "componente": "ISS_MUN", "coluna_ente": "codigo", "coluna_exercicio": "exercicio",
            "valor_coluna_1": "valor declarado", "valor_campo_1": "ISS_TOTAL", "valor_parcela_1": "TOTAL", "valor_conceito_1": "LIQUIDO",
        }).text
        assert "Importação concluída" in resposta
        assert "Código do ente não encontrado" in resposta
        assert "Formato numérico inválido" in resposta
        assert "Duplicado no arquivo" in resposta
    conexao = sqlite3.connect(destino)
    estados = dict(conexao.execute("select estado, count(*) from cf_registro where declaracao_id='BALANCO' group by estado").fetchall())
    assert estados.get("VALOR", 0) >= 2 and estados.get("INVALIDO") == 1
    valor = conexao.execute("select valor_centavos from cf_registro where declaracao_id='BALANCO' and id_ente=9900107").fetchone()[0]
    assert valor == 123456789


def test_migracao_preserva_versao_1(tmp_path):
    from datetime import datetime

    from validacao.conferencia.migracao import migrar
    from validacao.db import criar_tabelas
    from validacao.modelos import Carga, Decisao, Ente, Usuario, ValorObservado
    from validacao.catalogo import semear

    destino = tmp_path / "v1.sqlite"
    engine = criar_engine(destino)
    criar_tabelas(engine)
    with Session(engine) as session:
        semear(session)
        session.add(Ente(id_ente=2927408, esfera="MUNICIPIO", uf="BA", nome="Prefeitura Municipal de Salvador - BA", capital=True, tribunal_competente="TCM-BA"))
        session.add(Usuario(id=1, nome="Ana", perfil="ANALISTA"))
        session.add(Carga(id=1, fonte_id="SICONFI", arquivo_ou_endpoint="C:\\pasta\\SICONFI_DCA.xlsx", hash="h1",
                          data_extracao=datetime(2026, 10, 14), data_carga=datetime(2026, 10, 29), exercicios="2024",
                          usuario="carga_local", situacao="CONCLUIDA"))
        session.flush()
        session.add(ValorObservado(carga_id=1, id_ente=2927408, ano=2024, componente_id="ISS_TOTAL", valor=1979102342.31, conta_origem="ISS", linha_origem=10))
        session.add(Decisao(id_ente=2927408, ano=2024, bloco="ISS", acao="USAR_SUGERIDA", fonte="SICONFI", valor=1979102342.31,
                            usuario_id=1, data=datetime(2026, 10, 29, 15, 51), cargas_usadas='{"SICONFI": 1}', estado="VIGENTE"))
        session.commit()
    resumo = migrar(engine)
    assert {k: resumo[k] for k in ("lotes", "registros", "decisoes")} == {"lotes": 1, "registros": 1, "decisoes": 1}
    segundo = migrar(engine)  # idempotente
    assert {k: segundo[k] for k in ("lotes", "registros", "decisoes")} == {"lotes": 0, "registros": 0, "decisoes": 0}
    assert segundo["atualizacao"]["registros_copiados"] == 0
    executar(engine, "teste")
    with Session(engine) as session:
        registro = session.scalar(select(cf.Registro))
        assert registro.valor_centavos == 197910234231
        assert json.loads(registro.procedencia)["linha"] == 10
        decisao = session.scalar(select(cf.DecisaoCaso))
        assert decisao.origem == "MIGRADA_V1" and decisao.decisao_legada_id == 1 and decisao.usuario == "Ana"
        assert decisao.impressao != ""  # registrada na primeira validação
        caso = caso_por_chave(session, 2927408, 2024, "ISS_MUN")
        assert caso.nome_ente == "Salvador"
        assert situacao_do_caso(caso, decisao) == "ESCLARECIDO"
        assert session.scalar(select(func.count()).select_from(ValorObservado)) == 1


# ------------------------------------------- componentes e pacote SICONFI

def test_pacote_siconfi_rreo_msc_e_consultas(demo):
    destino, relatorio = demo
    pacote = relatorio["pacote"]
    assert pacote["SICONFI_RREO"]["novos"] > 0 and pacote["SICONFI_MSC"]["novos"] > 0
    assert pacote["status"] > 0
    session, caso = _caso(destino, 9900101, 2022)
    assert "|DSI|" in caso.tipos  # RREO 8% acima da DCA
    valores = expandir_valores(caso.valores)
    assert valores["SICONFI_RREO"]["comparabilidade"] == "SIM"
    falha = session.scalar(select(cf.Consulta).where(cf.Consulta.id_ente == 9900103, cf.Consulta.ano == 2023, cf.Consulta.fonte == "MSC"))
    assert falha is not None and falha.ok is False
    # reimportar o mesmo pacote não duplica
    from validacao.conferencia.pacote_siconfi import importar_pacote

    antes = session.scalar(select(func.count()).select_from(cf.Registro))
    session.close()
    segundo = importar_pacote(criar_engine(destino), Path(destino).parent / "pacote_siconfi_ficticio", "teste")
    assert segundo["SICONFI_RREO"]["situacao"] == "JA_IMPORTADO"
    with Session(criar_engine(destino)) as s2:
        assert s2.scalar(select(func.count()).select_from(cf.Registro)) == antes


def test_inferencia_fundeb_na_cota():
    dca = montar_fonte("SICONFI_DCA", [
        RegistroV(1, "SICONFI_DCA", "COTA_RECEBIDA_TOTAL", "TOTAL", "LIQUIDO", 1_000_000, "VALOR"),
        RegistroV(2, "SICONFI_DCA", "COTA_RECEBIDA_FUNDEB", "FUNDEB", "INFORMATIVO", 200_000, "VALOR"),
    ])
    tce = montar_fonte("TCE", [RegistroV(3, "TCE", "COTA_RECEBIDA_TOTAL", "TOTAL", "BRUTO", 800_000, "VALOR")])
    siope = montar_fonte("SIOPE", [RegistroV(4, "SIOPE", "COTA_RECEBIDA_TOTAL", "TOTAL", "LIQUIDO", 800_000, "VALOR")])
    comp = comparar(dca, siope, "LIQUIDO")
    assert comp.situacao == "RESSALVA" and comp.inferencia == "FUNDEB" and comp.absoluta == 0
    assert "FUNDEB" in comp.motivo
    assert comparar(dca, tce, "LIQUIDO").situacao == "NAO"  # bruto sem deduções: não comparável


def test_conciliacao_cota_na_uf():
    from validacao.conferencia.regras import Contexto, avaliar

    icms = montar_fonte("SICONFI_DCA", [
        RegistroV(1, "SICONFI_DCA", "ICMS_TOTAL", "TOTAL", "LIQUIDO", 4_000_000_00, "VALOR"),
        RegistroV(2, "SICONFI_DCA", "COTA_REPASSADA", "COTA_REPASSADA", "INFORMATIVO", 12_000_00, "VALOR"),
    ])
    ctx = Contexto(id_ente=13, nome_ente="Amazonas", uf="AM", exercicio=2023, exigivel=True, motivo_exigibilidade="",
                   principal="SICONFI_DCA", precedencia=["SICONFI_DCA"], conceito_ref="LIQUIDO",
                   fontes={"SICONFI_DCA": icms}, comparacoes={}, grupos={"SICONFI_DCA": "SICONFI"}, series={},
                   repeticoes_entes={}, componente="ICMS_UF", extras={"cota_municipal_uf": 1_000_000_00, "municipios_uf": 62})
    alertas = [a for a in avaliar(ctx, {}) if a.regra == "UFC"]
    assert len(alertas) == 1 and "62 municípios" in alertas[0].mensagem and "0,3%" in alertas[0].mensagem


def test_df_vira_componentes_proprios(tmp_path):
    from datetime import datetime

    from validacao.catalogo import semear
    from validacao.conferencia.migracao import migrar
    from validacao.db import criar_tabelas
    from validacao.modelos import Carga, Ente, ValorObservado

    engine = criar_engine(tmp_path / "df.sqlite")
    criar_tabelas(engine)
    with Session(engine) as session:
        semear(session)
        session.add(Ente(id_ente=53, esfera="DF", uf="DF", nome="Governo do Distrito Federal", capital=False, tribunal_competente="TCDF"))
        session.add(Carga(id=1, fonte_id="SICONFI", arquivo_ou_endpoint="dca.xlsx", hash="h", data_extracao=datetime(2026, 10, 1),
                          data_carga=datetime(2026, 10, 1), exercicios="2024", usuario="x", situacao="CONCLUIDA"))
        session.flush()
        session.add(ValorObservado(carga_id=1, id_ente=53, ano=2024, componente_id="ISS_TOTAL", valor=10.0))
        session.add(ValorObservado(carga_id=1, id_ente=53, ano=2024, componente_id="ICMS_TOTAL", valor=20.0))
        session.commit()
    migrar(engine)
    executar(engine, "teste")
    with Session(engine) as session:
        componentes = set(session.scalars(select(cf.Registro.componente_id)))
        assert componentes == {"ISS_DF", "ICMS_DF"}
        caso = caso_por_chave(session, 53, 2024, "ICMS_DF")
        assert caso is not None and caso.valor_principal == 2000 and caso.nome_ente == "Distrito Federal"


def test_paineis_visao_e_recuperacao(demo):
    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Gestor", "ANALISTA", "/visao")
        visao = cliente.get("/visao").text
        assert "Visão geral da base de referência" in visao and "Cota-parte do ICMS" in visao
        assert "Concordância das fontes" in visao
        rec = cliente.get("/recuperacao?componente=ISS_MUN&filtro=falha_consulta").text
        assert "Fictício C" in rec and "Fictício A" not in rec
        assert "Consulta com erro" in rec


def test_dados_dos_alertas_no_painel_do_caso(demo):
    from validacao.conferencia.evidencia import GERADORES, dados_alertas
    from validacao.conferencia.consultas import detalhe

    destino, _ = demo
    engine = criar_engine(destino)
    with Session(engine) as session:
        execucao = session.scalar(select(func.max(cf.Execucao.id)))
        alertas = list(session.scalars(select(cf.Alerta).where(cf.Alerta.execucao_id == execucao)))
        regras = set()
        for alerta in alertas:
            if alerta.regra not in GERADORES:
                continue
            caso = session.get(cf.Caso, alerta.caso_id)
            tabela = dados_alertas(session, caso, detalhe(session, caso))[alerta.id]
            assert "Não foi possível" not in (tabela["nota"] or ""), (alerta.regra, tabela["nota"])
            regras.add(alerta.regra)
        assert {"DSI", "AUS", "VAR"} <= regras
        caso_id = next(a.caso_id for a in alertas if a.regra == "DSI")
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        painel = cliente.get(f"/caso/{caso_id}?parcial=1").text
        assert "Ver os dados que geraram o alerta" in painel and "Valor usado na comparação" in painel


def test_dados_da_repeticao_entre_entes_e_exercicios(tmp_path):
    """Dois entes com o mesmo valor em dois exercícios: a tela lista o outro ente e os exercícios coincidentes."""
    from validacao.conferencia.evidencia import dados_alertas
    from validacao.conferencia.consultas import detalhe

    destino = tmp_path / "rep.sqlite"
    construir(destino)
    engine = criar_engine(destino)
    with Session(engine) as session:
        entes = [e for e, in session.execute(select(cf.Registro.id_ente).where(cf.Registro.componente_id == "ISS_MUN").distinct().limit(2))]
    conteudo = ("COD_IBGE;ANO;ISS\n" + "".join(f"{e};{a};777777,77\n" for e in entes for a in (2021, 2022))).encode()
    with Session(engine) as session:
        importar_demo(session, "SICONFI_DCA", conteudo, "repeticao.csv")
        session.commit()
    executar(engine, "teste", componente="ISS_MUN")
    with Session(engine) as session:
        caso = caso_por_chave(session, entes[0], 2022, "ISS_MUN")
        info = detalhe(session, caso)
        dados = dados_alertas(session, caso, info)
        por_regra = {a.regra: dados[a.id] for a in info["alertas"] if a.id in dados}
        assert "REE" in por_regra and "REP" in por_regra
        ree = por_regra["REE"]["linhas"]
        assert str(entes[1]) in [l["celulas"][2] for l in ree]
        outro = next(l for l in ree if l["celulas"][2] == str(entes[1]))
        assert outro["celulas"][4] == "2021" and outro["caso"]
        rep = [l["celulas"][0] for l in por_regra["REP"]["linhas"] if l["destaque"]]
        assert rep == ["2021", "2022"]


def test_tela_de_importacao_mostra_etapas_conferencia_e_filtro(demo):
    destino, relatorio = demo
    diagnostico = relatorio["pacote"]["diagnostico"]
    textos = " ".join(t for _n, t in diagnostico["observacoes"])
    assert "conferem, linha a linha" in textos and "confere com valor_msc" in textos
    assert any("falharam" in t for t in [t for _n, t in diagnostico["observacoes"]])  # ente C, MSC 2023
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        pagina = cliente.get("/importar").text
        assert "O que entrou no sistema" in pagina and "Conferência dos arquivos" in pagina
        assert "Importado e validado" in pagina
        # o link de divergências leva à central só com casos em que a fonte está no alerta DSI
        central = cliente.get("/?enviado=1&componente=ISS_MUN&tipo=DSI&fonte=SICONFI_RREO").text
        assert "Fictício A" in central
        vazio = cliente.get("/?enviado=1&componente=ISS_MUN&tipo=DSI&fonte=SIOPS").text
        assert "Fictício A" not in vazio


def test_central_mostra_coluna_dca_antes_do_rreo(demo):
    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        html = cliente.get("/").text
        cabecalhos = re.findall(r'<th class="num"[^>]*>([^<]+)</th>', html)
        assert cabecalhos[:4] == ["Principal", "DCA", "RREO", "MSC"]
        assert 'class="fonte ref-col"' in html  # a DCA de referência mostra o valor, não só "referência"


def _v(original, comp="SIM", assinada=0, pct=0.0, estado="VALOR", alertas=()):
    return {"original": original, "normalizado": original, "estado": estado, "comparabilidade": comp,
            "assinada": assinada, "percentual": pct, "alertas": list(alertas)}


def test_escala_de_confianca():
    from validacao.conferencia.confianca import classificar

    ref = {"SICONFI_DCA": _v(1_000_000_00, "REFERENCIA", None, None)}
    def caso(**fontes):
        return {**ref, **fontes}
    iguais = dict(SIOPE=_v(1_000_000_00), SIOPS=_v(1_000_000_00), TCE=_v(1_000_000_00))
    assert classificar("ISS_MUN", "SICONFI_DCA", caso(**iguais), "EXIGIVEL", "CONFERIDO")[0] == 5
    # terceira independente diverge -> 4
    div = dict(iguais, TCE=_v(1_300_000_00, assinada=300_000_00, pct=0.3))
    assert classificar("ISS_MUN", "SICONFI_DCA", caso(**div), "EXIGIVEL", "ALERTA")[0] == 4
    # repetição sobre a DCA rebaixa para 4
    rep = {"SICONFI_DCA": _v(1_000_000_00, "REFERENCIA", None, None, alertas=["REP"]), **iguais}
    assert classificar("ISS_MUN", "SICONFI_DCA", rep, "EXIGIVEL", "ALERTA")[0] == 4
    # ICMS estadual limitado a 4
    nivel, motivo = classificar("ICMS_UF", "SICONFI_DCA", caso(**iguais), "EXIGIVEL", "CONFERIDO")
    assert nivel == 4 and "Limitado" in motivo
    # uma independente, nenhuma diverge -> 3; uma concorda e outra diverge -> 2
    assert classificar("ISS_MUN", "SICONFI_DCA", caso(SIOPE=_v(1_000_000_00)), "EXIGIVEL", "CONFERIDO")[0] == 3
    um_div = caso(SIOPE=_v(1_000_000_00), SIOPS=_v(1_500_000_00, assinada=500_000_00, pct=0.5))
    assert classificar("ISS_MUN", "SICONFI_DCA", um_div, "EXIGIVEL", "ALERTA")[0] == 2
    # padrão FUNDEB na cota: independentes = DCA / 0,8 -> 3
    fundeb = caso(SIOPE=_v(1_250_000_00, assinada=250_000_00, pct=0.25), SIOPS=_v(1_250_000_00, assinada=250_000_00, pct=0.25))
    nivel, motivo = classificar("COTA_ICMS_MUN", "SICONFI_DCA", fundeb, "EXIGIVEL", "ALERTA")
    assert nivel == 3 and "FUNDEB" in motivo
    # no ISS o mesmo padrão é DCA divergente com consenso -> 1
    assert classificar("ISS_MUN", "SICONFI_DCA", fundeb, "EXIGIVEL", "ALERTA")[0] == 1
    # DCA zero com independentes de acordo -> 1; sem consenso -> 0
    zero = {"SICONFI_DCA": _v(0, "REFERENCIA", None, None, estado="ZERO"),
            "SIOPE": _v(500_000_00, assinada=500_000_00, pct=None), "SIOPS": _v(500_000_00, assinada=500_000_00, pct=None)}
    nivel, motivo = classificar("ISS_MUN", "SICONFI_DCA", zero, "EXIGIVEL", "ALERTA")
    assert nivel == 1 and motivo.startswith("DCA declara zero")
    zero["SIOPS"] = _v(900_000_00, assinada=900_000_00, pct=None)
    assert classificar("ISS_MUN", "SICONFI_DCA", zero, "EXIGIVEL", "ALERTA")[0] == 0
    # sem DCA: RREO confirmado -> 3; sem DCA e RREO com consenso -> 2; nada -> 0
    rreo = {"SICONFI_RREO": _v(1_000_000_00, "REFERENCIA", None, None), "SIOPS": _v(1_000_000_00)}
    assert classificar("ISS_MUN", "SICONFI_RREO", rreo, "EXIGIVEL", "SEM_VALIDACAO")[0] == 3
    so_ind = {"SIOPE": _v(1_000_000_00, "SEM_REFERENCIA", None, None), "SIOPS": _v(1_000_500_00, "SEM_REFERENCIA", None, None)}
    assert classificar("ISS_MUN", None, so_ind, "EXIGIVEL", "ALERTA")[0] == 2
    assert classificar("ISS_MUN", None, {}, "EXIGIVEL", "ALERTA")[0] == 0
    # não exigível e fora do universo -> não avaliado
    assert classificar("ISS_MUN", None, {}, "NAO_EXIGIVEL", "NAO_EXIGIVEL")[0] is None
    assert classificar("ISS_MUN", "SICONFI_DCA", caso(**iguais), "EXIGIVEL", "EXCLUIDO")[0] is None


def test_coluna_analise_na_central_e_atualizacao_de_banco_antigo(demo, tmp_path):
    import shutil
    from validacao.conferencia.migracao import atualizar_estrutura

    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        html = cliente.get("/?enviado=1&somente_alerta=").text
        assert ">Análise</th>" in html and 'class="confianca c' in html
        baixos = cliente.get("/?enviado=1&somente_alerta=&confianca=ate2&ordem=confianca").text
        assert 'class="confianca c5"' not in baixos
    # banco anterior (sem as colunas): a atualização cria e preenche sem revalidar
    from validacao.db import copiar_sqlite

    copia = tmp_path / "antigo.sqlite"
    copiar_sqlite(destino, copia)  # com WAL, copiar só o arquivo .sqlite perderia o que está no -wal
    with sqlite3.connect(copia) as conexao:
        conexao.execute("ALTER TABLE cf_caso DROP COLUMN confianca")
        conexao.execute("ALTER TABLE cf_caso DROP COLUMN confianca_motivo")
        conexao.execute("UPDATE cf_configuracao SET valor = '\"2\"' WHERE chave = 'versao_esquema'")
    resumo = atualizar_estrutura(criar_engine(copia))
    assert resumo["casos_com_confianca"] > 0
    with sqlite3.connect(copia) as conexao:
        sem = conexao.execute("SELECT count(*) FROM cf_caso WHERE confianca IS NULL AND situacao_exercicio = 'EXIGIVEL' AND diagnostico != 'EXCLUIDO'").fetchone()[0]
        assert sem == 0


def test_coluna_dca_continua_apos_aplicar_filtros(demo):
    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        html = cliente.get("/").text
        assert 'name="col" value="SICONFI_DCA" checked' in html


def test_composicao_do_valor_por_fonte():
    from types import SimpleNamespace as N
    from validacao.conferencia.composicao import composicao_fonte

    def reg(i, campo, parcela, conceito, valor):
        return (N(id=i, declaracao_id="SICONFI_DCA", campo=campo, parcela=parcela, conceito=conceito, valor_centavos=valor,
                  estado="VALOR", periodo="ANUAL", versao=1, vigente=True, duplicado=None, lote_id=1, data_extracao=None,
                  procedencia='{"arquivo": "dca.xlsx", "linha": 7}'), N(id=1, arquivo="dca.xlsx"))
    info = {"registros": [
        reg(1, "ISS_TOTAL", "TOTAL", "LIQUIDO", 900_00), reg(2, "ISS_BRUTO", "BRUTO", "BRUTO", 1_000_00),
        reg(3, "ISS_OUTRAS_DEDUCOES", "OUTRAS_DEDUCOES", "DEDUCAO", -100_00), reg(4, "ISS_FUNDEB", "FUNDEB", "INFORMATIVO", 180_00),
    ], "valores": {"SICONFI_DCA": {"normalizado": 900_00, "comparabilidade": "REFERENCIA"}}}
    k = composicao_fonte(info, "SICONFI_DCA")
    assert [l["sinal"] for l in k["linhas"]] == ["+", "−"]
    assert k["conta"][-1]["valor"] == 900_00 and "confere" in k["conferencia"]
    assert k["liquido_fundeb_calculado"] == 720_00
    assert {m["valor"] for m in k["medidas"]} >= {900_00, 1_000_00, 720_00}


def test_autocompletar_de_entes(demo):
    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        por_nome = cliente.get("/api/entes?q=ficticio a").json()
        assert por_nome and por_nome[0]["id"] == 9900101  # sem acento e sem diferença de maiúsculas
        por_codigo = cliente.get("/api/entes?q=990010").json()
        assert {i["id"] for i in por_codigo} >= {9900101, 9900102}
        assert cliente.get("/api/entes?q=").json() == []
        assert "data-autocompletar-ente" in cliente.get("/").text


def _esperar_tarefa(banco, tarefa_id, limite=180):
    import time as _t

    from validacao.conferencia import tarefas

    inicio = _t.time()
    while _t.time() - inicio < limite:
        estado = tarefas.estado(banco, tarefa_id)
        if estado and not estado.get("rodando"):
            return estado
        _t.sleep(0.3)
    raise AssertionError(f"tarefa {tarefa_id} não terminou")


def test_tarefas_em_processo_separado(demo, tmp_path):
    """Validação e exportação rodam fora do servidor; a tela só acompanha o andamento."""
    from validacao.db import copiar_sqlite

    destino, _ = demo
    banco = tmp_path / "tarefas.sqlite"
    copiar_sqlite(destino, banco)
    with TestClient(criar_app(banco)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        antes = Session(criar_engine(banco)).scalar(select(func.max(cf.Execucao.id)))
        resposta = cliente.post("/validar").json()
        assert resposta["ok"]
        assert cliente.post("/validar").status_code == 409  # uma escrita por vez
        estado = _esperar_tarefa(banco, resposta["tarefa"])
        assert not estado["erro"], estado
        assert estado["execucao"] > antes
        assert cliente.get("/validar/estado").json()["execucao"] == estado["execucao"]
        for formato in ("csv", "xlsx", "zip"):
            inicio = cliente.post("/resultado/exportar/iniciar", data={"formato": formato, "query": "enviado=1&somente_alerta="}).json()
            final = _esperar_tarefa(banco, inicio["tarefa"])
            assert not final["erro"], final
            arquivo = cliente.get(f"/tarefas/{inicio['tarefa']}/arquivo")
            assert arquivo.status_code == 200 and len(arquivo.content) > 200
            assert final["nome_arquivo"].endswith(formato)


def test_cache_dos_paineis_muda_com_decisao(demo, tmp_path):
    from validacao.db import copiar_sqlite

    destino, _ = demo
    banco = tmp_path / "cache.sqlite"
    copiar_sqlite(destino, banco)
    with TestClient(criar_app(banco)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        primeira = cliente.get("/visao").text
        assert cliente.get("/visao").text == primeira  # segunda visita vem do cache
        engine = criar_engine(banco)
        with Session(engine) as session:
            caso = session.scalar(select(cf.Caso).where(cf.Caso.diagnostico == "ALERTA").order_by(cf.Caso.id.desc()))
            caso_id = caso.id
        r = cliente.post(f"/caso/{caso_id}/decidir", data={"acao": "MANTER_PRINCIPAL", "justificativa": "Conferido no balanço."})
        assert r.json()["ok"], r.text
        depois = cliente.get("/visao").text
        assert depois != primeira  # a decisão muda a versão dos dados e o painel é recalculado


def test_busca_de_ente_sem_acento_e_dados_brutos(demo):
    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        assert "Fictício A" in cliente.get("/?enviado=1&somente_alerta=&q=ficticio a").text
        pagina = cliente.get("/dados?componente=ISS_MUN&q=ficticio").text
        assert "Fictício" in pagina
        assert cliente.get("/dados?componente=ISS_MUN&pagina=2").status_code == 200


def test_banco_copiado_para_pasta_local(tmp_path, monkeypatch):
    import validacao.db as db
    from validacao.db import copiar_sqlite

    legado = tmp_path / "onedrive" / "data"
    local = tmp_path / "local"
    construir(legado / "validacao.sqlite")
    monkeypatch.setattr(db, "PASTA_DADOS_LEGADO", legado)
    monkeypatch.setattr(db, "BANCO_LEGADO", legado / "validacao.sqlite")
    monkeypatch.setattr(db, "PASTA_DADOS", local)
    monkeypatch.setattr(db, "BANCO_PADRAO", local / "validacao.sqlite")
    (local).mkdir(parents=True, exist_ok=True)
    (local / "validacao.copiando").write_bytes(b"sobra de tentativa interrompida")
    novo = db.mover_para_pasta_local(progresso=lambda m: None)
    assert novo == local / "validacao.sqlite" and novo.exists()
    assert not (local / "validacao.copiando").exists()
    # nenhum arquivo do banco fica aberto (no Windows, arquivo aberto impede renomear: WinError 32)
    import os as _os
    abertos = {_os.path.realpath(f"/proc/self/fd/{fd}") for fd in _os.listdir("/proc/self/fd")} if _os.path.isdir("/proc/self/fd") else set()
    assert not any(str(local) in caminho for caminho in abertos), abertos  # (o legado fica aberto pelo construir do teste)
    assert (legado / "validacao.sqlite").exists()  # o original fica
    assert "banco movido" in " ".join(p.name for p in legado.iterdir())
    assert db.mover_para_pasta_local(progresso=lambda m: None) is None  # só uma vez
    with sqlite3.connect(novo) as conexao:
        assert conexao.execute("select count(*) from cf_caso").fetchone()[0] > 0
    assert db.em_pasta_sincronizada(tmp_path / "OneDrive - SEFAZ" / "x.sqlite")
    assert not db.em_pasta_sincronizada(local / "x.sqlite")


def test_controle_de_acesso(demo, monkeypatch):
    from validacao.conferencia import acesso as contas

    destino, _ = demo
    with TestClient(criar_app(destino)) as cliente:
        assert cliente.get("/saude").json() == {"ok": True}
        # sem login, nenhum dado: telas redirecionam e o estado das tarefas é recusado
        for rota in ("/", "/visao", "/dados", "/resultado", "/usuarios", "/api/entes?q=a"):
            assert cliente.get(rota, follow_redirects=False).status_code in (303, 307), rota
        assert cliente.get("/validar/estado").status_code == 401
        assert 'name="senha"' in cliente.get("/login").text
        assert cliente.post("/login", data={"login": "nao.existe", "senha": "x"}).status_code == 401
        # a conta admin nasce com a senha inicial (hash no código) e só o administrador vê Usuários
        with Session(criar_engine(destino)) as session:
            admin = session.scalar(select(cf.Acesso).where(cf.Acesso.login == "admin"))
            assert admin is not None and admin.senha_hash == contas.HASH_ADMIN_INICIAL
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        assert cliente.get("/usuarios").status_code == 403
        assert "/usuarios" not in cliente.get("/visao").text
        cliente.post("/sair")
        _entrar(cliente, destino, "Chefe", "ADMINISTRADOR", "/")
        pagina = cliente.get("/usuarios").text
        assert "Contas de acesso" in pagina and "t.analista" in pagina and "admin" in pagina
        # senha fraca recusada; conta nova com troca obrigatória
        fraca = cliente.post("/usuarios", data={"login": "maria.souza", "nome": "Maria Souza", "perfil": "ANALISTA",
                                                 "senha": "curta", "trocar_senha": "1"})
        assert "pelo menos 12" in fraca.text
        provisoria = "Prov1s0ria!Forte"
        criada = cliente.post("/usuarios", data={"login": "Maria.Souza", "nome": "Maria Souza", "perfil": "REVISOR",
                                                  "senha": provisoria, "trocar_senha": "1"})
        assert "maria.souza" in criada.text and provisoria in criada.text
        assert provisoria not in cliente.get("/usuarios").text  # a senha aparece uma vez só
        with Session(criar_engine(destino)) as session:
            maria = session.scalar(select(cf.Acesso).where(cf.Acesso.login == "maria.souza"))
            assert provisoria not in maria.senha_hash and contas.conferir_senha(provisoria, maria.senha_hash)
            maria_id = maria.usuario_id
    with TestClient(criar_app(destino)) as maria:
        resposta = maria.post("/login", data={"login": "maria.souza", "senha": provisoria, "volta": "/visao"})
        assert resposta.url.path == "/minha-senha"
        assert maria.get("/visao", follow_redirects=False).headers["location"] == "/minha-senha"
        assert maria.post("/minha-senha", data={"atual": provisoria, "nova": "Outra#Senha2026x",
                                                "confirmacao": "Outra#Senha2026y"}).status_code == 400
        trocou = maria.post("/minha-senha", data={"atual": provisoria, "nova": "Outra#Senha2026x",
                                                  "confirmacao": "Outra#Senha2026x"})
        assert trocou.url.path == "/visao" and "Senha alterada" in trocou.text
        # desativada pelo administrador: a sessão aberta cai na hora
        with Session(criar_engine(destino)) as session:
            chefe = session.scalar(select(cf.Acesso).where(cf.Acesso.login == "t.chefe"))
            contas.alterar_conta(session, maria_id, chefe.usuario_id, ativo=False)
        assert maria.get("/visao", follow_redirects=False).status_code == 303
        recusa = maria.post("/login", data={"login": "maria.souza", "senha": "Outra#Senha2026x"})
        assert recusa.status_code == 401 and "desativada" in recusa.text
    # bloqueio após 5 tentativas
    with Session(criar_engine(destino)) as session:
        contas.alterar_conta(session, maria_id, 0, ativo=True)
    with TestClient(criar_app(destino)) as cliente:
        for _ in range(5):
            cliente.post("/login", data={"login": "maria.souza", "senha": "errada"})
        bloqueada = cliente.post("/login", data={"login": "maria.souza", "senha": "Outra#Senha2026x"})
        assert bloqueada.status_code == 401 and "bloqueada" in bloqueada.text
    # o último administrador não pode ser rebaixado nem desativado
    from validacao.modelos import Usuario

    with Session(criar_engine(destino)) as session:
        admins = [a for a in session.scalars(select(cf.Acesso)).all()
                  if session.get(Usuario, a.usuario_id).perfil == "ADMINISTRADOR"]
        for a in admins[1:]:
            contas.alterar_conta(session, a.usuario_id, 0, ativo=False)
        with pytest.raises(contas.AcessoRecusado):
            contas.alterar_conta(session, admins[0].usuario_id, 0, perfil="ANALISTA")
        with pytest.raises(contas.AcessoRecusado):
            contas.alterar_conta(session, admins[0].usuario_id, 0, ativo=False)
        for a in admins[1:]:
            contas.alterar_conta(session, a.usuario_id, 0, ativo=True)

def test_senhas():
    from validacao.conferencia import acesso as contas

    guardado = contas.gerar_hash("Abc-123-def-456")
    assert guardado.startswith("scrypt$") and "Abc-123" not in guardado
    assert contas.conferir_senha("Abc-123-def-456", guardado) and not contas.conferir_senha("Abc-123-def-457", guardado)
    assert contas.gerar_hash("Abc-123-def-456") != guardado  # sal aleatório
    assert contas.problema_da_senha("curta1!")
    assert contas.problema_da_senha("somenteminusculasaqui")
    assert contas.problema_da_senha("Maria-Souza-2026", nome="Maria Souza")
    assert contas.problema_da_senha("Xq7!mR2#vL9pK4wz") is None
    gerada = contas.gerar_senha()
    assert len(gerada) == 20 and contas.problema_da_senha(gerada) is None


# ------------------------------------------------------- bases de layout fixo

def _xlsx(abas: dict) -> bytes:
    from openpyxl import Workbook

    livro = Workbook()
    livro.remove(livro.active)
    for nome, linhas in abas.items():
        aba = livro.create_sheet(nome)
        for linha in linhas:
            aba.append(list(linha))
    saida = io.BytesIO()
    livro.save(saida)
    return saida.getvalue()


def _planilhas_fixas(iss_a=1_000_000.0):
    dca = _xlsx({
        "SICONFI": [("ANO", "COD_IBGE", "ENTE", "ESFERA", "ICMS", "COTA_EST", "ISS", "COTA_MUN", "COMPEN_FIN", "COTA_COMPEN_FIN"),
                    (2023, 29, "Governo do Estado da Bahia", "Estado", 50_000_000.0, 12_500_000.0, None, None, 0, 0),
                    (2023, 9900101, "Fictício A", "Município", None, None, iss_a, 2_000_000.0, 10.0, 0)],
        "SICONFI_BRUTO": [("ANO", "COD_IBGE", "ENTE", "ESFERA", "ISS_BRUTO", "ISS_DEDUCOES_OUTRAS"),
                          (2023, 9900101, "Fictício A", "Município", iss_a + 5_000.0, 5_000.0)],
    })
    siops = _xlsx({
        "Municipios": [("UF", "COD_IBGE", "ANO", "BASE", "ENTE", "ISS", "Cota-Parte", "Comp. Fin"),
                       ("XX", 9900101, 2023, "SIOPS", "Fictício A", iss_a, 2_000_000.0, 0)],
        "Estados": [("UF", "COD_IBGE", "ANO", "BASE", "ENTE", "ICMS", "Cota-Parte", "Comp. Fin"),
                    ("BA", 29, 2023, "SIOPS", "BA", 50_000_000.0, 12_500_000.0, 0)],
    })
    siope = _xlsx({
        "Municipios": [("UF", "COD_IBGE", "ANO", "BASE", "Cota-Parte", "ISS", "ISS Principal", "ISS Multas e Juros Mora",
                        "ISS DA - Principal", "ISS DA - Multas e Juros", "ISS FECOP", "Compensacoes"),
                       ("XX", "9900101", 2023, "SIOPE", 2_000_000.0, iss_a, iss_a - 1_000.0, 1_000.0, None, None, None, None),
                       ("XX", "999999", 2023, "SIOPE", 1.0, 1.0, None, None, None, None, None, None)],
        "Estados": [("UF", "COD_IBGE", "ANO", "BASE", "ICMS", "ICMS Principal", "ICMS Multas e Juros Mora", "ICMS DA - Principal",
                     "ICMS DA - Multas e Juros", "ICMS FECOP", "Compensacoes"),
                    ("BA", "29", 2023, "SIOPE", 50_000_000.0, 49_000_000.0, 1_000_000.0, None, None, None, None)],
    })
    cabecalho_tce = ("UF", "Tribunal", "Cód. IBGE", "Município", "Exercício", "Principal", "Multas e juros", "Dívida ativa",
                     "Multas e juros da dívida ativa", "Outros itens / total sem abertura", "Total publicado", "Dedução FUNDEB",
                     "Outras deduções", "ISS — valor de referência", "Natureza do valor ISS", "Bruto", "Dedução FUNDEB",
                     "Outras deduções", "Líquido", "Valor único (sem classificação)", "Cota-parte — valor de referência",
                     "Natureza do valor cota-parte", "Observações")
    tce = _xlsx({
        "Leia-me": [("Arrecadação municipal",), ("Exercícios",), ("SEFAZ · Dados extraídos em 28/10/2026 pelo script",)],
        "ISS": [("t",), ("s",), (None,), ("UF", "Tribunal", "Cód. IBGE", "Município", "Situação"),
                ("XX", "TCE-XX", 9900101, "Fictício A", "Completo")],
        "Base detalhada": [("t",), ("s",), (None,), ("Identificação",), cabecalho_tce,
                           ("XX", "TCE-XX", 9900101, "Fictício A", 2023, iss_a, None, None, None, None, iss_a, None, None,
                            iss_a, "Bruto publicado", 2_500_000.0, -500_000.0, None, 2_000_000.0, None, 2_500_000.0,
                            "Bruto publicado", None)],
    })
    return {"dca.xlsx": dca, "siops.xlsx": siops, "siope.xlsx": siope, "tce.xlsx": tce}


def test_bases_de_layout_fixo(tmp_path):
    from validacao.conferencia import bases_fixas

    destino = tmp_path / "bases.sqlite"
    construir(destino)
    engine = criar_engine(destino)
    planilhas = _planilhas_fixas()
    ordenados = bases_fixas.ordenar(list(planilhas.items())[::-1])
    assert [fonte for fonte, _n, _c in ordenados] == ["SICONFI", "SIOPS", "SIOPE", "TCE"]  # DCA primeiro
    with pytest.raises(bases_fixas.LayoutDesconhecido):
        bases_fixas.identificar("outra.xlsx", _xlsx({"Planilha1": [("a", "b")]}))
    resultados = {}
    for fonte, nome, conteudo in ordenados:
        with Session(engine) as session:
            resultados[fonte] = bases_fixas.importar_arquivo(session, nome, conteudo, "teste")
    assert all(r["situacao"] == "IMPORTADO" and r["novos"] > 0 for r in resultados.values())
    assert resultados["SIOPE"]["ocorrencias"] == 1  # código 999999 sem ente: vai para o relatório, não aborta
    with Session(engine) as session:
        def vigente(declaracao, campo, ente=9900101):
            return session.scalar(select(cf.Registro).where(
                cf.Registro.declaracao_id == declaracao, cf.Registro.campo == campo, cf.Registro.id_ente == ente,
                cf.Registro.exercicio == 2023, cf.Registro.vigente.is_(True)))

        iss = vigente("SICONFI_DCA", "ISS_TOTAL")
        assert (iss.valor_centavos, iss.componente_id, iss.parcela, iss.conceito) == (100_000_000, "ISS_MUN", "TOTAL", "LIQUIDO")
        assert json.loads(iss.procedencia)["aba"] == "SICONFI"
        assert vigente("SIOPS", "ISS_TOTAL").valor_centavos == 100_000_000
        # (os códigos fictícios repetem os 6 primeiros dígitos; a conversão de 6 para 7 dígitos foi conferida
        #  com as planilhas reais: reimportá-las sobre a carga da versão 1 dá 0 novos e 0 versões)
        assert vigente("SIOPE", "ISS_PRINCIPAL").parcela == "PRINCIPAL"
        assert vigente("SIOPS", "ICMS_TOTAL", 29).componente_id == "ICMS_UF"  # estado criado pela DCA
        tce = vigente("TCE", "COTA_RECEBIDA_TOTAL")
        assert tce.conceito == "BRUTO" and json.loads(tce.procedencia)["natureza_informada"] == "Bruto publicado"
        assert vigente("SICONFI_DCA", "COMPENSACAO") is None  # fora do art. 115
        lote_tce = session.get(cf.Lote, resultados["TCE"]["lote"])
        assert lote_tce.data_extracao == datetime(2026, 10, 28)
    # mesmo arquivo: reconhecido; valor alterado: só a diferença vira nova versão
    with Session(engine) as session:
        assert bases_fixas.importar_arquivo(session, "dca.xlsx", planilhas["dca.xlsx"], "teste")["situacao"] == "JA_IMPORTADO"
    alterada = _planilhas_fixas(iss_a=1_100_000.0)["dca.xlsx"]
    with Session(engine) as session:
        r = bases_fixas.importar_arquivo(session, "dca-retificada.xlsx", alterada, "teste")
    assert (r["novos"], r["novas_versoes"]) == (0, 2) and r["repetidos"] > 0  # ISS_TOTAL e ISS_BRUTO mudaram
    with Session(engine) as session:
        novo = session.scalar(select(cf.Registro).where(
            cf.Registro.declaracao_id == "SICONFI_DCA", cf.Registro.campo == "ISS_TOTAL", cf.Registro.id_ente == 9900101,
            cf.Registro.exercicio == 2023, cf.Registro.vigente.is_(True)))
        assert novo.valor_centavos == 110_000_000  # a demonstração já tinha uma versão anterior
        anterior = session.get(cf.Registro, novo.substitui_id)
        assert anterior.valor_centavos == 100_000_000 and not anterior.vigente


def test_bases_de_layout_fixo_pela_tela(tmp_path):
    destino = tmp_path / "tela.sqlite"
    construir(destino)
    planilhas = _planilhas_fixas()
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        pagina = cliente.get("/importar").text
        assert "DCA, SIOPS, SIOPE e Tribunais de Contas" in pagina
        recusa = cliente.post("/importar/bases", files=[("arquivos", ("x.xlsx", _xlsx({"A": [("a",)]})))])
        assert recusa.status_code == 400 and "layout não reconhecido" in recusa.json()["erro"]
        resposta = cliente.post("/importar/bases", files=[("arquivos", (nome, conteudo)) for nome, conteudo in planilhas.items()])
        assert resposta.json()["ok"] and resposta.json()["reconhecidos"][0] == "DCA — SICONFI"
        import time

        for _ in range(240):
            estado = cliente.get("/validar/estado").json()
            if not estado["rodando"]:
                break
            time.sleep(0.5)
        assert not estado["erro"], estado
        assert estado["tipo"] == "importar_bases" and estado["execucao"]
        assert "Última importação" in cliente.get("/importar").text
    assert not list((tmp_path / "importacoes" / "pendentes").glob("*.xlsx"))  # temporários removidos


def test_pacote_siconfi_enviado_pela_tela(tmp_path):
    import time

    from validacao.conferencia.demo import gerar_pacote_ficticio
    from validacao.conferencia.pacote_siconfi import ARQUIVOS, identificar_csv

    destino = tmp_path / "envio.sqlite"
    construir(destino)
    pasta = gerar_pacote_ficticio(tmp_path / "pacote")
    for chave, nome in ARQUIVOS.items():
        assert identificar_csv((pasta / nome).read_bytes()[:4096]) == chave
    # ZIP com subpasta e nomes trocados: reconhecido pelo cabeçalho
    pacote_zip = tmp_path / "pacote.zip"
    with zipfile.ZipFile(pacote_zip, "w") as z:
        for chave, nome in ARQUIVOS.items():
            z.write(pasta / nome, f"extracao/{chave}-copia.csv")
    with TestClient(criar_app(destino)) as cliente:
        _entrar(cliente, destino, "Analista", "ANALISTA", "/")
        so_rreo = cliente.post("/importar/siconfi/arquivos",
                               files=[("arquivos", ("r.csv", (pasta / ARQUIVOS["rreo"]).read_bytes()))])
        assert so_rreo.status_code == 400 and "obrigatório" in so_rreo.json()["erro"]
        estranho = cliente.post("/importar/siconfi/arquivos", files=[
            ("arquivos", ("b.csv", (pasta / ARQUIVOS["base"]).read_bytes())), ("arquivos", ("x.csv", b"a;b\n1;2\n"))])
        assert estranho.status_code == 400 and "x.csv" in estranho.json()["erro"]
        resposta = cliente.post("/importar/siconfi/arquivos", files=[("arquivos", ("pacote.zip", pacote_zip.read_bytes()))])
        assert resposta.json()["ok"] and len(resposta.json()["reconhecidos"]) == 5
        for _ in range(240):
            estado = cliente.get("/validar/estado").json()
            if not estado["rodando"]:
                break
            time.sleep(0.5)
        assert not estado["erro"] and estado["tipo"] == "importar_siconfi" and estado["execucao"], estado
        assert sorted(p.name for p in (tmp_path / "pacote_siconfi").glob("*.csv")) == sorted(ARQUIVOS.values())
        pagina = cliente.get("/importar").text
        assert "Último pacote enviado pela tela" in pagina and "5 de 5 encontrados" in pagina
    with Session(criar_engine(destino)) as session:
        assert session.scalar(select(func.count()).select_from(cf.Registro).where(cf.Registro.declaracao_id == "SICONFI_RREO")) > 0


def test_pacote_siconfi_em_banco_vazio(tmp_path):
    """Banco novo (ex.: volume vazio na Railway): o pacote não pode falhar por falta do cadastro de fontes."""
    from validacao.cadastro import garantir_ente
    from validacao.conferencia.demo import gerar_pacote_ficticio
    from validacao.conferencia.migracao import preparar
    from validacao.conferencia.pacote_siconfi import importar_pacote
    from validacao.modelos import Ente, EnteCodigoExterno

    engine = criar_engine(tmp_path / "vazio.sqlite")
    preparar(engine)
    resultado = importar_pacote(engine, gerar_pacote_ficticio(tmp_path / "pacote"), "teste", tmp_path)
    assert resultado["SICONFI_RREO"]["situacao"] == "IMPORTADO"
    with Session(engine) as session:
        conhecidos = set(session.scalars(select(Ente.id_ente)))
        assert garantir_ente(session, 29, "ESTADO", "Bahia", conhecidos)  # estado novo entra no cadastro
        assert garantir_ente(session, 2927408, "MUNICIPIO", "Salvador", conhecidos)
        assert not garantir_ente(session, 99, "ESTADO", "Inexistente", conhecidos)
        session.commit()
        assert session.get(Ente, 29).uf == "BA"
        assert session.scalar(select(func.count()).select_from(EnteCodigoExterno).where(EnteCodigoExterno.id_ente == 2927408)) == 4
