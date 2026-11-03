"""Manutenção do banco: estatísticas do planejador, compactação e arquivamento das tabelas da versão 1."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from validacao.db import conectar

LIMITE_LIVRE = 0.20  # compacta quando mais de 20% das páginas estão livres
TABELAS_V1 = ("verificacao", "valor_observado")


def _numeros(engine: Engine) -> dict:
    with engine.connect() as conexao:
        paginas = conexao.execute(text("PRAGMA page_count")).scalar() or 0
        livres = conexao.execute(text("PRAGMA freelist_count")).scalar() or 0
        tamanho = conexao.execute(text("PRAGMA page_size")).scalar() or 4096
    return {"paginas": paginas, "livres": livres, "mb": round(paginas * tamanho / 1048576, 1),
            "mb_livres": round(livres * tamanho / 1048576, 1)}


def _vacuum(engine: Engine) -> None:
    with engine.connect() as conexao:
        conexao.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        conexao.exec_driver_sql("VACUUM")
        conexao.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")


def compactar_se_preciso(engine: Engine, progresso=print, limite: float = LIMITE_LIVRE) -> dict | None:
    antes = _numeros(engine)
    if not antes["paginas"] or antes["livres"] / antes["paginas"] <= limite:
        return None
    progresso(f"Compactando o banco ({antes['mb_livres']} MB livres de {antes['mb']} MB)...")
    _vacuum(engine)
    return {"antes": antes, "depois": _numeros(engine)}


def manter(engine: Engine, progresso=print, vacuum: bool = True) -> dict:
    antes = _numeros(engine)
    progresso("Atualizando as estatísticas do planejador (ANALYZE)...")
    with engine.connect() as conexao:
        conexao.exec_driver_sql("ANALYZE")
        conexao.commit()
    if vacuum:
        progresso(f"Compactando o banco ({antes['mb']} MB, {antes['mb_livres']} MB livres)...")
        _vacuum(engine)
    depois = _numeros(engine)
    progresso(f"Banco: {antes['mb']} MB → {depois['mb']} MB.")
    return {"antes": antes, "depois": depois}


def arquivar_v1(banco: Path, progresso=print) -> Path | None:
    """Copia as tabelas grandes da versão 1 para um arquivo à parte e as remove do banco principal.

    As telas da conferência não usam essas tabelas; a tela antiga (servir-v1) passa a mostrar
    o histórico vazio. O arquivo gerado guarda as linhas, para consulta ou restauração manual.
    """
    banco = Path(banco)
    destino = banco.parent / f"arquivo_v1_{datetime.now():%Y%m%d}.sqlite"
    with conectar(banco) as conexao:
        existentes = [t for (t,) in conexao.execute("SELECT name FROM sqlite_master WHERE type='table'") if t in TABELAS_V1]
        if not existentes:
            progresso("As tabelas da versão 1 já foram arquivadas.")
            return None
        conexao.execute("ATTACH DATABASE ? AS arquivo", (str(destino),))
        for tabela in existentes:
            sql = conexao.execute("SELECT sql FROM sqlite_master WHERE name = ?", (tabela,)).fetchone()[0]
            conexao.execute(sql.replace(f"CREATE TABLE {tabela}", f"CREATE TABLE IF NOT EXISTS arquivo.{tabela}", 1))
            conexao.execute(f"INSERT INTO arquivo.{tabela} SELECT * FROM main.{tabela}")
            copiadas = conexao.execute(f"SELECT count(*) FROM arquivo.{tabela}").fetchone()[0]
            originais = conexao.execute(f"SELECT count(*) FROM main.{tabela}").fetchone()[0]
            if copiadas != originais:
                raise RuntimeError(f"Cópia incompleta de {tabela}: {copiadas} de {originais} linhas. Nada foi removido.")
            progresso(f"{tabela}: {originais:,} linhas copiadas para {destino.name}".replace(",", "."))
        conexao.commit()
        conexao.execute("DETACH DATABASE arquivo")
        for tabela in existentes:
            conexao.execute(f"DELETE FROM {tabela}")
        conexao.commit()
    return destino
