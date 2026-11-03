"""Importação das bases com layout fixo: DCA (SICONFI), SIOPE, SIOPS e Tribunais de Contas.

São as mesmas planilhas que alimentaram a versão 1. A leitura segue as mesmas regras da
carga original (colunas, campos e esferas), de modo que reimportar uma planilha já
migrada não gera nada novo, e uma planilha atualizada gera só as diferenças:

- valor igual ao vigente: repetido, nada é gravado;
- valor diferente: nova versão do registro (retificação), a anterior é preservada;
- valor inexistente até então: registro novo.

O tipo da planilha é reconhecido pelas abas e cabeçalhos, não pelo nome do arquivo.
Células vazias não geram registro (como na carga original); compensações financeiras,
fora do art. 115, são ignoradas.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.cadastro import garantir_municipio, mapa_de_codigos
from validacao.catalogo import POR_ID, permitidos, semear
from validacao.cargas import siconfi as v1_siconfi
from validacao.cargas import siope as v1_siope
from validacao.cargas import siops as v1_siops
from validacao.cargas import tce as v1_tce
from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import (
    COMPONENTE_DF,
    COMPONENTE_DO_LEGADO,
    COMPONENTES_MIGRADOS,
    MAPEAMENTOS_LEGADO,
    NATUREZA_TCE,
    semear_catalogo,
)
from validacao.conferencia.importacao import Linha, persistir
from validacao.modelos import Ente

LAYOUTS = {
    "SICONFI": {
        "declaracao": "SICONFI_DCA", "nome": "DCA — SICONFI", "arquivo": "SICONFI_DCA_Receitas_2019_2025.xlsx",
        "descricao": "Abas SICONFI (totais) e SICONFI_BRUTO (bruto e deduções)",
    },
    "SIOPE": {
        "declaracao": "SIOPE", "nome": "SIOPE", "arquivo": "Dados SIOPE - ICMS e ISS.xlsx",
        "descricao": "Abas Municipios e Estados, com abertura do ISS e do ICMS por item",
    },
    "SIOPS": {
        "declaracao": "SIOPS", "nome": "SIOPS", "arquivo": "SIOPS - ICMS e ISS.xlsx",
        "descricao": "Abas Municipios e Estados (ISS, cota-parte, ICMS)",
    },
    "TCE": {
        "declaracao": "TCE", "nome": "Tribunais de Contas", "arquivo": "TCE Arrecadacao Municipios - 2019_2025.xlsx",
        "descricao": "Aba Base detalhada (abertura do ISS e da cota-parte, com a natureza do valor)",
    },
}
ORDEM = ("SICONFI", "SIOPS", "SIOPE", "TCE")  # a DCA primeiro: ela completa o cadastro de entes


class LayoutDesconhecido(ValueError):
    pass


@dataclass
class Leitura:
    fonte: str
    valores: dict = field(default_factory=dict)  # (id_ente, ano, campo) -> dict
    ocorrencias: list = field(default_factory=list)
    exercicios: set = field(default_factory=set)
    data_extracao: datetime | None = None


# ------------------------------------------------------------------ reconhecimento

def _abas(conteudo: bytes) -> dict[str, list[str]]:
    from openpyxl import load_workbook

    livro = load_workbook(io.BytesIO(conteudo), read_only=True)
    try:
        resultado = {}
        for aba in livro.worksheets:
            linhas = []
            for linha in aba.iter_rows(min_row=1, max_row=6, values_only=True):
                linhas.extend(str(v).strip() for v in linha if v is not None)
            resultado[aba.title] = linhas
        return resultado
    finally:
        livro.close()


def identificar(nome_arquivo: str, conteudo: bytes) -> str:
    """Devolve SICONFI, SIOPE, SIOPS ou TCE conforme as abas e cabeçalhos da planilha."""
    if Path(nome_arquivo).suffix.lower() not in (".xlsx", ".xlsm"):
        raise LayoutDesconhecido(f"{nome_arquivo}: as bases de layout fixo são planilhas XLSX.")
    try:
        abas = _abas(conteudo)
    except Exception as erro:  # arquivo corrompido ou não é XLSX
        raise LayoutDesconhecido(f"{nome_arquivo}: não foi possível abrir a planilha ({erro}).") from erro
    if {"SICONFI", "SICONFI_BRUTO"} <= set(abas):
        return "SICONFI"
    if "Base detalhada" in abas and "ISS" in abas:
        return "TCE"
    if {"Municipios", "Estados"} <= set(abas):
        cabecalho = set(abas["Municipios"])
        if "ISS Principal" in cabecalho or "SIOPE" in cabecalho:
            return "SIOPE"
        if "Comp. Fin" in cabecalho or "SIOPS" in cabecalho:
            return "SIOPS"
    raise LayoutDesconhecido(
        f"{nome_arquivo}: layout não reconhecido. Esperado: DCA (abas SICONFI e SICONFI_BRUTO), SIOPE ou SIOPS "
        "(abas Municipios e Estados) ou TCE (aba Base detalhada). Para outros arquivos use a importação avulsa."
    )


def _data_propriedades(conteudo: bytes) -> datetime | None:
    from openpyxl import load_workbook

    try:
        livro = load_workbook(io.BytesIO(conteudo), read_only=True)
        data = livro.properties.modified or livro.properties.created
        livro.close()
        return data.replace(microsecond=0, tzinfo=None) if data else None
    except Exception:
        return None


# ------------------------------------------------------------------ leitura

def _numero(valor):
    return v1_siconfi.numero(valor)


def _inteiro(valor):
    return v1_siconfi.codigo_inteiro(valor)


def _texto(valor):
    return v1_siconfi.texto(valor)


def _acrescentar(leitura: Leitura, id_ente, ano, campo, valor, natureza, coluna, aba, linha, liberados):
    if valor is None or campo not in liberados:
        return
    chave = (id_ente, ano, campo)
    atual = leitura.valores.get(chave)
    if atual is not None:
        if abs(atual["valor"] - valor) > 0.01:
            leitura.ocorrencias.append({
                "linha": linha, "coluna": coluna, "valor": f"{valor:.2f}".replace(".", ","),
                "erro": f"Valor divergente para o mesmo ente e exercício (aba {atual['aba']}, linha {atual['linha']}: "
                        f"{atual['valor']:.2f}); mantido o primeiro".replace(".", ","),
            })
        return
    leitura.valores[chave] = {"valor": valor, "natureza": natureza, "coluna": coluna, "aba": aba, "linha": linha}


class _Codigos:
    """Converte o código da planilha no código IBGE do cadastro (6 ou 7 dígitos, UF, DF)."""

    def __init__(self, session: Session, fonte: str):
        self.session = session
        self.externos = mapa_de_codigos(session, fonte)
        self.esferas = dict(session.execute(select(Ente.id_ente, Ente.esfera)).all())
        self.seis = {i // 10: i for i, e in self.esferas.items() if e == "MUNICIPIO"}
        self.conhecidos = set(self.esferas)

    def id_ente(self, codigo: int | None, nome: str | None = None) -> int | None:
        if codigo is None:
            return None
        if str(codigo) in self.externos:
            return self.externos[str(codigo)]
        if codigo in self.esferas:
            return codigo
        if codigo == 530010:
            return 53 if 53 in self.esferas else None
        if 100000 <= codigo <= 999999 and codigo in self.seis:
            return self.seis[codigo]
        if codigo >= 1_000_000 and nome:  # município novo com código de 7 dígitos
            garantir_municipio(self.session, codigo, nome, self.conhecidos)
            self.esferas[codigo] = "MUNICIPIO"
            return codigo
        return None


def ler_siconfi(session: Session, conteudo: bytes) -> Leitura:
    leitura = Leitura("SICONFI")
    processado = pd.read_excel(io.BytesIO(conteudo), sheet_name="SICONFI")
    bruto = pd.read_excel(io.BytesIO(conteudo), sheet_name="SICONFI_BRUTO")
    faltam = {"ANO", "COD_IBGE", "ESFERA", "ICMS", "ISS", "COTA_MUN", "COTA_EST"} - set(processado.columns)
    if faltam:
        raise LayoutDesconhecido(f"Aba SICONFI sem as colunas: {', '.join(sorted(faltam))}")
    v1_siconfi.registrar_codigos(session, v1_siconfi._entes(session, processado))  # completa o cadastro
    for aba, quadro, colunas in (("SICONFI", processado, v1_siconfi.PROCESSADO), ("SICONFI_BRUTO", bruto, v1_siconfi.BRUTO)):
        for deslocamento, registro in enumerate(quadro.to_dict("records")):
            id_ente, ano = _inteiro(registro.get("COD_IBGE")), _inteiro(registro.get("ANO"))
            if id_ente is None or ano is None:
                continue
            try:
                esfera = v1_siconfi.esfera_de(registro.get("ESFERA"))
            except ValueError:
                leitura.ocorrencias.append({"linha": deslocamento + 2, "coluna": "ESFERA", "valor": str(registro.get("ESFERA")),
                                            "erro": "Esfera desconhecida"})
                continue
            leitura.exercicios.add(ano)
            liberados = permitidos(esfera)
            for origem, campo in colunas.items():
                _acrescentar(leitura, id_ente, ano, campo, _numero(registro.get(origem)), None, origem, aba,
                             deslocamento + 2, liberados)
    return leitura


def _ler_sistema(session: Session, conteudo: bytes, fonte: str, municipios: dict, estados: dict) -> Leitura:
    leitura = Leitura(fonte)
    codigos = _Codigos(session, fonte)
    for aba, colunas in (("Municipios", municipios), ("Estados", estados)):
        quadro = pd.read_excel(io.BytesIO(conteudo), sheet_name=aba)
        faltam = {"COD_IBGE", "ANO"} - set(quadro.columns)
        if faltam:
            raise LayoutDesconhecido(f"Aba {aba} sem as colunas: {', '.join(sorted(faltam))}")
        for deslocamento, registro in enumerate(quadro.to_dict("records")):
            linha = deslocamento + 2
            codigo, ano = _inteiro(registro.get("COD_IBGE")), _inteiro(registro.get("ANO"))
            if codigo is None or ano is None:
                continue
            id_ente = codigos.id_ente(codigo, _texto(registro.get("ENTE")))
            if id_ente is None:
                leitura.ocorrencias.append({"linha": linha, "coluna": "COD_IBGE", "valor": str(codigo),
                                            "erro": f"Código sem ente no cadastro (aba {aba})"})
                continue
            leitura.exercicios.add(ano)
            liberados = permitidos(codigos.esferas[id_ente])
            for origem, campo in colunas.items():
                _acrescentar(leitura, id_ente, ano, campo, _numero(registro.get(origem)), None, origem, aba, linha, liberados)
    return leitura


def ler_siope(session: Session, conteudo: bytes) -> Leitura:
    return _ler_sistema(session, conteudo, "SIOPE", v1_siope.MUNICIPIOS, v1_siope.ESTADOS)


def ler_siops(session: Session, conteudo: bytes) -> Leitura:
    return _ler_sistema(session, conteudo, "SIOPS", v1_siops.MUNICIPIOS, v1_siops.ESTADOS)


def ler_tce(session: Session, conteudo: bytes) -> Leitura:
    leitura = Leitura("TCE")
    universo = pd.read_excel(io.BytesIO(conteudo), sheet_name="ISS", header=3)
    conhecidos = set(session.scalars(select(Ente.id_ente)))
    for registro in universo.to_dict("records"):
        id_ente = _inteiro(registro.get("Cód. IBGE"))
        if id_ente is not None and id_ente >= 1_000_000:
            garantir_municipio(session, id_ente, _texto(registro.get("Município")) or f"Município {id_ente}", conhecidos)
    codigos = _Codigos(session, "TCE")
    detalhe = pd.read_excel(io.BytesIO(conteudo), sheet_name="Base detalhada", header=4)
    if detalhe.shape[1] < 22:
        raise LayoutDesconhecido("Aba Base detalhada com menos colunas que o esperado (22).")
    aba = "Base detalhada"
    nomes = v1_tce.NOMES
    for indice, valores in enumerate(detalhe.to_numpy()):
        codigo, ano = _inteiro(valores[2]), _inteiro(valores[4])
        if codigo is None or ano is None:
            continue
        linha = indice + 6
        id_ente = codigos.id_ente(codigo, _texto(valores[3]))
        if id_ente is None:
            leitura.ocorrencias.append({"linha": linha, "coluna": "Cód. IBGE", "valor": str(codigo), "erro": "Código sem ente no cadastro"})
            continue
        leitura.exercicios.add(ano)
        liberados = permitidos(codigos.esferas[id_ente])
        natureza_iss, natureza_cota = _texto(valores[v1_tce.COLUNA_NATUREZA_ISS]), _texto(valores[v1_tce.COLUNA_NATUREZA_COTA])
        for coluna, campo in v1_tce.ISS:
            _acrescentar(leitura, id_ente, ano, campo, _numero(valores[coluna]), natureza_iss, nomes[coluna - 5], aba, linha, liberados)
        total = _numero(valores[v1_tce.COLUNA_ISS_TOTAL])
        if total is None:  # a coluna de referência é fórmula; sem cache, usa o total publicado
            total = _numero(valores[v1_tce.COLUNA_ISS_PUBLICADO])
        _acrescentar(leitura, id_ente, ano, "ISS_TOTAL", total, natureza_iss, "ISS — valor de referência", aba, linha, liberados)
        for coluna, campo in v1_tce.COTA:
            _acrescentar(leitura, id_ente, ano, campo, _numero(valores[coluna]), natureza_cota, nomes[coluna - 5], aba, linha, liberados)
        total = _numero(valores[v1_tce.COLUNA_COTA_TOTAL])
        if total is None:
            total = _numero(valores[v1_tce.COLUNA_COTA_UNICO])
        _acrescentar(leitura, id_ente, ano, "COTA_RECEBIDA_TOTAL", total, natureza_cota, "Cota-parte — valor de referência",
                     aba, linha, liberados)
    leitura.data_extracao = _data_leia_me(conteudo)
    return leitura


def _data_leia_me(conteudo: bytes) -> datetime | None:
    try:
        leia = pd.read_excel(io.BytesIO(conteudo), sheet_name="Leia-me", header=None, nrows=5)
    except ValueError:
        return None
    for valor in leia.to_numpy().ravel():
        encontrado = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(valor))
        if encontrado:
            dia, mes, ano = (int(p) for p in encontrado.groups())
            return datetime(ano, mes, dia)
    return None


LEITORES = {"SICONFI": ler_siconfi, "SIOPE": ler_siope, "SIOPS": ler_siops, "TCE": ler_tce}


# ------------------------------------------------------------------ gravação

def _linhas(session: Session, leitura: Leitura, nome_arquivo: str) -> tuple[list[Linha], dict[str, int]]:
    declaracao = LAYOUTS[leitura.fonte]["declaracao"]
    esferas = dict(session.execute(select(Ente.id_ente, Ente.esfera)).all())
    mapeamentos = {}
    for m in session.scalars(select(cf.Mapeamento).where(cf.Mapeamento.declaracao_id == declaracao).order_by(cf.Mapeamento.versao)):
        mapeamentos[m.campo] = m.id  # a versão mais recente fica por último
    linhas = []
    for (id_ente, ano, campo), dado in leitura.valores.items():
        definicao = POR_ID.get(campo)
        if definicao is None:
            continue
        componente = COMPONENTE_DO_LEGADO.get(definicao.bloco, definicao.bloco)
        if componente not in COMPONENTES_MIGRADOS:  # compensações financeiras: fora do art. 115
            continue
        if esferas.get(id_ente) == "DF":
            componente = COMPONENTE_DF.get(componente, componente)
        parcela = conceito = None
        mapa = MAPEAMENTOS_LEGADO.get((leitura.fonte, campo))
        if mapa:
            _decl, parcela, conceito, *_ = mapa
            if conceito == "POR_REGISTRO":
                conceito = NATUREZA_TCE.get(dado["natureza"] or "", "INDEFINIDO")
        centavos = _centavos(dado["valor"])
        procedencia = {"arquivo": nome_arquivo, "aba": dado["aba"], "linha": dado["linha"], "coluna": dado["coluna"]}
        if dado["natureza"]:
            procedencia["natureza_informada"] = dado["natureza"]
        linhas.append(Linha(id_ente, ano, campo, parcela, conceito, repr(dado["valor"]), centavos,
                            "ZERO" if centavos == 0 else "VALOR", procedencia, componente=componente, periodo="ANUAL"))
    return linhas, mapeamentos


def importar_arquivo(session: Session, nome_arquivo: str, conteudo: bytes, usuario: str,
                     pasta_arquivos: Path | None = None, progresso=None) -> dict:
    """Importa uma planilha de layout fixo. Devolve o resumo do lote."""
    aviso = progresso or (lambda _m: None)
    fonte = identificar(nome_arquivo, conteudo)
    layout = LAYOUTS[fonte]
    hash_conteudo = hashlib.sha256(conteudo).hexdigest()  # o mesmo da carga da versão 1, para reconhecer o arquivo
    existente = session.scalar(select(cf.Lote).where(cf.Lote.hash_conteudo == hash_conteudo, cf.Lote.situacao == "CONCLUIDO"))
    if existente is not None:
        return {"fonte": fonte, "nome": layout["nome"], "arquivo": nome_arquivo, "lote": existente.id, "situacao": "JA_IMPORTADO",
                "novos": 0, "novas_versoes": 0, "repetidos": 0, "ocorrencias": 0,
                "mensagem": f"{layout['nome']}: este arquivo já foi importado no lote {existente.id}; nada mudou."}
    semear(session)
    semear_catalogo(session)
    aviso(f"Lendo {layout['nome']} ({nome_arquivo})...")
    leitura = LEITORES[fonte](session, conteudo)
    linhas, mapeamentos = _linhas(session, leitura, nome_arquivo)
    if not linhas:
        raise LayoutDesconhecido(f"{nome_arquivo}: nenhum valor reconhecido na planilha.")
    data_extracao = leitura.data_extracao or _data_propriedades(conteudo)
    lote = cf.Lote(
        declaracao_id=layout["declaracao"], origem_tipo="ARQUIVO", arquivo=nome_arquivo, aba=None,
        parametros=json.dumps({"layout_fixo": fonte, "exercicios": sorted(leitura.exercicios)}, ensure_ascii=False),
        hash_conteudo=hash_conteudo, data_extracao=data_extracao, data_importacao=datetime.now().replace(microsecond=0),
        usuario=usuario, situacao="EM_ANDAMENTO",
        observacao=f"Base de layout fixo: {layout['nome']} ({layout['descricao']}).",
    )
    session.add(lote)
    session.flush()
    aviso(f"Gravando {_milhar(len(linhas))} valores de {layout['nome']}...")
    contagem = persistir(session, lote, linhas, "", "ANUAL", data_extracao, mapeamentos)
    lote.erros = len(leitura.ocorrencias)
    lote.relatorio = json.dumps({"erros": leitura.ocorrencias[:5000]}, ensure_ascii=False)
    lote.situacao = "CONCLUIDO"
    if pasta_arquivos is not None:
        pasta_arquivos.mkdir(parents=True, exist_ok=True)
        copia = pasta_arquivos / f"lote-{lote.id}-{hash_conteudo[:12]}.xlsx"
        copia.write_bytes(conteudo)
        lote.parametros = json.dumps({**json.loads(lote.parametros), "copia_local": copia.name}, ensure_ascii=False)
    session.commit()
    return {
        "fonte": fonte, "nome": layout["nome"], "arquivo": nome_arquivo, "lote": lote.id, "situacao": "IMPORTADO",
        "novos": contagem["novos"], "novas_versoes": contagem["novas_versoes"], "repetidos": contagem["repetidos"],
        "ocorrencias": len(leitura.ocorrencias),
        "mensagem": (f"{layout['nome']}: lote {lote.id} — {_milhar(contagem['novos'])} novos, "
                     f"{_milhar(contagem['novas_versoes'])} novas versões, {_milhar(contagem['repetidos'])} iguais aos vigentes, "
                     f"{_milhar(len(leitura.ocorrencias))} ocorrências."),
    }


def _centavos(valor: float) -> int:
    """Mesmo arredondamento da migração (ROUND do SQLite sobre valor × 100), para não criar versões falsas."""
    x = valor * 100
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _milhar(numero: int) -> str:
    return f"{numero:,}".replace(",", ".")


def ordenar(arquivos: list[tuple[str, bytes]]) -> list[tuple[str, str, bytes]]:
    """Identifica cada arquivo e põe a DCA primeiro (ela completa o cadastro de entes)."""
    identificados = [(identificar(nome, conteudo), nome, conteudo) for nome, conteudo in arquivos]
    tipos = [t for t, _n, _c in identificados]
    repetidos = {t for t in tipos if tipos.count(t) > 1}
    if repetidos:
        raise LayoutDesconhecido(f"Mais de um arquivo do mesmo tipo: {', '.join(LAYOUTS[t]['nome'] for t in repetidos)}.")
    return sorted(identificados, key=lambda item: ORDEM.index(item[0]))
