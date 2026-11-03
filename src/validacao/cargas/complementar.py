import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.cargas.baixar import obter_arquivo
from validacao.cargas.comum import abrir_carga, carga_concluida, gravar_valores, hash_arquivo
from validacao.cargas.ibge import atualizar_instalacao
from validacao.cargas.planilhas_publicas import (
    URL_CONFAZ,
    ler_anm,
    ler_anp,
    ler_confaz,
    ler_sefaz,
    ler_simples,
    ler_tesouro,
    ler_tribunal,
)
from validacao.cargas.siconfi_api import (
    agregar_msc,
    agregar_rreo,
    buscar_entrega,
    buscar_msc,
    buscar_rreo,
)
from validacao.catalogo import semear
from validacao.db import PASTA_FONTES
from validacao.operacao import registrar_evento_engine
from validacao.modelos import Carga, EntregaDeclaracao, ValorObservado, Verificacao
from validacao.regras import garantir_regra

PARES_SEM_ABERTURA = ("ISS", "COTA_RECEBIDA", "ICMS")


def carregar_complementares(
    engine,
    pasta: Path | None = None,
    limite: int | None = None,
    somente: list[tuple[int, int]] | None = None,
) -> None:
    destino = Path(pasta) if pasta is not None else PASTA_FONTES
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "tribunais").mkdir(exist_ok=True)
    (destino / "sefaz").mkdir(exist_ok=True)
    with Session(engine) as session:
        semear(session)
        session.commit()
        atualizados = atualizar_instalacao(session, destino)
        session.commit()
        print(f"IBGE: {atualizados} entes com ano de instalação ou extinção", flush=True)

    _arquivo_confaz(destino, engine)
    with Session(engine) as session:
        with session.begin():
            _gravar_fonte(session, "CONFAZ", destino / "confaz_boletim.xlsx", ler_confaz)
            for arquivo in _csvs(destino / "sefaz"):
                _gravar_mapa(session, "SEFAZ_UF", arquivo, ler_sefaz(arquivo))
            _gravar_mapa_se_existir(session, "TESOURO", destino / "tesouro_compensacoes.csv", ler_tesouro)
            _gravar_mapa_se_existir(session, "ANP", destino / "anp_royalties.csv", ler_anp)
            _gravar_mapa_se_existir(session, "ANM", destino / "anm_cfem.csv", ler_anm)
            _gravar_mapa_se_existir(session, "SIMPLES", destino / "iss_simples.csv", ler_simples)
            _juntar_tribunais(session, destino / "tribunais")

    with Session(engine) as session:
        pares = _pares_sem_abertura(session, limite)
        if somente is not None:
            pedido = {(int(ente), int(ano)) for ente, ano in somente}
            pares = [par for par in pares if par in pedido]
        session.commit()
    if not pares:
        print("MSC: nenhuma verificação com sem abertura. Rode verificar antes.", flush=True)
        return
    _coletar_apis(engine, destino, pares)


def _arquivo_confaz(pasta: Path, engine) -> None:
    destino = pasta / "confaz_boletim.xlsx"
    if destino.is_file():
        return
    try:
        obter_arquivo(URL_CONFAZ, destino)
        print(f"CONFAZ: baixado em {destino}", flush=True)
    except OSError as erro:
        print(f"CONFAZ: não baixou ({erro})", flush=True)
        registrar_evento_engine(engine, "CONFAZ", "ERRO", str(erro))


def _coletar_apis(engine, pasta: Path, pares: list[tuple[int, int]]) -> None:
    msc = pasta / "msc"
    rreo = pasta / "rreo"
    entrega = pasta / "entrega"
    for id_ente, ano in pares:
        _cache_json(msc / f"{id_ente}_{ano}.json", lambda: agregar_msc(buscar_msc(id_ente, ano), ano))
        _cache_json(rreo / f"{id_ente}_{ano}.json", lambda: agregar_rreo(buscar_rreo(id_ente, ano)))
        _cache_json(entrega / f"{id_ente}_{ano}.json", lambda: {"entregue": buscar_entrega(id_ente, ano)})
        print(f"API {id_ente} {ano}", flush=True)
    with Session(engine) as session:
        with session.begin():
            _gravar_caches(session, "MSC", msc)
            _gravar_caches(session, "RREO", rreo)
            _gravar_entregas(session, entrega)


def _cache_json(destino: Path, produzir) -> None:
    if destino.is_file():
        return
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(produzir(), ensure_ascii=False), encoding="utf-8")


def _gravar_caches(session: Session, fonte_id: str, pasta: Path) -> None:
    mapa: dict[tuple[int, int, str], float] = {}
    for arquivo in sorted(pasta.glob("*.json")):
        id_ente, ano = (int(parte) for parte in arquivo.stem.split("_"))
        itens = json.loads(arquivo.read_text(encoding="utf-8"))
        for componente, valor in itens.items():
            mapa[(id_ente, ano, componente)] = float(valor)
    if mapa:
        _gravar_mapa(session, fonte_id, pasta, mapa)


