"""Exportação da consulta filtrada (CSV/XLSX) e do pacote de conferência (ZIP)."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import NORMAS, PENDENCIAS, SIGLAS
from validacao.conferencia.consultas import Filtros, linhas_filtradas
from validacao.conferencia.decisao import ACOES, SITUACOES
from validacao.conferencia.execucao import ultima_execucao
from validacao.conferencia.regras import GRAVIDADES, REGRAS

DIAGNOSTICOS = {
    "ALERTA": "Com alerta",
    "SEM_VALIDACAO": "Sem validação suficiente",
    "CONFERIDO": "Conferido sem alerta",
    "NAO_EXIGIVEL": "Declaração não exigível",
    "EXCLUIDO": "Fora do universo",
}


def _reais(centavos):
    return None if centavos is None else round(centavos / 100, 2)


def linhas_resultado(session: Session, filtros: Filtros) -> tuple[list[str], list[list]]:
    execucao = ultima_execucao(session)
    declaracoes = [d for d, in session.execute(select(cf.Declaracao.id).order_by(cf.Declaracao.ordem))]
    cabecalho = [
        "execucao", "data_corte", "codigo_ibge", "ente", "uf", "exercicio", "componente", "diagnostico", "alertas",
        "gravidade_maxima", "declaracao_principal", "valor_principal",
    ]
    for d in declaracoes:
        sigla = SIGLAS.get(d, d)
        cabecalho += [f"{sigla}_valor_original", f"{sigla}_estado", f"{sigla}_comparabilidade", f"{sigla}_dif_percentual"]
    cabecalho += [
        "maior_diferenca_par", "maior_diferenca_reais", "maior_diferenca_percentual", "valor_proposto", "fonte_proposta",
        "motivo_proposta", "situacao_analise", "acao", "valor_adotado", "fonte_adotada", "elegivel", "justificativa",
        "evidencias", "responsavel", "data_decisao",
    ]
    dados = []
    for item in linhas_filtradas(session, filtros):
        caso, decisao, situacao, valores = item["caso"], item["decisao"], item["situacao"], item["valores"]
        linha = [
            execucao.id if execucao else None, execucao.data_corte.strftime("%d/%m/%Y") if execucao and execucao.data_corte else "",
            caso.id_ente, caso.nome_ente, caso.uf, caso.exercicio, caso.componente_id,
            DIAGNOSTICOS.get(caso.diagnostico, caso.diagnostico), caso.tipos.strip("|").replace("|", ", "),
            GRAVIDADES.get(caso.gravidade_max, ""), SIGLAS.get(caso.declaracao_principal or "", ""), _reais(caso.valor_principal),
        ]
        for d in declaracoes:
            v = valores.get(d) or {}
            pct = v.get("percentual")
            linha += [_reais(v.get("original")), v.get("estado", ""), v.get("comparabilidade", ""), None if pct is None else round(pct * 100, 4)]
        linha += [
            caso.par_maior or "", _reais(caso.maior_dif_abs), None if caso.maior_dif_pct is None else round(caso.maior_dif_pct * 100, 4),
            _reais(caso.valor_proposto), SIGLAS.get(caso.declaracao_proposta or "", ""), caso.motivo_proposta,
            SITUACOES.get(situacao, (situacao,))[0],
        ]
        if decisao is not None:
            linha += [
                ACOES.get(decisao.acao, {}).get("rotulo", decisao.acao), _reais(decisao.valor_adotado),
                SIGLAS.get(decisao.declaracao_adotada or "", decisao.declaracao_adotada or ""), "sim" if decisao.elegivel else "não",
                decisao.justificativa, decisao.evidencias, decisao.usuario, decisao.criado_em.strftime("%d/%m/%Y %H:%M"),
            ]
        else:
            linha += ["", None, "", "", "", "", "", ""]
        dados.append(linha)
    return cabecalho, dados


def _celula_csv(v):
    return "" if v is None else (str(v).replace(".", ",") if isinstance(v, float) else v)


def csv_bytes(cabecalho: list[str], dados: list[list]) -> bytes:
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow(cabecalho)
    for linha in dados:
        escritor.writerow([_celula_csv(v) for v in linha])
    return saida.getvalue().encode("utf-8-sig")


def gravar_csv(destino, cabecalho: list[str], dados) -> None:
    with open(destino, "w", encoding="utf-8-sig", newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=";")
        escritor.writerow(cabecalho)
        for linha in dados:
            escritor.writerow([_celula_csv(v) for v in linha])


def _livro(abas: dict[str, tuple[list[str], list[list]]]) -> Workbook:
    """Planilha em modo write_only: memória constante e ~2× mais rápida que o modo normal (medido)."""
    from openpyxl.cell import WriteOnlyCell

    livro = Workbook(write_only=True)
    for nome, (cabecalho, dados) in abas.items():
        aba = livro.create_sheet(nome[:31])
        aba.freeze_panes = "A2"
        cab = []
        for texto in cabecalho:
            celula = WriteOnlyCell(aba, value=texto)
            celula.font = Font(bold=True)
            cab.append(celula)
        aba.append(cab)
        for linha in dados:
            aba.append(linha)
    return livro


def xlsx_bytes(abas: dict[str, tuple[list[str], list[list]]]) -> bytes:
    saida = io.BytesIO()
    _livro(abas).save(saida)
    return saida.getvalue()


def gravar_xlsx(destino, abas: dict[str, tuple[list[str], list[list]]]) -> None:
    _livro(abas).save(destino)


def pacote(session: Session, filtros: Filtros, usuario: str) -> bytes:
    execucao = ultima_execucao(session)
    cabecalho, dados = linhas_resultado(session, filtros)
    itens = linhas_filtradas(session, filtros)
    casos = [i["caso"] for i in itens]
    caso_ids = [c.id for c in casos]
    chaves = {(c.id_ente, c.exercicio, c.componente_id) for c in casos}
    alertas_cab = ["codigo_ibge", "ente", "exercicio", "regra", "regra_nome", "gravidade", "declaracao_a", "declaracao_b",
                   "valor_a", "valor_b", "diferenca_assinada", "diferenca_absoluta", "diferenca_percentual", "explicacao", "parametros"]
    alertas = []
    por_id = {c.id: c for c in casos}
    for inicio in range(0, len(caso_ids), 900):
        for a in session.scalars(select(cf.Alerta).where(cf.Alerta.caso_id.in_(caso_ids[inicio:inicio + 900]))):
            caso = por_id[a.caso_id]
            evid = json.loads(a.evidencias or "{}")
            alertas.append([
                caso.id_ente, caso.nome_ente, caso.exercicio, a.regra, REGRAS.get(a.regra, {}).get("nome", a.regra),
                GRAVIDADES.get(a.gravidade, a.gravidade), SIGLAS.get(a.declaracao_a or "", a.declaracao_a or ""),
                SIGLAS.get(a.declaracao_b or "", a.declaracao_b or ""), _reais(a.valor_a), _reais(a.valor_b),
                _reais(a.dif_assinada), _reais(a.dif_abs), None if a.dif_pct is None else round(a.dif_pct * 100, 4),
                a.mensagem, json.dumps(evid.get("parametros", {}), ensure_ascii=False),
            ])
    decisoes_cab = ["id", "codigo_ibge", "exercicio", "componente", "acao", "situacao", "valor_anterior", "valor_adotado",
                    "fonte_adotada", "registro_adotado", "elegivel", "justificativa", "evidencias", "responsavel", "data",
                    "vigente", "substitui", "execucao", "conjunto_regras", "origem"]
    decisoes = []
    if chaves:
        for d in session.scalars(select(cf.DecisaoCaso).order_by(cf.DecisaoCaso.id)):
            if (d.id_ente, d.exercicio, d.componente_id) not in chaves:
                continue
            decisoes.append([
                d.id, d.id_ente, d.exercicio, d.componente_id, ACOES.get(d.acao, {}).get("rotulo", d.acao),
                SITUACOES.get(d.situacao_analise, (d.situacao_analise,))[0], _reais(d.valor_anterior), _reais(d.valor_adotado),
                d.declaracao_adotada or "", d.registro_adotado_id or "", "sim" if d.elegivel else "não", d.justificativa,
                d.evidencias, d.usuario, d.criado_em.strftime("%d/%m/%Y %H:%M"), "sim" if d.vigente else "não",
                d.substitui_id or "", d.execucao_id or "", d.conjunto_id or "", d.origem,
            ])
    registros_cab = ["registro", "codigo_ibge", "exercicio", "declaracao", "campo", "parcela", "conceito", "valor_texto",
                     "valor", "estado", "periodo", "versao", "vigente", "lote", "arquivo", "aba", "linha", "coluna", "data_extracao"]
    registros = []
    arquivos_lote = {lote.id: lote.arquivo for lote in session.scalars(select(cf.Lote))}
    entes = sorted({c.id_ente for c in casos})
    componentes = sorted({c.componente_id for c in casos})
    for inicio in range(0, len(entes), 900):
        consulta = select(cf.Registro).where(
            cf.Registro.id_ente.in_(entes[inicio:inicio + 900]), cf.Registro.componente_id.in_(componentes)
        ).order_by(cf.Registro.id_ente, cf.Registro.exercicio, cf.Registro.declaracao_id, cf.Registro.campo, cf.Registro.versao)
        for r in session.scalars(consulta):
            if (r.id_ente, r.exercicio, r.componente_id) not in chaves:
                continue
            p = json.loads(r.procedencia or "{}")
            registros.append([
                r.id, r.id_ente, r.exercicio, SIGLAS.get(r.declaracao_id, r.declaracao_id), r.campo, r.parcela or "",
                r.conceito or "", r.valor_texto or "", _reais(r.valor_centavos), r.estado, r.periodo, r.versao,
                "sim" if r.vigente else "não", r.lote_id, p.get("arquivo") or arquivos_lote.get(r.lote_id, ""), p.get("aba", ""), p.get("linha", ""),
                p.get("coluna", ""), r.data_extracao.strftime("%d/%m/%Y") if r.data_extracao else "",
            ])
    conjunto = session.get(cf.ConjuntoRegras, execucao.conjunto_id) if execucao else None
    lotes = []
    for lote in session.scalars(select(cf.Lote).order_by(cf.Lote.id)):
        lotes.append({
            "lote": lote.id, "declaracao": lote.declaracao_id, "origem": lote.origem_tipo, "arquivo": lote.arquivo,
            "aba": lote.aba, "hash": lote.hash_conteudo, "data_extracao": str(lote.data_extracao or ""),
            "importado_em": str(lote.data_importacao), "usuario": lote.usuario, "ficticio": lote.ficticio,
        })
    mapeamentos = [
        {"declaracao": m.declaracao_id, "campo": m.campo, "componente": m.componente_id, "parcela": m.parcela,
         "conceito": m.conceito, "origem": m.origem_detalhe, "status": m.status, "evidencia": m.evidencia, "versao": m.versao}
        for m in session.scalars(select(cf.Mapeamento).order_by(cf.Mapeamento.declaracao_id, cf.Mapeamento.campo))
    ]
    execucao_info = {
        "execucao": execucao.id if execucao else None,
        "iniciada_em": str(execucao.iniciada_em) if execucao else None,
        "concluida_em": str(execucao.concluida_em) if execucao else None,
        "data_corte": str(execucao.data_corte) if execucao else None,
        "usuario": execucao.usuario if execucao else None,
        "lotes_considerados": json.loads(execucao.lotes) if execucao else [],
        "conjunto_regras": conjunto.id if conjunto else None,
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gerado_por": usuario,
        "filtros": filtros.query(),
    }
    leia = (
        "PACOTE DE CONFERÊNCIA — coeficiente de participação (IBS)\n\n"
        f"Gerado em {execucao_info['gerado_em']} por {usuario}. Execução de validação nº {execucao_info['execucao']}.\n"
        f"Casos no pacote: {len(casos)} (consulta filtrada: {execucao_info['filtros']}).\n\n"
        "Arquivos:\n"
        "  resultado.csv     um caso por linha (ente × exercício × componente) com valores, diagnóstico, proposta e decisão\n"
        "  alertas.csv       alertas disparados, com explicação e parâmetros\n"
        "  decisoes.csv      histórico completo das decisões (vigentes e substituídas)\n"
        "  registros.csv     registros originais por fonte e versão, com arquivo, aba, linha e coluna\n"
        "  parametros.json   conjunto de regras usado (parâmetros, gravidades, precedência)\n"
        "  execucao.json     lotes considerados, data de corte e filtros\n"
        "  mapeamentos.json  como cada campo de cada declaração foi interpretado (conceito, parcela, status)\n\n"
        "Valores em reais com duas casas; cálculos internos em centavos inteiros.\n"
        "Diferença assinada = fonte − referência; percentual = diferença ÷ |referência| (vazio quando a referência é zero).\n\n"
        "Limitações: os parâmetros das regras e a precedência das fontes são hipóteses de trabalho; nenhum valor\n"
        "deste pacote é o coeficiente de participação. Pendências de definição:\n"
        + "\n".join(f"  {codigo} — {titulo}: {texto}" for codigo, titulo, texto in PENDENCIAS)
        + "\n\nEnquadramento normativo consultado:\n"
        + "\n".join(f"  {n['ref']} ({n['versao']})" for n in NORMAS)
        + "\n"
    )
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as zipado:
        zipado.writestr("LEIA-ME.txt", leia.encode("utf-8"))
        zipado.writestr("resultado.csv", csv_bytes(cabecalho, dados))
        zipado.writestr("alertas.csv", csv_bytes(alertas_cab, alertas))
        zipado.writestr("decisoes.csv", csv_bytes(decisoes_cab, decisoes))
        zipado.writestr("registros.csv", csv_bytes(registros_cab, registros))
        zipado.writestr("parametros.json", json.dumps(json.loads(conjunto.parametros) if conjunto else {}, ensure_ascii=False, indent=2))
        zipado.writestr("execucao.json", json.dumps({**execucao_info, "lotes": lotes}, ensure_ascii=False, indent=2))
        zipado.writestr("mapeamentos.json", json.dumps(mapeamentos, ensure_ascii=False, indent=2))
    return saida.getvalue()
