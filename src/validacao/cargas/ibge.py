import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from validacao.cadastro import aplicar_anos
from validacao.cargas.baixar import obter_json
from validacao.modelos import CargaEvento

URL_MUNICIPIOS = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"

# A API de localidades confirma o código e não traz o ano. O IBGE publica,
# na página Cidades e Estados, a instalação de Boa Esperança do Norte em 01/01/2025.
ANOS_PUBLICADOS = {
    5101837: (2025, None),
}


def anos_do_ibge(pasta: Path | None = None) -> dict[int, tuple[int | None, int | None]]:
    anos = dict(ANOS_PUBLICADOS)
    if pasta is None:
        return anos
    arquivo = pasta / "ibge_instalacao.csv"
    if not arquivo.is_file():
        return anos
    with arquivo.open(encoding="utf-8", newline="") as fonte:
        for linha in csv.DictReader(fonte):
            if not linha.get("id_ente"):
                continue
            anos[int(linha["id_ente"])] = (
                _ano(linha.get("ano_instalacao")),
                _ano(linha.get("ano_extincao")),
            )
    return anos


def confirmar_codigos(pasta: Path) -> set[int]:
    payload = obter_json(URL_MUNICIPIOS, pasta / "ibge_municipios.json")
    return {int(municipio["id"]) for municipio in payload}


def atualizar_instalacao(session: Session, pasta: Path) -> int:
    try:
        codigos = confirmar_codigos(pasta)
    except OSError as erro:
        session.add(
            CargaEvento(
                quando=datetime.now().replace(microsecond=0),
                fonte_id=None,
                situacao="ERRO",
                mensagem=f"IBGE: {erro}"[:1000],
            )
        )
        codigos = None
    anos = anos_do_ibge(pasta)
    if codigos is not None:
        anos = {
            ente: par
            for ente, par in anos.items()
            if ente in codigos or ente < 100 or ente in ANOS_PUBLICADOS
        }
    return aplicar_anos(session, anos)


def _ano(texto: str | None) -> int | None:
    if texto is None or str(texto).strip() == "":
        return None
    return int(texto)
