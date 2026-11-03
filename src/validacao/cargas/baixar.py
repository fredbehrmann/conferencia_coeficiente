import json
import urllib.request
from pathlib import Path

AGENTE = "validacao-coeficiente"


def obter_json(url: str, destino: Path | None = None):
    if destino is not None and destino.is_file():
        return json.loads(destino.read_text(encoding="utf-8"))
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE, "Accept": "application/json"})
    with urllib.request.urlopen(pedido, timeout=120) as resposta:
        conteudo = resposta.read()
    if destino is not None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(conteudo)
    return json.loads(conteudo.decode("utf-8"))


def obter_arquivo(url: str, destino: Path) -> Path:
    if destino.is_file() and destino.stat().st_size > 0:
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(pedido, timeout=180) as resposta, destino.open("wb") as saida:
        while True:
            bloco = resposta.read(256 * 1024)
            if not bloco:
                break
            saida.write(bloco)
    return destino


def paginas(url: str) -> list[dict]:
    itens = []
    offset = 0
    while True:
        separador = "&" if "?" in url else "?"
        pagina = obter_json(f"{url}{separador}offset={offset}")
        lote = pagina.get("items") or []
        itens.extend(lote)
        if not pagina.get("hasMore") or not lote:
            break
        offset += len(lote)
    return itens
