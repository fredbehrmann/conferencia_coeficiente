"""Planilha completa da central de inconsistências (todos os entes, 2019 a 2025) com abas explicativas.

Abas:
  Leia-me       o que é a planilha, de onde vêm os dados, como ler cada coluna
  Central       uma linha por ente × exercício × componente, com todas as fontes, ausências,
                alertas, nível de confiança (Análise), proposta e decisão
  Resumo        contagens por componente (fórmulas sobre a aba Central)
  Alertas       o que cada regra verifica, o que retorna e quantos casos atinge
  Análise       domínio da coluna Análise (níveis 0 a 5) e quantos casos em cada um
  Domínios      significado dos demais códigos (situação da fonte, comparabilidade, diagnóstico...)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import COMPONENTES, DECLARACOES, ROTULO_CURTO
from validacao.conferencia.confianca import NIVEIS
from validacao.conferencia.consultas import Filtros, consulta_base
from validacao.conferencia.decisao import ACOES, SITUACOES
from validacao.conferencia.execucao import expandir_valores, ultima_execucao
from validacao.conferencia.exportacao import DIAGNOSTICOS
from validacao.conferencia.regras import GRAVIDADES, GRAVIDADE_POR_NOME, REGRAS
from validacao.modelos import Ente

ANOS = list(range(2019, 2026))
FONTES = ["SICONFI_DCA", "SICONFI_RREO", "SICONFI_MSC", "SIOPE", "SIOPS", "TCE"]
SIGLA = {d["id"]: d["sigla"] for d in DECLARACOES}
NOME_FONTE = {d["id"]: d["nome"] for d in DECLARACOES}
NOME_COMP = {c["id"]: c["nome"] for c in COMPONENTES}
ORDEM_COMP = {c["id"]: c["ordem"] for c in COMPONENTES}
ESFERA = {"MUNICIPIO": "Município", "ESTADO": "Estado", "DF": "Distrito Federal"}

SITUACAO_FONTE = {
    "VALOR": "Valor",
    "ZERO": "Zero declarado",
    "AUSENTE": "Registro sem valor",
    "INVALIDO": "Formato inválido",
    "NAO_APLICAVEL": "Não aplicável",
    "SEM_REGISTRO": "Sem registro",
    "CONSULTA_ERRO": "Consulta com erro",
}
EXPLICA_SITUACAO_FONTE = {
    "Valor": "A fonte informa um valor para o ente, exercício e componente.",
    "Zero declarado": "A fonte informa zero. Zero é um valor declarado, não ausência.",
    "Registro sem valor": "A linha existe na fonte, mas a célula está vazia (no pacote do SICONFI: SEM_RREO / SEM_MSC).",
    "Formato inválido": "O conteúdo da célula não pôde ser lido como número.",
    "Sem registro": "A fonte não tem nenhuma linha para este ente, exercício e componente (ex.: TCE não cobre o estado do ente).",
    "Consulta com erro": "RREO/MSC: a consulta ao SICONFI falhou (status.csv) e precisa ser refeita.",
    "Não aplicável": "A fonte não se aplica a este caso.",
}
COMPARABILIDADE = {
    "REFERENCIA": "Referência",
    "SIM": "Comparável",
    "RESSALVA": "Comparável com ressalva",
    "NAO": "Não comparável diretamente",
    "SEM_DADO": "Sem valor",
    "SEM_REFERENCIA": "Sem referência",
}
EXPLICA_COMPARABILIDADE = {
    "Referência": "É a fonte principal do caso (DCA; o RREO quando falta a DCA). As diferenças das demais são medidas contra ela.",
    "Comparável": "Mesma grandeza e mesmo conceito da referência: a diferença em R$ e % é direta.",
    "Comparável com ressalva": "Comparada numa parcela comum depois de ajuste de conceito (ex.: valor líquido de FUNDEB). Ver o motivo na tela do caso.",
    "Não comparável diretamente": "A fonte mede outra coisa (conceito ou abrangência) e não há parcela comum. Não entra na contagem de concordância.",
    "Sem valor": "A fonte não tem valor neste caso.",
    "Sem referência": "Não há fonte principal com valor no caso; nada a comparar.",
}
EXPLICA_DIAGNOSTICO = {
    "Com alerta": "Ao menos uma regra de qualidade disparou com gravidade baixa ou maior.",
    "Sem validação suficiente": "A referência existe, mas nenhuma fonte independente comparável a confirma.",
    "Conferido sem alerta": "A referência foi conferida com fontes independentes dentro das tolerâncias.",
    "Declaração não exigível": "O prazo de entrega da declaração ainda não venceu na data de corte.",
    "Fora do universo": "Ente inexistente no exercício ou excluído por decisão.",
}
CORES_NIVEL = {5: ("0F4A1A", "FFFFFF"), 4: ("2E7D32", "FFFFFF"), 3: ("F2C94C", "3D2E00"), 2: ("F08C00", "2B1700"),
               1: ("C62828", "FFFFFF"), 0: ("6B0F0F", "FFFFFF"), None: ("E7E6E1", "555555")}
CRITERIO_NIVEL = {
    5: "A DCA coincide com 2 ou mais fontes independentes (SIOPE, SIOPS, TCE), nenhuma independente diverge e não há alerta de repetição ou de escala sobre a própria DCA.",
    4: "A DCA coincide com 2 ou mais independentes, mas uma terceira diverge; ou seria nível 5, mas há alerta de repetição/escala sobre a DCA. O ICMS estadual fica limitado a este nível.",
    3: "A DCA coincide com 1 independente e nenhuma diverge; ou falta a DCA e o RREO é confirmado por 1 independente; ou, na cota-parte, as independentes valem DCA ÷ 0,8 (DCA líquida de FUNDEB — depende da pendência P12).",
    2: "A DCA coincide com 1 independente e outra diverge; ou só coincide com RREO/MSC (mesmo declarante); ou não há outra fonte comparável; ou falta a DCA e o RREO não tem confirmação; ou faltam DCA e RREO, mas 2+ independentes concordam entre si.",
    1: "A DCA diverge de todas as fontes, mas as independentes concordam entre si: há valor alternativo consistente (inclui DCA zerada com independentes positivas).",
    0: "Nenhum valor confiável: a DCA diverge de todas e as demais também divergem entre si; ou não há DCA nem consenso entre as demais fontes.",
    None: "Exercício não exigível ou ente fora do universo.",
}
LEITURA_NIVEL = {
    5: "Valor pode ser usado sem análise adicional.", 4: "Valor seguro; olhar a fonte divergente se houver tempo.",
    3: "Valor provável; conferir rapidamente.", 2: "Analisar antes de usar.",
    1: "Provável erro da DCA; prioridade de análise (há valor alternativo).", 0: "Prioridade máxima: não há valor confiável.",
    None: "—",
}
SAIDA_REGRA = {
    "AUS": "Indica que a fonte principal não tem valor e quais fontes têm.",
    "INS": "Quantas fontes independentes comparáveis existem e o mínimo exigido.",
    "DSI": "Valor do RREO ou da MSC, valor da DCA, diferença em R$ e % (fonte − referência).",
    "DAX": "Valor da base auxiliar (SIOPE, SIOPS, TCE), valor de referência, diferença em R$ e %; gravidade alta acima de 20%.",
    "DUP": "Campo repetido no mesmo lote, com os registros envolvidos.",
    "ZER": "Fonte que declara zero e as fontes que informam valor positivo.",
    "NEG": "Fonte, campo e valor negativo encontrado.",
    "ESC": "Razão de cerca de 10, 100 ou 1.000 vezes entre fontes ou contra a série do ente.",
    "REP": "Exercícios em que a fonte principal repete o mesmo valor ao centavo.",
    "REE": "Outros entes com o mesmo valor ao centavo no mesmo exercício.",
    "VAR": "Crescimento/queda ou pico/vale e se outra fonte corrobora o movimento.",
    "TOT": "Total declarado × soma das parcelas da mesma fonte e a diferença.",
    "RET": "Versão vigente do valor, versão anterior e data de extração.",
    "NCP": "Fonte e motivo pelo qual não é comparável (conceito ou abrangência).",
    "FLF": "Fonte que coincide com a referência menos o FUNDEB (diferença de conceito).",
    "UFC": "ICMS estadual: cota repassada pelo estado × soma das cotas dos municípios da UF.",
    "PAR": "Declaração não exigível ou período parcial.",
}

ARIAL = "Arial"
F_NORMAL = Font(name=ARIAL, size=10)
F_NEGRITO = Font(name=ARIAL, size=10, bold=True)
F_TITULO = Font(name=ARIAL, size=14, bold=True)
F_CAB = Font(name=ARIAL, size=10, bold=True, color="FFFFFF")
F_SUTIL = Font(name=ARIAL, size=9, color="666666")
FILL_CAB = PatternFill("solid", fgColor="1F3A5F")
FILL_GRUPO = {f: PatternFill("solid", fgColor=c) for f, c in zip(FONTES, ["DCE6F2", "E2EFDA", "EDE7F6", "FFF2CC", "FCE4D6", "E7E6E6"])}
FILL_AUSENTE = PatternFill("solid", fgColor="FDECEA")
FILL_ZERO = PatternFill("solid", fgColor="FEF3E2")
FILL_REF = PatternFill("solid", fgColor="E8F0FC")
FINA = Side(style="thin", color="BFBFBF")
QUEBRA = Alignment(wrap_text=True, vertical="top")
TOPO = Alignment(vertical="top")
FMT_REAIS = '#,##0.00;[Red]-#,##0.00'
FMT_PCT = '0.00%;[Red]-0.00%'


ESTILO_NIVEL = {
    n: {"preenchimento": PatternFill("solid", fgColor=fundo), "fonte": Font(name=ARIAL, size=10, bold=True, color=letra)}
    for n, (fundo, letra) in CORES_NIVEL.items()
}


def _reais(centavos):
    return None if centavos is None else round(centavos / 100, 2)


def _celula(ws, valor, fonte=F_NORMAL, preenchimento=None, formato=None, alinhamento=None):
    c = WriteOnlyCell(ws, value=valor)
    c.font = fonte
    if preenchimento is not None:
        c.fill = preenchimento
    if formato:
        c.number_format = formato
    if alinhamento is not None:
        c.alignment = alinhamento
    return c


def _colunas_central() -> list[tuple[str, str, int, str]]:
    """(chave, cabeçalho, largura, descrição) de cada coluna da aba Central."""
    cols = [
        ("ibge", "Código IBGE", 11, "Código do ente no IBGE (2 dígitos para estados e DF, 7 para municípios)."),
        ("ente", "Ente", 30, "Nome do ente."),
        ("uf", "UF", 5, "Unidade da federação."),
        ("esfera", "Esfera", 11, "Município, Estado ou Distrito Federal."),
        ("ano", "Exercício", 9, "Exercício financeiro (2019 a 2025)."),
        ("comp", "Componente", 14, "Componente do art. 115 da LC 227/2026 (ver aba Domínios)."),
        ("exig", "Exigibilidade", 13, "Se a declaração já era exigível na data de corte."),
        ("principal_fonte", "Fonte principal", 10, "Fonte usada como referência: DCA; RREO quando falta a DCA; vazio quando nenhuma das duas tem valor."),
        ("principal_valor", "Valor principal (R$)", 18, "Valor de referência do caso, no conceito de comparação (líquido de outras deduções)."),
        ("principal_estado", "Situação da principal", 16, "Situação do valor da fonte principal (ver aba Domínios)."),
    ]
    for f in FONTES:
        s = SIGLA[f]
        cols += [
            (f"{f}:valor", f"{s} — valor (R$)", 17, f"Valor original informado pelo {s} (total declarado)."),
            (f"{f}:sit", f"{s} — situação", 15, f"Valor, zero, registro sem valor, sem registro ou consulta com erro (ausências destacadas em vermelho claro)."),
            (f"{f}:pct", f"{s} — dif. % vs ref.", 11, f"(valor do {s} − referência) ÷ |referência|, na parcela comparada. Vazio quando não comparável ou sem referência."),
            (f"{f}:comp", f"{s} — comparabilidade", 16, "Referência, comparável, comparável com ressalva ou não comparável (ver aba Domínios)."),
        ]
    cols += [
        ("n_fontes", "Fontes com valor (de 6)", 9, "Quantas das 6 fontes têm valor (inclui zero declarado)."),
        ("ausentes", "Fontes ausentes", 22, "Fontes sem valor neste caso, com o motivo entre parênteses."),
        ("concordam", "Fontes que coincidem com a ref.", 22, "Fontes comparáveis dentro da tolerância (1% ou R$ 1.000) em relação à referência."),
        ("divergem", "Fontes que divergem da ref.", 22, "Fontes comparáveis fora da tolerância."),
        ("maior_abs", "Maior diferença (R$)", 16, "Maior diferença absoluta entre um par comparável."),
        ("maior_pct", "Maior diferença (%)", 11, "Percentual da maior diferença em relação à referência."),
        ("maior_par", "Par da maior diferença", 14, "Fonte × referência da maior diferença."),
        ("alertas_cod", "Alertas (códigos)", 16, "Códigos das regras que dispararam (ver aba Alertas). Use filtro de texto 'contém'."),
        ("alertas", "Alertas (nome e gravidade)", 40, "Nome da regra e a maior gravidade dela no caso."),
        ("gravidade", "Gravidade máxima", 11, "Maior gravidade entre os alertas do caso."),
        ("detalhe", "Detalhe dos alertas", 70, "Texto que cada regra gerou para o caso (valores e diferenças)."),
        ("diag", "Diagnóstico", 18, "Resultado das regras (ver aba Domínios)."),
        ("situacao", "Situação da análise", 18, "Andamento da análise humana (ver aba Domínios)."),
        ("nivel", "Análise (nível)", 8, "Nível de confiança no valor principal, de 0 (sem confiança) a 5 (muito alta). Ver aba Análise."),
        ("nivel_nome", "Análise", 14, "Nome do nível de confiança."),
        ("nivel_motivo", "Análise — motivo", 60, "Por que o caso recebeu este nível."),
        ("prop_valor", "Valor proposto (R$)", 17, "Valor proposto automaticamente pela regra de precedência (não é decisão)."),
        ("prop_fonte", "Fonte proposta", 10, "Fonte do valor proposto."),
        ("prop_motivo", "Motivo da proposta", 40, "Por que esse valor foi proposto (ou por que não há proposta)."),
        ("dec_acao", "Decisão do analista", 22, "Conclusão registrada pelo analista, se houver."),
        ("dec_valor", "Valor adotado (R$)", 17, "Valor adotado na decisão vigente."),
        ("dec_fonte", "Fonte adotada", 10, "Fonte do valor adotado (ou 'correção')."),
        ("dec_quem", "Decidido por / em", 22, "Responsável e data da decisão vigente."),
        ("caso", "Nº do caso", 9, "Identificador do caso na validação (para abrir na aplicação: /caso/<nº>)."),
    ]
    return cols


def gerar(engine, destino: Path, progresso=None) -> dict:
    aviso = progresso or (lambda _m: None)
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with Session(engine) as session:
        execucao = ultima_execucao(session)
        if execucao is None:
            raise RuntimeError("Nenhuma validação concluída.")
        aviso("Lendo consultas e alertas...")
        consultas_falhas = {
            (c.fonte.upper(), c.id_ente, c.ano)
            for c in session.scalars(select(cf.Consulta).where(cf.Consulta.ok.is_(False)))
        }
        mensagens: dict[int, list[str]] = defaultdict(list)
        for caso_id, regra, gravidade, mensagem in session.execute(
            select(cf.Alerta.caso_id, cf.Alerta.regra, cf.Alerta.gravidade, cf.Alerta.mensagem)
            .where(cf.Alerta.execucao_id == execucao.id).order_by(cf.Alerta.caso_id, cf.Alerta.gravidade.desc())
        ):
            mensagens[caso_id].append(f"[{regra} · {GRAVIDADES.get(gravidade, gravidade)}] {mensagem}")
        esferas = {e.id_ente: e.esfera for e in session.scalars(select(Ente))}
        conjunto = session.get(cf.ConjuntoRegras, execucao.conjunto_id)
        parametros = json.loads(conjunto.parametros) if conjunto else {}

        wb = Workbook(write_only=True)
        from openpyxl.utils.indexed_list import IndexedList

        wb._fonts = IndexedList([F_NORMAL])  # fonte padrão do arquivo: Arial 10 (células sem estilo próprio)
        colunas = _colunas_central()
        indice = {chave: i for i, (chave, *_r) in enumerate(colunas)}

        # ------------------------------------------------------------ Leia-me (escrita depois, mas criada primeiro)
        ws_leia = wb.create_sheet("Leia-me")
        ws_resumo = wb.create_sheet("Resumo")
        ws = wb.create_sheet("Central")
        ws.freeze_panes = "F2"
        for i, (_c, _cab, largura, _d) in enumerate(colunas, start=1):
            ws.column_dimensions[get_column_letter(i)].width = largura
        cab = []
        for chave, texto, _l, descricao in colunas:
            fonte_col = chave.split(":")[0] if ":" in chave else None
            c = WriteOnlyCell(ws, value=texto)
            c.font = F_CAB
            c.fill = FILL_CAB if fonte_col is None else PatternFill("solid", fgColor="2F5597")
            c.alignment = Alignment(wrap_text=True, vertical="center")
            c.comment = Comment(descricao, "Conferência")
            cab.append(c)
        ws.row_dimensions[1].height = 42
        ws.append(cab)

        aviso("Escrevendo a aba Central...")
        consulta, _sit = consulta_base(execucao.id, Filtros(somente_alerta=False))
        consulta = consulta.where(cf.Caso.exercicio.in_(ANOS), cf.Caso.componente_id != "FUNDOS_UF")
        ordem_esfera = {"ESTADO": 0, "DF": 1, "MUNICIPIO": 2}
        linhas = sorted(
            session.execute(consulta).all(),
            key=lambda t: (t[0].uf, ordem_esfera.get(t[0].esfera, 3), t[0].nome_ente, t[0].id_ente,
                           ORDEM_COMP.get(t[0].componente_id, 9), t[0].exercicio),
        )
        total = 0
        for caso, decisao, situacao in linhas:
            if caso.diagnostico == "EXCLUIDO":
                continue
            total += 1
            v = expandir_valores(caso.valores)
            linha = [None] * len(colunas)

            def poe(chave, valor, **estilo):
                linha[indice[chave]] = (valor, estilo)

            poe("ibge", caso.id_ente)
            poe("ente", caso.nome_ente)
            poe("uf", caso.uf)
            poe("esfera", ESFERA.get(esferas.get(caso.id_ente, caso.esfera), caso.esfera))
            poe("ano", caso.exercicio)
            poe("comp", ROTULO_CURTO.get(caso.componente_id, caso.componente_id))
            poe("exig", "Exigível" if caso.situacao_exercicio == "EXIGIVEL" else "Não exigível")
            poe("principal_fonte", SIGLA.get(caso.declaracao_principal, "—") if caso.declaracao_principal else "—")
            poe("principal_valor", _reais(caso.valor_principal), formato=FMT_REAIS)
            poe("principal_estado", SITUACAO_FONTE.get(caso.estado_principal, caso.estado_principal))
            com_valor, ausentes, concordam, divergem = 0, [], [], []
            for f in FONTES:
                item = v.get(f)
                s = SIGLA[f]
                curto = "RREO" if f == "SICONFI_RREO" else ("MSC" if f == "SICONFI_MSC" else None)
                if item is None:
                    estado = "CONSULTA_ERRO" if curto and (curto, caso.id_ente, caso.exercicio) in consultas_falhas else "SEM_REGISTRO"
                else:
                    estado = item.get("estado") or "SEM_REGISTRO"
                    if estado == "AUSENTE" and curto and (curto, caso.id_ente, caso.exercicio) in consultas_falhas:
                        estado = "CONSULTA_ERRO"
                tem = estado in ("VALOR", "ZERO")
                com_valor += tem
                if not tem:
                    ausentes.append(f"{s} ({SITUACAO_FONTE.get(estado, estado).lower()})")
                fill = FILL_AUSENTE if not tem else (FILL_ZERO if estado == "ZERO" else None)
                comp = (item or {}).get("comparabilidade")
                if f == caso.declaracao_principal:
                    fill = FILL_REF if tem else fill
                poe(f"{f}:valor", _reais((item or {}).get("original")) if tem else None, formato=FMT_REAIS, preenchimento=fill)
                poe(f"{f}:sit", SITUACAO_FONTE.get(estado, estado), preenchimento=fill)
                pct = (item or {}).get("percentual") if comp in ("SIM", "RESSALVA") else None
                poe(f"{f}:pct", pct, formato=FMT_PCT)
                poe(f"{f}:comp", COMPARABILIDADE.get(comp, "") if tem else "")
                if comp in ("SIM", "RESSALVA") and (item or {}).get("assinada") is not None:
                    a, pc = item["assinada"], item.get("percentual")
                    ok = abs(a) <= 100_000 if pc is None else not (abs(a) > 100_000 and abs(pc) > 0.01)
                    (concordam if ok else divergem).append(s)
            poe("n_fontes", com_valor)
            poe("ausentes", "; ".join(ausentes) or "—")
            poe("concordam", ", ".join(concordam) or "—")
            poe("divergem", ", ".join(divergem) or "—")
            poe("maior_abs", _reais(caso.maior_dif_abs), formato=FMT_REAIS)
            poe("maior_pct", caso.maior_dif_pct, formato=FMT_PCT)
            poe("maior_par", " × ".join(SIGLA.get(x, x) for x in (caso.par_maior or "").split("×") if x) or None)
            resumo = json.loads(caso.alertas_resumo or "[]")
            poe("alertas_cod", ", ".join(r for r, _g in resumo) or None)
            poe("alertas", "; ".join(f"{REGRAS[r]['nome'] if r in REGRAS else r} ({GRAVIDADES.get(g, g)})" for r, g in resumo) or None)
            poe("gravidade", GRAVIDADES.get(caso.gravidade_max, "") if caso.gravidade_max else "Nenhuma")
            texto = " | ".join(mensagens.get(caso.id, []))
            poe("detalhe", (texto[:3000] + "…") if len(texto) > 3000 else (texto or None))
            poe("diag", DIAGNOSTICOS.get(caso.diagnostico, caso.diagnostico))
            poe("situacao", SITUACOES.get(situacao, (situacao,))[0] if situacao else "")
            nivel = caso.confianca
            estilo_nivel = ESTILO_NIVEL[nivel]
            poe("nivel", nivel if nivel is not None else "—", **estilo_nivel)
            poe("nivel_nome", NIVEIS[nivel][0] if nivel is not None else "Não avaliado", **estilo_nivel)
            poe("nivel_motivo", caso.confianca_motivo or None)
            poe("prop_valor", _reais(caso.valor_proposto), formato=FMT_REAIS)
            poe("prop_fonte", SIGLA.get(caso.declaracao_proposta, caso.declaracao_proposta) if caso.declaracao_proposta else None)
            poe("prop_motivo", caso.motivo_proposta or None)
            if decisao is not None:
                poe("dec_acao", ACOES.get(decisao.acao, {}).get("rotulo", decisao.acao) + ("" if decisao.elegivel else " (desconsiderado)"))
                poe("dec_valor", _reais(decisao.valor_adotado), formato=FMT_REAIS)
                poe("dec_fonte", "correção" if decisao.declaracao_adotada == "CORRECAO" else SIGLA.get(decisao.declaracao_adotada, decisao.declaracao_adotada))
                poe("dec_quem", f"{decisao.usuario}, {decisao.criado_em:%d/%m/%Y %H:%M}")
            poe("caso", caso.id)
            celulas = []
            for item in linha:
                if item is None:
                    celulas.append(None)
                    continue
                valor, estilo = item
                if not estilo or (valor is None and not estilo.get("preenchimento")):
                    celulas.append(valor)
                    continue
                celulas.append(_celula(ws, valor, fonte=estilo.get("fonte", F_NORMAL), preenchimento=estilo.get("preenchimento"),
                                       formato=estilo.get("formato"), alinhamento=estilo.get("alinhamento")))
            ws.append(celulas)
        ultima = total + 1
        ws.auto_filter.ref = f"A1:{get_column_letter(len(colunas))}{ultima}"

        def col(chave):
            return get_column_letter(indice[chave] + 1)

        def faixa(chave):
            return f"Central!${col(chave)}$2:${col(chave)}${ultima}"

        componentes = [c for c in sorted(ORDEM_COMP, key=ORDEM_COMP.get) if c != "FUNDOS_UF"]

        # ------------------------------------------------------------ Alertas
        aviso("Abas explicativas...")
        wa = wb.create_sheet("Alertas")
        for letra, largura in zip("ABCDEFGHIJ", (8, 30, 14, 50, 44, 50, 34, 12, 12, 14)):
            wa.column_dimensions[letra].width = largura
        wa.append([_celula(wa, "Alertas — o que cada regra verifica e o que retorna", F_TITULO)])
        wa.append([_celula(wa, "Uma regra 'dispara' um alerta quando encontra o problema descrito. A coluna 'Casos' conta, na aba Central, os casos com o código na coluna 'Alertas (códigos)'. Parâmetros são hipóteses de trabalho, configuráveis na aplicação (aba Regras).", F_SUTIL, alinhamento=QUEBRA)])
        wa.append([])
        wa.append([_celula(wa, t, F_CAB, FILL_CAB, alinhamento=QUEBRA) for t in
                   ("Código", "Nome", "Tipo", "O que verifica", "O que o alerta retorna", "Fórmula", "Parâmetros vigentes", "Gravidade padrão", "Casos", "% dos casos")])
        regras_cfg = parametros.get("regras", {})
        for n, (codigo, r) in enumerate(REGRAS.items(), start=5):
            params = (regras_cfg.get(codigo, {}) or {}).get("parametros", r.get("parametros", {}))
            gravidade = (regras_cfg.get(codigo, {}) or {}).get("gravidade", r.get("gravidade"))
            wa.append([
                _celula(wa, codigo, F_NEGRITO, alinhamento=TOPO),
                _celula(wa, r["nome"], alinhamento=QUEBRA),
                _celula(wa, r.get("tipo", ""), alinhamento=QUEBRA),
                _celula(wa, r.get("objetivo", ""), alinhamento=QUEBRA),
                _celula(wa, SAIDA_REGRA.get(codigo, ""), alinhamento=QUEBRA),
                _celula(wa, r.get("formula", ""), alinhamento=QUEBRA),
                _celula(wa, "; ".join(f"{k} = {v}" for k, v in params.items()) or "—", alinhamento=QUEBRA),
                _celula(wa, GRAVIDADES.get(GRAVIDADE_POR_NOME.get(gravidade, gravidade), gravidade), alinhamento=TOPO),
                _celula(wa, f'=COUNTIF({faixa("alertas_cod")},"*{codigo}*")', formato="#,##0"),
                _celula(wa, f"=IFERROR(I{n}/(COUNTA({faixa('caso')})),0)", formato="0.0%"),
            ])
        wa.append([])
        wa.append([_celula(wa, "Gravidades", F_NEGRITO)])
        for g, texto in ((4, "Alta: afeta o valor de referência de forma relevante (ex.: divergência acima de 20%)."),
                         (3, "Média: precisa de conclusão do analista antes de o valor ser considerado elegível."),
                         (2, "Baixa: indício a verificar; não bloqueia o valor."),
                         (1, "Informativa: registro de contexto (ex.: diferença de conceito explicada); não conta como alerta.")):
            wa.append([_celula(wa, GRAVIDADES[g], F_NEGRITO), _celula(wa, texto, alinhamento=QUEBRA)])

        # ------------------------------------------------------------ Análise
        wn = wb.create_sheet("Análise")
        for letra, largura in zip("ABCDEFG", (8, 16, 80, 40, 12, 10, 4)):
            wn.column_dimensions[letra].width = largura
        wn.append([_celula(wn, "Coluna Análise — escala de confiança no valor principal", F_TITULO)])
        wn.append([_celula(wn, "Pergunta respondida: o valor principal deste caso é confiável para entrar na apuração? Fontes independentes: SIOPE, SIOPS e TCE. RREO e MSC são do mesmo declarante da DCA (SICONFI) e reforçam, mas não bastam sozinhos. 'Coincide' = diferença até 1% ou até R$ 1.000. A decisão do analista não altera o nível.", F_SUTIL, alinhamento=QUEBRA)])
        wn.append([])
        wn.append([_celula(wn, t, F_CAB, FILL_CAB, alinhamento=QUEBRA) for t in ("Nível", "Nome", "Quando o caso recebe o nível", "Como usar", "Casos", "%")])
        for n, nivel in enumerate([5, 4, 3, 2, 1, 0, None], start=5):
            fundo, letra = CORES_NIVEL[nivel]
            estilo = dict(preenchimento=PatternFill("solid", fgColor=fundo), fonte=Font(name=ARIAL, size=10, bold=True, color=letra))
            criterio = f'"{nivel}"' if nivel is not None else '"—"'
            contagem = f"=COUNTIF({faixa('nivel')},{nivel})" if nivel is not None else f'=COUNTIF({faixa("nivel")},{criterio})'
            wn.append([
                _celula(wn, nivel if nivel is not None else "—", **estilo, alinhamento=TOPO),
                _celula(wn, NIVEIS[nivel][0] if nivel is not None else "Não avaliado", **estilo, alinhamento=TOPO),
                _celula(wn, CRITERIO_NIVEL[nivel], alinhamento=QUEBRA),
                _celula(wn, LEITURA_NIVEL[nivel], alinhamento=QUEBRA),
                _celula(wn, contagem, formato="#,##0"),
                _celula(wn, f"=IFERROR(E{n}/SUM($E$5:$E$11),0)", formato="0.0%"),
            ])
        wn.append([_celula(wn, "Total", F_NEGRITO), None, None, None, _celula(wn, "=SUM(E5:E11)", F_NEGRITO, formato="#,##0"),
                   _celula(wn, "=SUM(F5:F11)", F_NEGRITO, formato="0.0%")])

        # ------------------------------------------------------------ Domínios
        wd = wb.create_sheet("Domínios")
        wd.column_dimensions["A"].width = 30
        wd.column_dimensions["B"].width = 100
        wd.append([_celula(wd, "Domínios — significado dos códigos usados na aba Central", F_TITULO)])

        def bloco(titulo, itens):
            wd.append([])
            wd.append([_celula(wd, titulo, F_CAB, FILL_CAB), _celula(wd, "Significado", F_CAB, FILL_CAB)])
            for chave, texto in itens:
                wd.append([_celula(wd, chave, F_NEGRITO, alinhamento=TOPO), _celula(wd, texto, alinhamento=QUEBRA)])

        bloco("Fontes", [(f"{SIGLA[f]}", f"{NOME_FONTE[f]}" + (" — mesmo declarante da DCA (não conta como validação independente)." if f in ("SICONFI_RREO", "SICONFI_MSC") else (" — fonte principal (referência)." if f == "SICONFI_DCA" else " — fonte independente."))) for f in FONTES])
        bloco("Componentes", [(ROTULO_CURTO[c], f"{NOME_COMP[c]}") for c in componentes])
        bloco("Situação da fonte (colunas '— situação')", list(EXPLICA_SITUACAO_FONTE.items()))
        bloco("Comparabilidade", list(EXPLICA_COMPARABILIDADE.items()))
        bloco("Diagnóstico", list(EXPLICA_DIAGNOSTICO.items()))
        bloco("Situação da análise", [(rotulo, {
            "PENDENTE": "Tem alerta e ainda não foi analisado.", "REANALISE": "Havia decisão, mas as evidências mudaram depois dela.",
            "AGUARDANDO_ESCLARECIMENTO": "Analista pediu esclarecimento ao ente ou à fonte.", "ESCLARECIDO": "Analisado; o valor principal foi mantido ou explicado.",
            "AJUSTADO": "Analisado; outro valor foi adotado (outra fonte ou correção).", "DESCONSIDERADO": "Valor declarado inelegível para a apuração.",
            "SEM_PENDENCIA": "Sem alerta e sem decisão.", "NAO_EXIGIVEL": "Declaração não exigível.", "EXCLUIDO": "Fora do universo.",
        }.get(codigo, "")) for codigo, (rotulo, _c) in SITUACOES.items()])
        bloco("Cores na aba Central", [
            ("Vermelho claro", "Fonte sem valor no caso (registro sem valor, sem registro ou consulta com erro)."),
            ("Laranja claro", "Zero declarado."), ("Azul claro", "Fonte usada como referência do caso."),
            ("Coluna Análise", "Cor do nível de confiança, do vermelho escuro (0) ao verde escuro (5)."),
        ])
        bloco("Valores e diferenças", [
            ("Valores em R$", "Valores originais informados por cada fonte (total declarado), em reais com centavos."),
            ("Diferença %", "(fonte − referência) ÷ |referência|, na parcela comparada. Positivo: fonte acima da referência."),
            ("Tolerância", "Uma fonte coincide com a referência quando a diferença é de até 1% ou até R$ 1.000 (a divergência exige exceder os dois)."),
        ])

        # ------------------------------------------------------------ Resumo (fórmulas)
        wr = ws_resumo
        for letra, largura in zip("ABCDEFGHIJKL", (34, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13, 13)):
            wr.column_dimensions[letra].width = largura
        wr.append([_celula(wr, "Resumo por componente (calculado a partir da aba Central)", F_TITULO)])
        wr.append([_celula(wr, f"Exercícios 2019 a 2025 · validação nº {execucao.id} de {execucao.concluida_em:%d/%m/%Y %H:%M} · data de corte {execucao.data_corte:%d/%m/%Y}", F_SUTIL)])
        wr.append([])
        rotulos = [ROTULO_CURTO[c] for c in componentes]
        wr.append([_celula(wr, "Casos e confiança", F_CAB, FILL_CAB)] + [_celula(wr, r, F_CAB, FILL_CAB) for r in rotulos] + [_celula(wr, "Total", F_CAB, FILL_CAB)])
        comp_col = faixa("comp")
        linha_atual = 5
        ultima_comp = get_column_letter(1 + len(rotulos))
        tot_col = get_column_letter(2 + len(rotulos))

        def linha_formula(rotulo, formula_por_comp, formato="#,##0", negrito=False):
            nonlocal linha_atual
            cells = [_celula(wr, rotulo, F_NEGRITO if negrito else F_NORMAL)]
            for r in rotulos:
                cells.append(_celula(wr, formula_por_comp(r), F_NEGRITO if negrito else F_NORMAL, formato=formato))
            if formato == "#,##0":
                cells.append(_celula(wr, f"=SUM(B{linha_atual}:{ultima_comp}{linha_atual})", F_NEGRITO, formato=formato))
            wr.append(cells)
            linha_atual += 1

        linha_casos = linha_atual
        linha_formula("Casos (ente × exercício)", lambda r: f'=COUNTIF({comp_col},"{r}")', negrito=True)
        for nivel in (5, 4, 3, 2, 1, 0):
            linha_formula(f"Análise {nivel} · {NIVEIS[nivel][0]}", lambda r, n=nivel: f'=COUNTIFS({comp_col},"{r}",{faixa("nivel")},{n})')
        linha_formula("Não avaliado", lambda r: f'=COUNTIFS({comp_col},"{r}",{faixa("nivel")},"—")')
        linha_formula("Com alerta", lambda r: f'=COUNTIFS({comp_col},"{r}",{faixa("diag")},"Com alerta")')
        linha_formula("Pendentes de análise", lambda r: f'=COUNTIFS({comp_col},"{r}",{faixa("situacao")},"Pendente de análise")')
        linha_formula("Com decisão do analista", lambda r: f'=COUNTIFS({comp_col},"{r}",{faixa("dec_acao")},"<>")')
        wr.append([])
        linha_atual += 1
        wr.append([_celula(wr, "Completude: % de casos com valor", F_CAB, FILL_CAB)] + [_celula(wr, r, F_CAB, FILL_CAB) for r in rotulos])
        linha_atual += 1
        for f in FONTES:
            sit = faixa(f"{f}:sit")
            linha_formula(SIGLA[f], lambda r, s=sit: f'=IFERROR((COUNTIFS({comp_col},"{r}",{s},"Valor")+COUNTIFS({comp_col},"{r}",{s},"Zero declarado"))/{get_column_letter(2 + rotulos.index(r))}${linha_casos},0)', formato="0.0%")
        linha_formula("Todas as 6 fontes", lambda r: f'=IFERROR(COUNTIFS({comp_col},"{r}",{faixa("n_fontes")},6)/{get_column_letter(2 + rotulos.index(r))}${linha_casos},0)', formato="0.0%")
        wr.append([])
        linha_atual += 1
        wr.append([_celula(wr, "Ausências (nº de casos sem valor)", F_CAB, FILL_CAB)] + [_celula(wr, r, F_CAB, FILL_CAB) for r in rotulos] + [_celula(wr, "Total", F_CAB, FILL_CAB)])
        linha_atual += 1
        for f in FONTES:
            sit = faixa(f"{f}:sit")
            for estado in ("Registro sem valor", "Sem registro", "Consulta com erro"):
                if estado == "Consulta com erro" and f not in ("SICONFI_RREO", "SICONFI_MSC"):
                    continue
                linha_formula(f"{SIGLA[f]} — {estado.lower()}", lambda r, s=sit, e=estado: f'=COUNTIFS({comp_col},"{r}",{s},"{e}")')
        wr.append([])
        linha_atual += 1
        wr.append([_celula(wr, "Concordância com a referência (% dos casos)", F_CAB, FILL_CAB)] + [_celula(wr, r, F_CAB, FILL_CAB) for r in rotulos])
        linha_atual += 1
        for f in FONTES[1:]:
            s = SIGLA[f]
            linha_formula(f"{s} coincide com a referência", lambda r, s=s: f'=IFERROR(COUNTIFS({comp_col},"{r}",{faixa("concordam")},"*{s}*")/{get_column_letter(2 + rotulos.index(r))}${linha_casos},0)', formato="0.0%")

        # ------------------------------------------------------------ Leia-me
        wl = ws_leia
        wl.column_dimensions["A"].width = 26
        wl.column_dimensions["B"].width = 110
        wl.append([_celula(wl, "Central de inconsistências — planilha completa (2019 a 2025)", F_TITULO)])
        wl.append([_celula(wl, f"Gerada em {datetime.now():%d/%m/%Y %H:%M} a partir da validação nº {execucao.id} ({execucao.concluida_em:%d/%m/%Y %H:%M}), data de corte {execucao.data_corte:%d/%m/%Y}, regras versão {execucao.conjunto_id}.", F_SUTIL)])
        wl.append([])
        itens = [
            ("O que é", "Uma linha por ente × exercício × componente, com o valor de cada fonte, o que está ausente, os alertas, o nível de confiança (Análise), o valor proposto e a decisão do analista. É o mesmo conteúdo da central de inconsistências da aplicação, sem filtro."),
            ("Universo", f"{total:,} casos: todos os municípios (ISS e cota-parte do ICMS), os 26 estados (ICMS) e o Distrito Federal (ISS e ICMS), exercícios 2019 a 2025. Entes fora do universo no exercício foram omitidos. Contribuições a fundos estaduais não aparecem: aguardam a informação dos Estados ao CGIBS.".replace(",", ".")),
            ("Fontes", "DCA (referência), RREO e MSC (SICONFI, mesmo declarante); SIOPE, SIOPS e TCE (independentes). Detalhes na aba Domínios."),
            ("Ausências", "Para cada fonte, a coluna '— situação' diz se há valor, zero, registro sem valor, sem registro ou consulta com erro. A coluna 'Fontes ausentes' resume o que falta no caso. Células em vermelho claro indicam ausência."),
            ("Como filtrar", "A aba Central tem filtro em todas as colunas. Exemplos: Análise (nível) ≤ 2 para os casos a analisar; 'Alertas (códigos)' contém DAX; 'Fontes ausentes' contém DCA."),
            ("Abas", "Resumo: contagens por componente (fórmulas sobre a Central). Alertas: o que cada regra verifica e retorna. Análise: os níveis de confiança 0 a 5. Domínios: significado de cada código. Central: os dados."),
            ("Valores", "Em reais com centavos. Diferenças percentuais = (fonte − referência) ÷ |referência|. Passe o mouse sobre o cabeçalho de cada coluna da Central para ver a descrição."),
            ("Atenção", "Parâmetros e conceitos são hipóteses de trabalho (pendências P1 a P13 na aba Fontes e metodologia da aplicação). Nenhum valor desta planilha é a apuração do coeficiente."),
        ]
        for titulo, texto in itens:
            wl.append([_celula(wl, titulo, F_NEGRITO, alinhamento=TOPO), _celula(wl, texto, alinhamento=QUEBRA)])
        wl.append([])
        wl.append([_celula(wl, "Colunas da aba Central", F_CAB, FILL_CAB), _celula(wl, "Descrição", F_CAB, FILL_CAB)])
        for _chave, texto, _l, descricao in colunas:
            wl.append([_celula(wl, texto, F_NEGRITO, alinhamento=TOPO), _celula(wl, descricao, alinhamento=QUEBRA)])

        aviso("Gravando o arquivo...")
        wb.save(destino)
    return {"arquivo": str(destino), "casos": total, "colunas": len(colunas), "execucao": execucao.id}
