"""Base de demonstração com DADOS FICTÍCIOS para os cenários de aceitação.

Todos os entes têm código 99xxxxx e UF "XX", e todos os lotes são marcados como
fictícios. Os valores passam pelo mesmo caminho de importação, validação e
decisão usado com os dados reais.

Parâmetros de teste explícitos (iguais aos padrões da configuração inicial):
  Divergência com base auxiliar: tolerância relativa 1%, absoluta R$ 1.000,
  modo "ambas", gravidade alta acima de 20%.
  Variação atípica: crescimento máximo 3,0×; corroboração ±15%.
  Data de corte: 31/12/2026 (fictícia).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.conferencia import importacao
from validacao.conferencia.decisao import registrar
from validacao.conferencia.execucao import executar
from validacao.conferencia.migracao import definir_configuracao, garantir_conjunto, preparar
from validacao.conferencia import modelos as cf
from validacao.db import criar_engine
from validacao.modelos import Ente, Usuario

ANOS = list(range(2019, 2026))
DATA_CORTE = "2026-12-31"

ENTES = {
    9900101: "Fictício A — divergência isolada",
    9900102: "Fictício B — ausência na fonte principal",
    9900103: "Fictício C — referência zero",
    9900104: "Fictício D — conceitos distintos",
    9900105: "Fictício E — alta corroborada",
    9900106: "Fictício F — retificação após decisão",
    9900107: "Fictício G — sem alerta",
    9900108: "Fictício H — sem alerta",
}

MILHAO = 1_000_000


def _serie(base: float) -> dict[int, float]:
    return {ano: round(base * (1.06 ** (ano - 2019)), 2) for ano in ANOS}


def dados_iniciais() -> dict[str, dict[tuple[int, int], dict[str, float | None]]]:
    """(declaração) -> (ente, ano) -> {campo: valor}"""
    dca, siops, siope, tce = {}, {}, {}, {}
    bases = {9900101: 8.5, 9900102: 4.2, 9900103: 2.6, 9900104: 6.8, 9900105: 1.7, 9900106: 5.9, 9900107: 3.3, 9900108: 12.4}
    for ente, base in bases.items():
        for ano, valor in _serie(base * MILHAO).items():
            dca[(ente, ano)] = {"ISS_TOTAL": valor}
            siops[(ente, ano)] = {"ISS_TOTAL": valor}
            siope[(ente, ano)] = {"ISS_TOTAL": valor}
    # 1. Divergência isolada: referência 10 mi; SIOPS 10,02 mi (0,2%); SIOPE 16 mi (60%)
    dca[(9900101, 2023)] = {"ISS_TOTAL": 10_000_000.00}
    siops[(9900101, 2023)] = {"ISS_TOTAL": 10_020_000.00}
    siope[(9900101, 2023)] = {"ISS_TOTAL": 16_000_000.00}
    # 2. Ausência na fonte principal: linha existe, célula vazia; auxiliares informam
    dca[(9900102, 2023)] = {"ISS_TOTAL": None}
    siops[(9900102, 2023)] = {"ISS_TOTAL": 5_100_000.00}
    siope[(9900102, 2023)] = {"ISS_TOTAL": 5_100_000.00}
    # 3. Referência zero com outra fonte positiva
    dca[(9900103, 2023)] = {"ISS_TOTAL": 0.0}
    siops[(9900103, 2023)] = {"ISS_TOTAL": 3_000_000.00}
    siope[(9900103, 2023)] = {"ISS_TOTAL": 3_000_000.00}
    # 4. Conceitos distintos: TCE informa bruto; a DCA, líquido, sem deduções identificadas.
    #    SIOPS e SIOPE não informam este ente, para que a única comparação possível seja a incompatível.
    for ano in ANOS:
        siops.pop((9900104, ano), None)
        siope.pop((9900104, ano), None)
        liquido = dca[(9900104, ano)]["ISS_TOTAL"]
        tce[(9900104, ano)] = {"ISS_TOTAL": round(liquido * 1.05, 2)}
    # 5. Alta corroborada: crescimento de 3,25× em 2023 em todas as fontes
    for ano, valor in {2022: 2_000_000.00, 2023: 6_500_000.00, 2024: 6_800_000.00, 2025: 7_100_000.00}.items():
        dca[(9900105, ano)] = {"ISS_TOTAL": valor}
        siops[(9900105, ano)] = {"ISS_TOTAL": valor}
        siope[(9900105, ano)] = {"ISS_TOTAL": round(valor * 1.001, 2)}
    # 6. Retificação: primeira versão igual às auxiliares (sem alerta)
    dca[(9900106, 2023)] = {"ISS_TOTAL": 7_000_000.00}
    siops[(9900106, 2023)] = {"ISS_TOTAL": 7_000_000.00}
    siope[(9900106, 2023)] = {"ISS_TOTAL": 7_000_000.00}
    return {"SICONFI_DCA": dca, "SIOPS": siops, "SIOPE": siope, "TCE": tce}


def csv_de(dados: dict[tuple[int, int], dict[str, float | None]]) -> bytes:
    linhas = ["COD_IBGE;ANO;ISS"]
    for (ente, ano), campos in sorted(dados.items()):
        valor = campos["ISS_TOTAL"]
        texto = "" if valor is None else f"{valor:.2f}".replace(".", ",")
        linhas.append(f"{ente};{ano};{texto}")
    return ("\n".join(linhas) + "\n").encode("utf-8")


CONCEITO_DEMO = {"SICONFI_DCA": "LIQUIDO", "SIOPS": "LIQUIDO", "SIOPE": "LIQUIDO", "TCE": "BRUTO"}


def mapa_demo(declaracao: str, data: str = "2026-10-01", componente: str = "ISS_MUN", campo: str = "ISS_TOTAL") -> importacao.Mapeamento:
    return importacao.Mapeamento(
        declaracao=declaracao, componente=componente, aba="CSV", linha_cabecalho=1, coluna_ente="COD_IBGE",
        coluna_exercicio="ANO", exercicio_fixo=None,
        campos=[importacao.CampoValor(coluna="ISS", campo=campo, parcela="TOTAL", conceito=CONCEITO_DEMO[declaracao])],
        data_extracao=datetime.fromisoformat(data), observacao="Dados fictícios de demonstração.", ficticio=True,
    )


def importar_demo(session: Session, declaracao: str, conteudo: bytes, nome: str, data: str = "2026-10-01",
                  componente: str = "ISS_MUN", campo: str = "ISS_TOTAL") -> dict:
    planilha = importacao.ler(nome, conteudo)
    return importacao.importar(session, planilha, mapa_demo(declaracao, data, componente, campo), "demonstração")


def dados_cota() -> dict[str, dict[tuple[int, int], dict[str, float | None]]]:
    """Cota-parte do ICMS fictícia, coincidente entre DCA, SIOPS e SIOPE (sem alertas)."""
    base = dados_iniciais()["SIOPS"]
    cota = {}
    for chave in [(ente, ano) for ente in ENTES for ano in ANOS]:
        referencia = dados_iniciais()["SICONFI_DCA"].get(chave, {}).get("ISS_TOTAL") or base.get(chave, {}).get("ISS_TOTAL") or 1_000_000.0
        cota[chave] = {"ISS_TOTAL": round(referencia * 1.8, 2)}
    return {"SICONFI_DCA": cota, "SIOPS": cota, "SIOPE": cota}


def gerar_pacote_ficticio(pasta: Path) -> Path:
    """Gera os cinco arquivos do pacote SICONFI no layout definido, com dados fictícios.

    RREO igual à DCA, exceto: ente A em 2022 (RREO 8% acima: divergência entre declarações
    do SICONFI) e ente B em 2023 (RREO sem dado). MSC igual ao RREO, exceto ente C em 2023
    (consulta da MSC com erro).
    """
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    dca = dados_iniciais()["SICONFI_DCA"]
    cota = dados_cota()["SICONFI_DCA"]
    base = ["cod_ibge;ente;uf;esfera;ano;componente;valor_rreo;valor_msc;diferenca_msc_menos_rreo;diferenca_pct_rreo;tolerancia_aplicada;situacao"]
    rreo = ["cod_ibge;ente;uf;esfera;ano;componente;valor;cod_conta;conta;coluna;anexo;demonstrativo;tipo_demonstrativo;rotulo;instituicao;populacao"]
    msc = ["cod_ibge;ente;uf;esfera;ano;componente;natureza_receita;conta_contabil;natureza_conta;valor;multiplicador;valor_sinalizado;poder_orgao;fonte_recursos;complemento_fonte;tipo_matriz;mes_referencia;tipo_valor;entrada_msc"]
    status = ["fonte;cod_ibge;ente;uf;esfera;ano;ok;linhas_retidas;linhas_lidas;paginas;detalhe;erro;atualizado_em"]
    cobertura = ["cod_ibge;ente;uf;esfera;ano;componentes_esperados;componentes_rreo;componentes_msc;consultas_rreo_ok;consultas_msc_ok;validacoes_ok;divergencias;ausencias;situacao"]
    contas = {"ISS": "ISSLiquidoExcetoTransferenciasEFUNDEB", "COTA_PARTE_ICMS": "RREO3CotaParteDoICMS"}
    for ente, nome in ENTES.items():
        for ano in ANOS:
            ok_msc = not (ente == 9900103 and ano == 2023)
            divergencias = 0
            ausencias = 0
            for comp, fonte in (("ISS", dca), ("COTA_PARTE_ICMS", cota)):
                valor = fonte.get((ente, ano), {}).get("ISS_TOTAL")
                v_rreo = valor
                if ente == 9900101 and ano == 2022 and comp == "ISS":
                    v_rreo = round(valor * 1.08, 2)
                if ente == 9900102 and ano == 2023 and comp == "ISS":
                    v_rreo = None
                v_msc = v_rreo if ok_msc else None
                if v_rreo is None and v_msc is None:
                    situacao = "SEM_RREO_E_SEM_MSC"
                elif v_rreo is None:
                    situacao = "SEM_RREO"
                elif v_msc is None:
                    situacao = "SEM_MSC"
                else:
                    situacao = "OK"
                ausencias += situacao != "OK"
                fmt = lambda x: "" if x is None else f"{x:.2f}"
                dif = "" if v_rreo is None or v_msc is None else f"{v_msc - v_rreo:.2f}"
                base.append(f"{ente};{nome};XX;M;{ano};{comp};{fmt(v_rreo)};{fmt(v_msc)};{dif};{'0' if dif else ''};{fmt((v_rreo or 0) * 0.01)};{situacao}")
                if v_rreo is not None:
                    rreo.append(f"{ente};{nome};XX;M;{ano};{comp};{v_rreo:.2f};{contas[comp]};{comp} (fictício);TOTAL (ÚLTIMOS 12 MESES);RREO-Anexo 03;RREO;RREO;6º bimestre;{nome};10000")
                if v_msc is not None:
                    metade = round(v_msc / 2, 2)
                    for parte in (metade, round(v_msc - metade, 2)):
                        msc.append(f"{ente};{nome};XX;M;{ano};{comp};11145110;621200000;C;{parte:.2f};1;{parte:.2f};10131;1500;0;MSCE;12;period_change;fictícia")
            for fonte, ok in (("RREO", True), ("MSC", ok_msc)):
                status.append(f"{fonte};{ente};{nome};XX;M;{ano};{'True' if ok else 'False'};2;40;1;;{'' if ok else 'HTTP 500 (fictício)'};2026-10-30T09:00:00")
            situacao_cob = "INCOMPLETO" if ausencias else ("DIVERGENCIA" if divergencias else "OK")
            cobertura.append(f"{ente};{nome};XX;M;{ano};ISS|COTA_PARTE_ICMS;ISS|COTA_PARTE_ICMS;{'ISS|COTA_PARTE_ICMS' if ok_msc else ''};1/1;{'1/1' if ok_msc else '0/1'};{2 - ausencias};{divergencias};{ausencias};{situacao_cob}")
    for nome, linhas in (("base_validacao_coeficiente.csv", base), ("rreo_coeficiente.csv", rreo), ("msc_coeficiente.csv", msc),
                         ("status.csv", status), ("cobertura.csv", cobertura)):
        (pasta / nome).write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return pasta


def construir(destino: Path, sobrescrever: bool = True) -> dict:
    destino = Path(destino)
    if destino.exists():
        if not sobrescrever:
            raise FileExistsError(destino)
        destino.unlink()
    engine = criar_engine(destino)
    preparar(engine, "demonstração")
    relatorio = {}
    with Session(engine) as session:
        for codigo, nome in ENTES.items():
            session.add(Ente(id_ente=codigo, esfera="MUNICIPIO", uf="XX", nome=nome, capital=False, tribunal_competente="TCE-XX"))
        analista = Usuario(nome="Analista de demonstração", perfil="ANALISTA")
        session.add(analista)
        definir_configuracao(session, "demonstracao", True)
        definir_configuracao(session, "migrado_em", "demonstração")
        conjunto = garantir_conjunto(session, "demonstração")
        config = json.loads(conjunto.parametros)
        config["data_corte"] = DATA_CORTE
        conjunto.parametros = json.dumps(config, ensure_ascii=False)
        conjunto.observacao = "Configuração da demonstração: parâmetros iniciais e data de corte 31/12/2026 (fictícia)."
        session.commit()
        dados = dados_iniciais()
        for declaracao, valores in dados.items():
            relatorio[f"lote_{declaracao}"] = importar_demo(session, declaracao, csv_de(valores), f"FICTICIO_{declaracao}.csv")
        for declaracao, valores in dados_cota().items():
            importar_demo(session, declaracao, csv_de(valores), f"FICTICIO_COTA_{declaracao}.csv",
                          componente="COTA_ICMS_MUN", campo="COTA_RECEBIDA_TOTAL")
        session.commit()
        analista_id = analista.id
    from validacao.conferencia.pacote_siconfi import importar_pacote

    pasta_pacote = gerar_pacote_ficticio(destino.parent / "pacote_siconfi_ficticio")
    relatorio["pacote"] = importar_pacote(engine, pasta_pacote, "demonstração", destino.parent / f"{destino.stem}_detalhes")
    with Session(engine) as session:
        for lote in session.scalars(select(cf.Lote).where(cf.Lote.ficticio.is_(False))):
            lote.ficticio = True
        session.commit()
    executar(engine, "demonstração")

    with Session(engine) as session:
        from validacao.conferencia.consultas import caso_por_chave

        caso_f = caso_por_chave(session, 9900106, 2023, "ISS_MUN")
        registrar(session, caso_f, "MANTER_PRINCIPAL", "Analista de demonstração", analista_id,
                  justificativa="Valor da DCA conferido com SIOPS e SIOPE (cenário fictício).")
        caso_a = caso_por_chave(session, 9900101, 2023, "ISS_MUN")
        registrar(session, caso_a, "SOLICITAR_ESCLARECIMENTO", "Analista de demonstração", analista_id,
                  justificativa="SIOPE 60% acima da DCA e do SIOPS. Pedido de esclarecimento ao ente sobre o valor informado ao FNDE (cenário fictício).",
                  evidencias="Ofício fictício nº 001/2026")
        session.commit()

    # Retificação da DCA do ente F depois da decisão
    retificada = dados_iniciais()["SICONFI_DCA"]
    retificada[(9900106, 2023)] = {"ISS_TOTAL": 7_700_000.00}
    with Session(engine) as session:
        relatorio["retificacao"] = importar_demo(session, "SICONFI_DCA", csv_de(retificada), "FICTICIO_SICONFI_DCA_retificada.csv", "2026-10-20")
        # Reimportação idêntica do primeiro arquivo do SIOPS
        relatorio["reimportacao"] = importar_demo(session, "SIOPS", csv_de(dados_iniciais()["SIOPS"]), "FICTICIO_SIOPS.csv")
        session.commit()
    relatorio["execucao"] = executar(engine, "demonstração")
    return relatorio
