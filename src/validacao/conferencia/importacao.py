"""Importação de CSV e XLSX com prévia, mapeamento de colunas e relatório de erros.

- O mesmo conteúdo (arquivo + mapeamento) não é importado duas vezes.
- Valor igual ao vigente não gera registro novo.
- Valor diferente do vigente cria nova versão (retificação) e preserva a anterior.
- Zero, célula vazia e formato inválido ficam registrados como estados distintos.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from validacao.conferencia import modelos as cf
from validacao.conferencia.dinheiro import ValorInvalido, para_centavos
from validacao.modelos import Ente

LIMITE_PREVIA = 15


class ImportacaoRecusada(ValueError):
    pass


@dataclass
class Planilha:
    nome_arquivo: str
    conteudo: bytes
    abas: dict[str, pd.DataFrame]

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.conteudo).hexdigest()


@dataclass
class CampoValor:
    coluna: str
    campo: str
    parcela: str
    conceito: str


@dataclass
class Mapeamento:
    declaracao: str
    componente: str
    aba: str
    linha_cabecalho: int
    coluna_ente: str
    coluna_exercicio: str | None
    exercicio_fixo: int | None
    campos: list[CampoValor]
    periodo: str = "ANUAL"
    escala: int = 1
    data_extracao: datetime | None = None
    coluna_conceito: str | None = None
    conceitos_por_texto: dict[str, str] = field(default_factory=dict)
    observacao: str = ""
    ficticio: bool = False

    def canonico(self) -> str:
        dados = {
            "declaracao": self.declaracao, "componente": self.componente, "aba": self.aba,
            "linha_cabecalho": self.linha_cabecalho, "coluna_ente": self.coluna_ente,
            "coluna_exercicio": self.coluna_exercicio, "exercicio_fixo": self.exercicio_fixo,
            "campos": [c.__dict__ for c in self.campos], "periodo": self.periodo, "escala": self.escala,
            "coluna_conceito": self.coluna_conceito, "conceitos_por_texto": self.conceitos_por_texto,
        }
        return json.dumps(dados, sort_keys=True, ensure_ascii=False)


def ler(nome_arquivo: str, conteudo: bytes) -> Planilha:
    sufixo = Path(nome_arquivo).suffix.lower()
    if sufixo in (".xlsx", ".xlsm"):
        abas = pd.read_excel(io.BytesIO(conteudo), sheet_name=None, header=None, dtype=object)
    elif sufixo in (".csv", ".txt"):
        texto = _decodificar(conteudo)
        try:
            dialeto = csv.Sniffer().sniff(texto[:5000], delimiters=";,\t|")
            separador = dialeto.delimiter
        except csv.Error:
            separador = ";"
        quadro = pd.read_csv(io.StringIO(texto), sep=separador, header=None, dtype=str, keep_default_na=False)
        abas = {"CSV": quadro}
    else:
        raise ImportacaoRecusada("Formato não suportado. Use CSV ou XLSX.")
    return Planilha(nome_arquivo, conteudo, abas)


def _decodificar(conteudo: bytes) -> str:
    for codificacao in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return conteudo.decode(codificacao)
        except UnicodeDecodeError:
            continue
    raise ImportacaoRecusada("Não foi possível ler o texto do arquivo.")


def colunas(planilha: Planilha, aba: str, linha_cabecalho: int) -> list[str]:
    quadro = planilha.abas[aba]
    if linha_cabecalho < 1 or linha_cabecalho > len(quadro):
        raise ImportacaoRecusada("Linha de cabeçalho fora da planilha.")
    nomes = []
    for posicao, valor in enumerate(quadro.iloc[linha_cabecalho - 1].tolist()):
        texto = "" if valor is None or (isinstance(valor, float) and pd.isna(valor)) else str(valor).strip()
        nomes.append(texto or f"Coluna {posicao + 1}")
    vistos: dict[str, int] = {}
    unicos = []
    for nome in nomes:
        if nome in vistos:
            vistos[nome] += 1
            unicos.append(f"{nome} ({vistos[nome]})")
        else:
            vistos[nome] = 1
            unicos.append(nome)
    return unicos


def previa(planilha: Planilha, aba: str, linha_cabecalho: int) -> dict:
    nomes = colunas(planilha, aba, linha_cabecalho)
    quadro = planilha.abas[aba]
    linhas = []
    for indice in range(linha_cabecalho, min(len(quadro), linha_cabecalho + LIMITE_PREVIA)):
        linhas.append({"linha": indice + 1, "valores": [_texto(v) for v in quadro.iloc[indice].tolist()]})
    return {"colunas": nomes, "linhas": linhas, "total_linhas": max(len(quadro) - linha_cabecalho, 0)}


def _texto(valor) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    if isinstance(valor, float) and valor.is_integer() and abs(valor) < 1e15:
        return str(int(valor)) if abs(valor) >= 1e6 else repr(valor)
    return str(valor)


@dataclass
class Linha:
    id_ente: int
    exercicio: int
    campo: str
    parcela: str
    conceito: str
    valor_texto: str
    valor: int | None
    estado: str
    procedencia: dict
    duplicado: str | None = None
    componente: str | None = None
    periodo: str | None = None


def interpretar(session: Session, planilha: Planilha, mapa: Mapeamento) -> tuple[list[Linha], list[dict]]:
    if mapa.aba not in planilha.abas:
        raise ImportacaoRecusada("Aba não encontrada.")
    nomes = colunas(planilha, mapa.aba, mapa.linha_cabecalho)
    indice = {nome: posicao for posicao, nome in enumerate(nomes)}
    obrigatorias = [mapa.coluna_ente] + ([mapa.coluna_exercicio] if mapa.coluna_exercicio else []) + [c.coluna for c in mapa.campos]
    faltam = [c for c in obrigatorias if c not in indice]
    if faltam:
        raise ImportacaoRecusada(f"Colunas não encontradas: {', '.join(faltam)}")
    if not mapa.campos:
        raise ImportacaoRecusada("Escolha ao menos uma coluna de valor.")
    if mapa.coluna_exercicio is None and mapa.exercicio_fixo is None:
        raise ImportacaoRecusada("Informe a coluna do exercício ou um exercício fixo.")
    entes = {id_ente: esfera for id_ente, esfera in session.execute(select(Ente.id_ente, Ente.esfera))}
    seis = {id_ente // 10: id_ente for id_ente in entes if id_ente >= 1_000_000}
    componente = session.get(cf.Componente, mapa.componente)
    if componente is None:
        raise ImportacaoRecusada("Componente desconhecido.")
    quadro = planilha.abas[mapa.aba]
    linhas: list[Linha] = []
    erros: list[dict] = []
    vistos: dict[tuple[int, int, str], Linha] = {}
    for posicao in range(mapa.linha_cabecalho, len(quadro)):
        bruto = quadro.iloc[posicao].tolist()
        numero_linha = posicao + 1
        if all(_texto(v).strip() == "" for v in bruto):
            continue
        codigo_texto = _texto(bruto[indice[mapa.coluna_ente]]).strip()
        try:
            codigo = int(float(codigo_texto.replace(",", "."))) if codigo_texto else None
        except ValueError:
            codigo = None
        if codigo is not None and codigo not in entes and codigo in seis:
            codigo = seis[codigo]
        if codigo is None or codigo not in entes:
            erros.append({"linha": numero_linha, "coluna": mapa.coluna_ente, "valor": codigo_texto, "erro": "Código do ente não encontrado no cadastro"})
            continue
        if entes[codigo] != componente.esfera:
            erros.append({"linha": numero_linha, "coluna": mapa.coluna_ente, "valor": codigo_texto, "erro": f"Ente de outra esfera ({entes[codigo]})"})
            continue
        if mapa.coluna_exercicio:
            ano_texto = _texto(bruto[indice[mapa.coluna_exercicio]]).strip()
            try:
                exercicio = int(float(ano_texto))
            except ValueError:
                erros.append({"linha": numero_linha, "coluna": mapa.coluna_exercicio, "valor": ano_texto, "erro": "Exercício inválido"})
                continue
        else:
            exercicio = int(mapa.exercicio_fixo)
        if exercicio < 2000 or exercicio > 2100:
            erros.append({"linha": numero_linha, "coluna": mapa.coluna_exercicio or "", "valor": str(exercicio), "erro": "Exercício fora do intervalo"})
            continue
        conceito_linha = None
        if mapa.coluna_conceito and mapa.coluna_conceito in indice:
            texto_conceito = _texto(bruto[indice[mapa.coluna_conceito]]).strip()
            conceito_linha = mapa.conceitos_por_texto.get(texto_conceito, "INDEFINIDO" if texto_conceito else None)
        for campo in mapa.campos:
            original = bruto[indice[campo.coluna]]
            texto = _texto(original).strip()
            procedencia = {
                "arquivo": planilha.nome_arquivo, "aba": mapa.aba, "linha": numero_linha, "coluna": campo.coluna,
            }
            try:
                valor = para_centavos(original if not isinstance(original, str) else texto, escala=mapa.escala)
                estado = "AUSENTE" if valor is None else ("ZERO" if valor == 0 else "VALOR")
            except ValorInvalido:
                valor = None
                estado = "INVALIDO"
                erros.append({"linha": numero_linha, "coluna": campo.coluna, "valor": texto, "erro": "Formato numérico inválido (registrado como inválido)"})
            conceito = campo.conceito
            if campo.parcela == "TOTAL" and conceito_linha:
                conceito = conceito_linha
            linha = Linha(codigo, exercicio, campo.campo, campo.parcela, conceito, texto, valor, estado, procedencia)
            chave = (codigo, exercicio, campo.campo)
            if chave in vistos:
                primeira = vistos[chave]
                linha.duplicado = "IGUAL" if (primeira.valor, primeira.estado) == (valor, estado) else "CONFLITANTE"
                erros.append({
                    "linha": numero_linha, "coluna": campo.coluna, "valor": texto,
                    "erro": f"Duplicado no arquivo (primeira ocorrência na linha {primeira.procedencia['linha']}; {'mesmo valor' if linha.duplicado == 'IGUAL' else 'valor diferente'})",
                })
            else:
                vistos[chave] = linha
            linhas.append(linha)
    return linhas, erros


def importar(session: Session, planilha: Planilha, mapa: Mapeamento, usuario: str, pasta_arquivos: Path | None = None) -> dict:
    hash_conteudo = hashlib.sha256(planilha.conteudo + mapa.canonico().encode()).hexdigest()
    existente = session.scalar(select(cf.Lote).where(cf.Lote.hash_conteudo == hash_conteudo, cf.Lote.situacao == "CONCLUIDO"))
    if existente is not None:
        return {
            "lote": existente.id, "situacao": "JA_IMPORTADO", "novos": 0, "repetidos": 0, "novas_versoes": 0,
            "erros": [], "mensagem": f"Este conteúdo já foi importado no lote {existente.id} em {existente.data_importacao:%d/%m/%Y %H:%M}. Nada foi alterado.",
        }
    linhas, erros = interpretar(session, planilha, mapa)
    mapeamentos = _garantir_mapeamentos(session, mapa)
    lote = cf.Lote(
        declaracao_id=mapa.declaracao, origem_tipo="ARQUIVO", arquivo=planilha.nome_arquivo, aba=mapa.aba,
        parametros=mapa.canonico(), hash_conteudo=hash_conteudo, data_extracao=mapa.data_extracao,
        data_importacao=datetime.now().replace(microsecond=0), usuario=usuario, situacao="EM_ANDAMENTO",
        ficticio=mapa.ficticio, observacao=mapa.observacao,
    )
    session.add(lote)
    session.flush()
    contagem = persistir(session, lote, linhas, mapa.componente, mapa.periodo, mapa.data_extracao, mapeamentos)
    novos, repetidos, versoes = contagem["novos"], contagem["repetidos"], contagem["novas_versoes"]
    lote.erros = len(erros)
    lote.relatorio = json.dumps({"erros": erros[:5000]}, ensure_ascii=False)
    lote.situacao = "CONCLUIDO"
    if pasta_arquivos is not None:
        pasta_arquivos.mkdir(parents=True, exist_ok=True)
        destino = pasta_arquivos / f"lote-{lote.id}-{planilha.hash[:12]}{Path(planilha.nome_arquivo).suffix.lower()}"
        destino.write_bytes(planilha.conteudo)
        lote.endpoint = None
        lote.parametros = json.dumps({**json.loads(mapa.canonico()), "copia_local": str(destino.name)}, ensure_ascii=False)
    session.flush()
    return {
        "lote": lote.id, "situacao": "IMPORTADO", "novos": novos, "repetidos": repetidos, "novas_versoes": versoes,
        "erros": erros, "lidos": len(linhas),
        "mensagem": f"Lote {lote.id}: {novos} registros novos, {versoes} novas versões (retificações), {repetidos} iguais aos vigentes, {len(erros)} ocorrências no relatório.",
    }


def persistir(session: Session, lote: cf.Lote, linhas: list[Linha], componente: str, periodo: str,
              data_extracao, mapeamentos: dict[str, int]) -> dict:
    """Grava as linhas no lote com as regras de versão.

    Igual ao vigente: não grava (repetido). Diferente: nova versão e a anterior deixa de
    ser vigente. Inexistente: versão 1. Duplicados no mesmo arquivo: preservados, não vigentes.
    """
    declaracao = lote.declaracao_id
    lote.lidos = len(linhas)
    campos = sorted({linha.campo for linha in linhas})
    vigentes = {}
    if campos:
        for registro in session.scalars(
            select(cf.Registro).where(
                cf.Registro.declaracao_id == declaracao, cf.Registro.campo.in_(campos), cf.Registro.vigente.is_(True)
            )
        ):
            vigentes[(registro.id_ente, registro.exercicio, registro.campo)] = registro
    novos = repetidos = versoes = 0
    inserir = []
    substituidos = []
    for linha in linhas:
        periodo_linha = linha.periodo or periodo
        chave = (linha.id_ente, linha.exercicio, linha.campo)
        base = {
            "lote_id": lote.id, "declaracao_id": declaracao, "id_ente": linha.id_ente, "exercicio": linha.exercicio,
            "campo": linha.campo, "componente_id": linha.componente or componente, "parcela": linha.parcela,
            "conceito": linha.conceito, "mapeamento_id": mapeamentos.get(linha.campo), "valor_texto": (linha.valor_texto or "")[:80],
            "valor_centavos": linha.valor, "estado": linha.estado, "periodo": periodo_linha,
            "procedencia": json.dumps(linha.procedencia, ensure_ascii=False), "data_extracao": data_extracao,
            "duplicado": linha.duplicado, "substitui_id": None,
        }
        if linha.duplicado:
            inserir.append({**base, "versao": 0, "vigente": False})
            continue
        atual = vigentes.get(chave)
        if atual is not None and (atual.valor_centavos, atual.estado, atual.periodo) == (linha.valor, linha.estado, periodo_linha):
            repetidos += 1
            continue
        if atual is not None:
            substituidos.append(atual.id)
            inserir.append({**base, "versao": atual.versao + 1, "vigente": True, "substitui_id": atual.id})
            versoes += 1
        else:
            inserir.append({**base, "versao": 1, "vigente": True})
            novos += 1
    for inicio in range(0, len(substituidos), 500):
        session.execute(update(cf.Registro).where(cf.Registro.id.in_(substituidos[inicio:inicio + 500])).values(vigente=False))
    for inicio in range(0, len(inserir), 2000):
        session.execute(insert(cf.Registro), inserir[inicio:inicio + 2000])
    lote.novos = novos
    lote.repetidos = repetidos
    lote.novas_versoes = versoes
    return {"novos": novos, "repetidos": repetidos, "novas_versoes": versoes}


def _garantir_mapeamentos(session: Session, mapa: Mapeamento) -> dict[str, int]:
    resultado = {}
    for campo in mapa.campos:
        atual = session.scalar(
            select(cf.Mapeamento)
            .where(cf.Mapeamento.declaracao_id == mapa.declaracao, cf.Mapeamento.campo == campo.campo)
            .order_by(cf.Mapeamento.versao.desc())
            .limit(1)
        )
        if atual is not None and (atual.parcela, atual.conceito, atual.componente_id) == (campo.parcela, campo.conceito, mapa.componente):
            resultado[campo.campo] = atual.id
            continue
        novo = cf.Mapeamento(
            declaracao_id=mapa.declaracao, campo=campo.campo, componente_id=mapa.componente,
            exercicio_inicio=2019, exercicio_fim=2026, parcela=campo.parcela, conceito=campo.conceito,
            origem_detalhe=f"Importação: coluna '{campo.coluna}' do arquivo {mapa.aba}",
            abrangencia="{}", status="HIPOTESE", evidencia="Definido pelo usuário na importação.",
            versao=1 if atual is None else atual.versao + 1,
        )
        session.add(novo)
        session.flush()
        resultado[campo.campo] = novo.id
    return resultado
