from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from validacao.modelos import Ente, EnteCodigoExterno

UF_POR_CODIGO = {
    11: "RO",
    12: "AC",
    13: "AM",
    14: "RR",
    15: "PA",
    16: "AP",
    17: "TO",
    21: "MA",
    22: "PI",
    23: "CE",
    24: "RN",
    25: "PB",
    26: "PE",
    27: "AL",
    28: "SE",
    29: "BA",
    31: "MG",
    32: "ES",
    33: "RJ",
    35: "SP",
    41: "PR",
    42: "SC",
    43: "RS",
    50: "MS",
    51: "MT",
    52: "GO",
    53: "DF",
}

CAPITAIS = {
    1200401,
    2704302,
    1302603,
    1600303,
    2927408,
    2304400,
    3205309,
    5208707,
    2111300,
    3106200,
    5002704,
    5103403,
    1501402,
    2507507,
    2611606,
    2211001,
    4106902,
    3304557,
    2408102,
    1100205,
    1400100,
    4314902,
    4205407,
    2800308,
    3550308,
    1721000,
}

TRIBUNAL_DA_CAPITAL = {
    3550308: "TCM-SP",
    3304557: "TCM-RJ",
    2927408: "TCM-BA",
}


def esfera_de(texto: str) -> str:
    normalizado = str(texto).strip().upper()
    if normalizado == "DF":
        return "DF"
    if normalizado.startswith("EST"):
        return "ESTADO"
    if normalizado.startswith("MUN"):
        return "MUNICIPIO"
    raise ValueError(f"Esfera desconhecida: {texto}")


def uf_de(esfera: str, id_ente: int) -> str:
    codigo = id_ente // 100000 if esfera == "MUNICIPIO" else id_ente
    try:
        return UF_POR_CODIGO[codigo]
    except KeyError as erro:
        raise ValueError(f"UF desconhecida para o ente {id_ente}") from erro


def tribunal_de(esfera: str, uf: str, id_ente: int) -> str:
    if esfera == "DF":
        return "TCDF"
    if esfera == "ESTADO":
        return f"TCE-{uf}"
    return TRIBUNAL_DA_CAPITAL.get(id_ente, f"TCE-{uf}")


def pares_de_codigo(ente: Ente) -> list[tuple[str, str]]:
    identificador = str(ente.id_ente)
    if ente.esfera == "MUNICIPIO":
        seis = str(ente.id_ente // 10)
        return [
            ("SICONFI", identificador),
            ("SIOPS", seis),
            ("SIOPE", seis),
            ("TCE", identificador),
        ]
    if ente.esfera == "DF":
        return [
            ("SICONFI", identificador),
            ("SIOPS", "53"),
            ("SIOPS", "530010"),
            ("SIOPE", "53"),
            ("SIOPE", "530010"),
        ]
    return [
        ("SICONFI", identificador),
        ("SIOPS", identificador),
        ("SIOPE", identificador),
    ]


def registrar_codigos(session: Session, entes: list[Ente]) -> None:
    existentes = set(
        session.execute(
            select(EnteCodigoExterno.fonte_id, EnteCodigoExterno.codigo_externo)
        ).all()
    )
    novos = []
    for ente in entes:
        for fonte_id, codigo in pares_de_codigo(ente):
            chave = (fonte_id, codigo)
            if chave in existentes:
                continue
            existentes.add(chave)
            novos.append(
                {
                    "fonte_id": fonte_id,
                    "codigo_externo": codigo,
                    "id_ente": ente.id_ente,
                }
            )
    for inicio in range(0, len(novos), 2000):
        session.execute(insert(EnteCodigoExterno), novos[inicio : inicio + 2000])


def garantir_municipio(session: Session, id_ente: int, nome: str, conhecidos: set[int]) -> None:
    if id_ente in conhecidos:
        return
    uf = uf_de("MUNICIPIO", id_ente)
    ente = Ente(
        id_ente=id_ente,
        esfera="MUNICIPIO",
        uf=uf,
        nome=nome or f"Município {id_ente}",
        capital=id_ente in CAPITAIS,
        tribunal_competente=tribunal_de("MUNICIPIO", uf, id_ente),
    )
    session.add(ente)
    session.flush()
    registrar_codigos(session, [ente])
    conhecidos.add(id_ente)


def existia_no_ano(ano: int, instalacao: int | None, extincao: int | None) -> bool:
    if instalacao is not None and ano < instalacao:
        return False
    if extincao is not None and ano > extincao:
        return False
    return True


def aplicar_anos(session: Session, anos: dict[int, tuple[int | None, int | None]]) -> int:
    atualizados = 0
    for id_ente, (instalacao, extincao) in anos.items():
        ente = session.get(Ente, id_ente)
        if ente is None:
            continue
        ente.ano_instalacao = instalacao
        ente.ano_extincao = extincao
        atualizados += 1
    return atualizados


def mapa_de_codigos(session: Session, fonte_id: str) -> dict[str, int]:
    linhas = session.execute(
        select(EnteCodigoExterno.codigo_externo, EnteCodigoExterno.id_ente).where(
            EnteCodigoExterno.fonte_id == fonte_id
        )
    ).all()
    return {codigo: id_ente for codigo, id_ente in linhas}
