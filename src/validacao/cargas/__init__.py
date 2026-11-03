from pathlib import Path

from sqlalchemy.orm import Session

from validacao.cadastro import mapa_de_codigos
from validacao.cargas import siconfi, siope, siops, tce
from validacao.catalogo import semear

ARQUIVOS = (
    (siconfi, siconfi.ARQUIVO),
    (siops, siops.ARQUIVO),
    (siope, siope.ARQUIVO),
    (tce, tce.ARQUIVO),
)


def carregar_pasta(engine, pasta: Path) -> list[dict]:
    pasta = Path(pasta)
    with Session(engine) as session:
        semear(session)
        session.commit()

    resultados = []
    for modulo, nome in ARQUIVOS:
        caminho = pasta / nome
        if not caminho.is_file():
            raise FileNotFoundError(caminho)
        with Session(engine) as session:
            with session.begin():
                resultado = modulo.carregar(session, caminho)
        print(f"{resultado['fonte']}: {resultado['situacao']} ({resultado['valores']} valores)", flush=True)
        resultados.append(resultado)
    return resultados


def codigos_ligados(engine, fonte_id: str) -> dict[str, int]:
    with Session(engine) as session:
        return mapa_de_codigos(session, fonte_id)
