import hashlib
import io
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from validacao.modelos import Carga, ValorObservado

USUARIO_CARGA = "carga_local"
LOTE = 2000
_COMPARTILHADOS: dict[str, bytes] = {}


def hash_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    try:
        with caminho.open("rb") as arquivo:
            for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
                digest.update(bloco)
        return digest.hexdigest()
    except PermissionError:
        digest.update(_ler_compartilhado(caminho))
        return digest.hexdigest()


def ler_planilha(caminho: Path, **kwargs):
    try:
        return pd.read_excel(caminho, **kwargs)
    except PermissionError:
        return pd.read_excel(io.BytesIO(_ler_compartilhado(caminho)), **kwargs)


def _ler_compartilhado(caminho: Path) -> bytes:
    chave = str(Path(caminho).resolve())
    if chave in _COMPARTILHADOS:
        return _COMPARTILHADOS[chave]
    dados = _ler_windows(Path(chave))
    _COMPARTILHADOS[chave] = dados
    return dados


def _ler_windows(caminho: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(caminho), 0x80000000, 1 | 2 | 4, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise PermissionError(caminho)
    try:
        partes = []
        buffer = ctypes.create_string_buffer(1024 * 1024)
        lido = wintypes.DWORD()
        while kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(lido), None) and lido.value:
            partes.append(buffer.raw[: lido.value])
        return b"".join(partes)
    finally:
        kernel.CloseHandle(handle)


def data_do_arquivo(caminho: Path) -> datetime:
    return datetime.fromtimestamp(caminho.stat().st_mtime).replace(microsecond=0)


def carga_concluida(session: Session, hash_hex: str) -> Carga | None:
    return session.scalar(
        select(Carga).where(Carga.hash == hash_hex, Carga.situacao == "CONCLUIDA")
    )


def numero(valor):
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except TypeError:
        pass
    if isinstance(valor, str):
        texto = valor.strip()
        if texto == "" or texto.lower() == "nan":
            return None
        return float(texto.replace(",", "."))
    return float(valor)


def texto(valor) -> str | None:
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except TypeError:
        pass
    limpo = str(valor).strip()
    if limpo == "" or limpo.lower() == "nan":
        return None
    return limpo


def codigo_inteiro(valor) -> int | None:
    try:
        convertido = numero(valor)
    except (TypeError, ValueError):
        return None
    if convertido is None:
        return None
    return int(convertido)


def acrescentar(mapa, id_ente, ano, componente, valor, natureza, conta, linha, permitidos):
    if valor is None or componente not in permitidos:
        return
    chave = (id_ente, ano, componente)
    if chave in mapa:
        anterior = mapa[chave]["valor"]
        if abs(anterior - valor) > 0.01:
            raise ValueError(
                f"Valor divergente para o ente {id_ente}, ano {ano}, componente {componente}: "
                f"{anterior} e {valor}"
            )
        return
    mapa[chave] = {
        "valor": valor,
        "natureza": natureza,
        "conta": conta,
        "linha": linha,
    }


def gravar_valores(session: Session, carga_id: int, mapa: dict) -> int:
    linhas = [
        {
            "carga_id": carga_id,
            "id_ente": id_ente,
            "ano": ano,
            "componente_id": componente,
            "valor": info["valor"],
            "natureza": info["natureza"],
            "conta_origem": info["conta"],
            "linha_origem": info["linha"],
        }
        for (id_ente, ano, componente), info in mapa.items()
    ]
    for inicio in range(0, len(linhas), LOTE):
        session.execute(insert(ValorObservado), linhas[inicio : inicio + LOTE])
    return len(linhas)


def abrir_carga(session, fonte_id, caminho: Path, hash_hex: str, data_extracao, exercicios: set[int]) -> Carga:
    carga = Carga(
        fonte_id=fonte_id,
        arquivo_ou_endpoint=str(caminho),
        hash=hash_hex,
        data_extracao=data_extracao,
        data_carga=datetime.now().replace(microsecond=0),
        exercicios=",".join(str(ano) for ano in sorted(exercicios)),
        usuario=USUARIO_CARGA,
        situacao="CONCLUIDA",
    )
    session.add(carga)
    session.flush()
    return carga
