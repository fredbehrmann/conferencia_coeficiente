import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from validacao.catalogo import FONTES
from validacao.decisoes import consultar_fila, consultar_revisao
from validacao.modelos import AgendaFonte, Carga, CargaEvento, Decisao, Ente, Verificacao
from validacao.regras import garantir_regra

INTERVALO_INICIAL = 30


def registrar_evento_engine(engine, fonte_id: str | None, situacao: str, mensagem: str) -> None:
    with Session(engine) as session:
        registrar_evento(session, fonte_id, situacao, mensagem)
        session.commit()


def registrar_evento(session: Session, fonte_id: str | None, situacao: str, mensagem: str) -> None:
    session.add(
        CargaEvento(
            quando=datetime.now().replace(microsecond=0),
            fonte_id=fonte_id,
            situacao=situacao,
            mensagem=mensagem[:1000],
        )
    )


def garantir_agenda(session: Session) -> None:
    existentes = set(session.scalars(select(AgendaFonte.fonte_id)))
    for fonte in FONTES:
        if fonte.id not in existentes:
            session.add(AgendaFonte(fonte_id=fonte.id, intervalo_dias=INTERVALO_INICIAL))


def definir_intervalo(session: Session, fonte_id: str, intervalo_dias: int) -> None:
    garantir_agenda(session)
    agenda = session.get(AgendaFonte, fonte_id)
    if agenda is None:
        raise ValueError("Fonte sem agenda.")
    agenda.intervalo_dias = int(intervalo_dias)


def copiar_banco(origem: Path, manter: int = 2) -> Path:
    """Cópia de segurança consistente na pasta backup/ ao lado do banco; guarda só as `manter` mais recentes."""
    destino_dir = Path(origem).parent / "backup"
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"validacao-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    from validacao.db import copiar_sqlite

    copiar_sqlite(origem, destino)
    antigas = sorted(destino_dir.glob("validacao-*.sqlite"), key=lambda p: p.name, reverse=True)[max(1, manter):]
    for arquivo in antigas:
        try:
            arquivo.unlink()
        except OSError:
            pass
    return destino


def copia_nuvem(banco: Path, pasta=None, intervalo_horas: float = 24, progresso=print) -> Path | None:
    """Cópia compactada do banco local numa pasta sincronizada (OneDrive), no máximo uma vez por intervalo.

    Substitui o envio do banco inteiro a cada validação por um arquivo .zip diário.
    Não faz nada quando o próprio banco já está numa pasta sincronizada.
    """
    import tempfile
    import time
    import zipfile

    from validacao.db import PASTA_COPIA_NUVEM, em_pasta_sincronizada

    banco = Path(banco)
    pasta = Path(pasta) if pasta is not None else PASTA_COPIA_NUVEM
    if em_pasta_sincronizada(banco) or not banco.exists():
        return None
    destino = pasta / "validacao-ultima.sqlite.zip"
    if destino.exists() and time.time() - destino.stat().st_mtime < intervalo_horas * 3600:
        return None
    pasta.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=banco.parent) as temporaria:
        copia = Path(temporaria) / "validacao.sqlite"
        from validacao.db import copiar_sqlite

        copiar_sqlite(banco, copia)
        parcial = destino.with_suffix(".parcial")
        with zipfile.ZipFile(parcial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zip_:
            zip_.write(copia, "validacao.sqlite")
        parcial.replace(destino)
    progresso(f"Cópia compactada gravada em {destino}")
    return destino


def painel(session: Session) -> dict:
    garantir_agenda(session)
    regra = garantir_regra(session)
    aplicavel = and_(
        Verificacao.regra_versao_id == regra.id,
        ~Verificacao.situacoes.contains('"NAO_SE_APLICA"'),
    )
    totais = session.execute(
        select(Ente.esfera, Ente.uf, Verificacao.ano, func.count())
        .join(Ente, Ente.id_ente == Verificacao.id_ente)
        .where(aplicavel)
        .group_by(Ente.esfera, Ente.uf, Verificacao.ano)
    ).all()
    decisoes = {
        (esfera, uf, ano): quantidade
        for esfera, uf, ano, quantidade in session.execute(
            select(Ente.esfera, Ente.uf, Decisao.ano, func.count())
            .join(Ente, Ente.id_ente == Decisao.id_ente)
            .join(
                Verificacao,
                and_(
                    Verificacao.id_ente == Decisao.id_ente,
                    Verificacao.ano == Decisao.ano,
                    Verificacao.bloco == Decisao.bloco,
                    aplicavel,
                ),
            )
            .where(Decisao.estado == "VIGENTE")
            .group_by(Ente.esfera, Ente.uf, Decisao.ano)
        ).all()
    }
    grupos = {
        (esfera, uf, ano): [total, decisoes.get((esfera, uf, ano), 0)]
        for esfera, uf, ano, total in totais
    }
    proporcoes = [
        {
            "esfera": esfera,
            "uf": uf,
            "ano": ano,
            "total": total,
            "decididos": decididos,
            "proporcao": 0 if total == 0 else decididos / total,
        }
        for (esfera, uf, ano), (total, decididos) in sorted(grupos.items())
    ]
    por_uf: dict[tuple[str, str], dict] = {}
    for item in proporcoes:
        grupo = por_uf.setdefault(
            (item["esfera"], item["uf"]),
            {"esfera": item["esfera"], "uf": item["uf"], "total": 0, "decididos": 0, "anos": []},
        )
        grupo["total"] += item["total"]
        grupo["decididos"] += item["decididos"]
        grupo["anos"].append(item)
    andamento = list(por_uf.values())
    for grupo in andamento:
        grupo["proporcao"] = 0 if grupo["total"] == 0 else grupo["decididos"] / grupo["total"]
        grupo["anos"].sort(key=lambda item: item["ano"])
    andamento.sort(key=lambda grupo: (grupo["proporcao"], grupo["uf"], grupo["esfera"]))
    _fila, pendentes = consultar_fila(session, regra.id, pagina=1)
    _revisao, sem_revisao = consultar_revisao(session, pagina=1)
    cargas = _cargas(session)
    return {
        "proporcoes": proporcoes,
        "por_uf": andamento,
        "pendentes": pendentes,
        "sem_revisao": sem_revisao,
        "atrasadas": sum(1 for carga in cargas if carga["atrasada"]),
        "cargas": cargas,
    }


def _cargas(session: Session) -> list[dict]:
    agora = datetime.now().replace(microsecond=0)
    agendas = {agenda.fonte_id: agenda.intervalo_dias for agenda in session.scalars(select(AgendaFonte))}
    ultimas = dict(
        session.execute(
            select(Carga.fonte_id, func.max(Carga.data_carga))
            .where(Carga.situacao == "CONCLUIDA")
            .group_by(Carga.fonte_id)
        ).all()
    )
    erros = {}
    for evento in session.scalars(select(CargaEvento).where(CargaEvento.situacao == "ERRO").order_by(CargaEvento.id)):
        erros[evento.fonte_id] = evento
    linhas = []
    for fonte in FONTES:
        ultima = ultimas.get(fonte.id)
        intervalo = agendas.get(fonte.id, INTERVALO_INICIAL)
        erro = erros.get(fonte.id)
        atrasada = ultima is None or (agora - ultima).days > intervalo
        if erro is not None and (ultima is None or erro.quando >= ultima):
            atrasada = True
        linhas.append(
            {
                "fonte": fonte.id,
                "nome": fonte.nome,
                "ultima": ultima,
                "intervalo_dias": intervalo,
                "atrasada": atrasada,
                "erro": None if erro is None else erro.mensagem,
            }
        )
    return linhas
