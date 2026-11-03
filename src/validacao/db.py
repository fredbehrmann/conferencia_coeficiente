"""Localização do banco e configuração das conexões SQLite.

Os dados ficam numa pasta local do usuário (fora do OneDrive): no Windows,
%LOCALAPPDATA%\\ConferenciaCoeficiente. A variável de ambiente CONFERENCIA_DADOS
escolhe outra pasta. Na primeira execução, o banco que estava em `data/` (pasta
sincronizada) é copiado para a pasta local; o original fica onde estava.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from validacao.modelos import Base

RAIZ = Path(__file__).resolve().parents[2]
PASTA_DADOS_LEGADO = RAIZ / "data"
# Pasta com as planilhas e o pacote do SICONFI (no Docker: volume /bases, via CONFERENCIA_BASES).
PASTA_PLANILHAS = Path(os.environ["CONFERENCIA_BASES"]) if os.environ.get("CONFERENCIA_BASES") else RAIZ.parent
PASTA_RELATORIOS = Path(os.environ["CONFERENCIA_RELATORIOS"]) if os.environ.get("CONFERENCIA_RELATORIOS") else RAIZ / "relatorios"
PASTA_FONTES = RAIZ / "fontes"
PASTA_COPIA_NUVEM = PASTA_DADOS_LEGADO / "copia_nuvem"

SINCRONIZADAS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")

# Ajustes medidos no diagnóstico de performance (soma das telas −16%).
PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA cache_size=-131072",      # 128 MB de cache de páginas por conexão
    "PRAGMA mmap_size=1073741824",    # leitura por mapeamento de memória (até 1 GB)
    "PRAGMA temp_store=MEMORY",       # ordenações e agrupamentos temporários em memória
    "PRAGMA busy_timeout=30000",      # espera até 30 s por uma escrita em andamento
)


def _pasta_dados_padrao() -> Path:
    escolhida = os.environ.get("CONFERENCIA_DADOS")
    if escolhida:
        return Path(escolhida)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "ConferenciaCoeficiente"
    return PASTA_DADOS_LEGADO


PASTA_DADOS = _pasta_dados_padrao()
BANCO_PADRAO = PASTA_DADOS / "validacao.sqlite"
BANCO_LEGADO = PASTA_DADOS_LEGADO / "validacao.sqlite"


def sem_acento(texto):
    """Minúsculas sem acento (também registrada como função SQL `sem_acento` em cada conexão)."""
    if texto is None:
        return None
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn").lower()


def em_pasta_sincronizada(caminho: Path) -> bool:
    partes = [p.lower() for p in Path(caminho).resolve().parts]
    return any(p.startswith(s) for p in partes for s in SINCRONIZADAS)


def criar_engine(banco: Path | None = None) -> Engine:
    destino = Path(banco) if banco is not None else BANCO_PADRAO
    destino.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{destino}", connect_args={"timeout": 30})
    # WAL deixa as telas lerem enquanto a validação grava. Nunca numa pasta sincronizada:
    # o OneDrive pode copiar os arquivos -wal/-shm no meio de uma gravação.
    usar_wal = not em_pasta_sincronizada(destino) and os.environ.get("CONFERENCIA_SEM_WAL") != "1"

    @event.listens_for(engine, "connect")
    def _configurar(conexao, _registro):
        conexao.create_function("sem_acento", 1, sem_acento, deterministic=True)
        cursor = conexao.cursor()
        for pragma in PRAGMAS:
            cursor.execute(pragma)
        if usar_wal:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def criar_tabelas(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@contextmanager
def conectar(caminho):
    """Conexão sqlite3 que FECHA o arquivo ao sair.

    `with sqlite3.connect(...)` só confirma a transação e deixa o arquivo aberto; no Windows
    isso impede renomear ou apagar o arquivo logo depois (WinError 32).
    """
    conexao = sqlite3.connect(caminho)
    try:
        with conexao:
            yield conexao
    finally:
        conexao.close()


def copiar_sqlite(origem: Path, destino: Path) -> None:
    """Cópia consistente (API de backup do SQLite), segura mesmo com o banco em uso."""
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    with conectar(origem) as fonte, conectar(destino) as copia:
        fonte.backup(copia)


def _com_espera(acao, tentativas: int = 60, intervalo: float = 0.5):
    """Repete a operação de arquivo enquanto o Windows a recusar por arquivo em uso.

    Logo depois de gravado, um arquivo grande costuma ficar alguns segundos aberto pelo antivírus
    ou pelo indexador, o que também gera o WinError 32.
    """
    import time

    for tentativa in range(tentativas):
        try:
            return acao()
        except PermissionError:
            if tentativa == tentativas - 1:
                raise
            time.sleep(intervalo)


def mover_para_pasta_local(progresso=print) -> Path | None:
    """Copia o banco da pasta sincronizada para a pasta local, uma única vez.

    Copia também o banco de detalhes do SICONFI, o segredo de sessão e os arquivos importados,
    e deixa um aviso na pasta antiga. Nada é apagado. Devolve o novo caminho, ou None se não
    havia o que copiar.
    """
    if BANCO_PADRAO.resolve() == BANCO_LEGADO.resolve() or BANCO_PADRAO.exists() or not BANCO_LEGADO.exists():
        return None
    progresso(f"Copiando o banco para a pasta local {PASTA_DADOS} (uma única vez; o original fica em {PASTA_DADOS_LEGADO})...")
    temporario = BANCO_PADRAO.with_suffix(".copiando")
    if temporario.exists():  # sobra de uma tentativa interrompida
        _com_espera(temporario.unlink)
    copiar_sqlite(BANCO_LEGADO, temporario)
    _com_espera(lambda: temporario.replace(BANCO_PADRAO))
    detalhes = PASTA_DADOS_LEGADO / "detalhes_siconfi.sqlite"
    if detalhes.exists():
        copiar_sqlite(detalhes, PASTA_DADOS / "detalhes_siconfi.sqlite")
    segredo = PASTA_DADOS_LEGADO / ".segredo_sessao"
    if segredo.exists():
        shutil.copy2(segredo, PASTA_DADOS / ".segredo_sessao")
    importacoes = PASTA_DADOS_LEGADO / "importacoes"
    if importacoes.is_dir() and not (PASTA_DADOS / "importacoes").exists():
        shutil.copytree(importacoes, PASTA_DADOS / "importacoes")
    with conectar(BANCO_PADRAO) as conexao:
        if conexao.execute("SELECT name FROM sqlite_master WHERE name = 'cf_configuracao'").fetchone():
            conexao.execute("UPDATE cf_configuracao SET valor = ? WHERE chave = 'detalhes_siconfi'",
                            (json.dumps(str(PASTA_DADOS / "detalhes_siconfi.sqlite")),))
    aviso = PASTA_DADOS_LEGADO / "LEIA-ME - banco movido.txt"
    aviso.write_text(
        f"Em {datetime.now():%d/%m/%Y %H:%M} o banco da conferência passou a ficar em\n{PASTA_DADOS}\n\n"
        "Motivo: o banco é reescrito a cada validação e o OneDrive voltava a enviar o arquivo inteiro.\n"
        "Os arquivos desta pasta (validacao.sqlite e backup\\) não são mais usados pela aplicação e podem ser apagados\n"
        "quando você quiser. Uma cópia compactada diária do banco local é gravada em copia_nuvem\\.\n",
        encoding="utf-8",
    )
    progresso(f"Banco copiado para {BANCO_PADRAO}.")
    return BANCO_PADRAO