def _gravar_entregas(session: Session, pasta: Path) -> None:
    arquivos = sorted(pasta.glob("*.json"))
    if not arquivos:
        return
    resumo = _hash_arquivos(arquivos)
    if carga_concluida(session, resumo) is not None:
        print("ENTREGA: arquivo já carregado", flush=True)
        return
    carga = abrir_carga(session, "ENTREGA", pasta, resumo, None, {int(a.stem.split("_")[1]) for a in arquivos})
    for arquivo in arquivos:
        id_ente, ano = (int(parte) for parte in arquivo.stem.split("_"))
        entregue = bool(json.loads(arquivo.read_text(encoding="utf-8")).get("entregue"))
        existente = session.scalar(
            select(EntregaDeclaracao).where(
                EntregaDeclaracao.id_ente == id_ente,
                EntregaDeclaracao.ano == ano,
                EntregaDeclaracao.declaracao == "DCA",
            )
        )
        if existente is None:
            session.add(
                EntregaDeclaracao(
                    id_ente=id_ente,
                    ano=ano,
                    declaracao="DCA",
                    entregue=entregue,
                    carga_id=carga.id,
                )
            )
        else:
            existente.entregue = entregue
            existente.carga_id = carga.id
    print(f"ENTREGA: {len(arquivos)} exercícios", flush=True)


def _pares_sem_abertura(session: Session, limite: int | None) -> list[tuple[int, int]]:
    regra = garantir_regra(session)
    linhas = session.execute(
        select(Verificacao.id_ente, Verificacao.ano)
        .where(
            Verificacao.regra_versao_id == regra.id,
            Verificacao.bloco.in_(PARES_SEM_ABERTURA),
            Verificacao.situacoes.contains('"SEM_ABERTURA"'),
        )
        .distinct()
    ).all()
    pares = sorted({(int(ente), int(ano)) for ente, ano in linhas})
    if limite is not None:
        return pares[:limite]
    return pares


def _juntar_tribunais(session: Session, pasta: Path) -> None:
    arquivos = _csvs(pasta)
    if not arquivos:
        return
    novos: dict[tuple[int, int, str], tuple[float, str | None]] = {}
    for arquivo in arquivos:
        novos.update(ler_tribunal(arquivo))
    if not novos:
        return
    base = session.scalar(
        select(Carga)
        .where(Carga.fonte_id == "TCE", Carga.situacao == "CONCLUIDA")
        .order_by(Carga.id.desc())
        .limit(1)
    )
    digest = hashlib.sha256()
    if base is not None:
        digest.update(base.hash.encode())
    for arquivo in arquivos:
        digest.update(hash_arquivo(arquivo).encode())
    resumo = digest.hexdigest()
    if carga_concluida(session, resumo) is not None:
        print("TCE: tribunais já carregados", flush=True)
        return
    mapa: dict[tuple[int, int, str], dict] = {}
    if base is not None:
        linhas = session.scalars(select(ValorObservado).where(ValorObservado.carga_id == base.id))
        for linha in linhas:
            mapa[(linha.id_ente, linha.ano, linha.componente_id)] = {
                "valor": linha.valor,
                "natureza": linha.natureza,
                "conta": linha.conta_origem,
                "linha": linha.linha_origem,
            }
    for (id_ente, ano, componente), (valor, natureza) in novos.items():
        mapa[(id_ente, ano, componente)] = {
            "valor": valor,
            "natureza": natureza,
            "conta": "tribunal",
            "linha": None,
        }
    exercicios = {ano for (_ente, ano, _componente) in mapa}
    carga = abrir_carga(session, "TCE", pasta, resumo, None, exercicios)
    gravar_valores(session, carga.id, mapa)
    print(f"TCE: {len(novos)} valores de tribunal acrescentados", flush=True)


def _gravar_fonte(session, fonte_id: str, caminho: Path, leitor) -> None:
    if not caminho.is_file():
        print(f"{fonte_id}: arquivo ausente", flush=True)
        return
    _gravar_mapa(session, fonte_id, caminho, leitor(caminho))


def _gravar_mapa_se_existir(session, fonte_id: str, caminho: Path, leitor) -> None:
    if not caminho.is_file():
        print(f"{fonte_id}: arquivo ausente em {caminho.name}", flush=True)
        return
    _gravar_mapa(session, fonte_id, caminho, leitor(caminho))


def _gravar_mapa(session, fonte_id: str, caminho: Path, valores: dict) -> None:
    if not valores:
        print(f"{fonte_id}: sem valores", flush=True)
        return
    if caminho.is_dir():
        resumo = _hash_arquivos(sorted(caminho.glob("*.json")))
    else:
        resumo = hash_arquivo(caminho)
    if carga_concluida(session, resumo) is not None:
        print(f"{fonte_id}: arquivo já carregado", flush=True)
        return
    exercicios = {ano for (_ente, ano, _componente) in valores}
    carga = abrir_carga(session, fonte_id, caminho, resumo, None, exercicios)
    mapa = {
        chave: {"valor": float(valor), "natureza": "Bruto", "conta": None, "linha": None}
        for chave, valor in valores.items()
    }
    quantidade = gravar_valores(session, carga.id, mapa)
    print(f"{fonte_id}: {quantidade} valores", flush=True)


def _csvs(pasta: Path) -> list[Path]:
    if not pasta.is_dir():
        return []
    return [arquivo for arquivo in sorted(pasta.glob("*.csv")) if arquivo.stem != "modelo" and _tem_dados(arquivo)]


def _tem_dados(caminho: Path) -> bool:
    with caminho.open(encoding="utf-8") as arquivo:
        linhas = [linha for linha in arquivo.readlines() if linha.strip()]
    return len(linhas) > 1


def _hash_arquivos(arquivos: list[Path]) -> str:
    digest = hashlib.sha256()
    for arquivo in arquivos:
        digest.update(arquivo.name.encode())
        digest.update(arquivo.read_bytes())
    return digest.hexdigest()
