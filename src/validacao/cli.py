import argparse
import os
from pathlib import Path

from sqlalchemy.orm import Session

from validacao.cargas import carregar_pasta
from validacao.catalogo import semear
from validacao.db import BANCO_PADRAO, PASTA_PLANILHAS, criar_engine, criar_tabelas
from validacao.decisoes import reabrir_alterados
from validacao.motor import reprocessar, texto_resumo as texto_verificacao
from validacao.referencia import gerar_relatorio, metas_atingidas, texto_resumo
from validacao.regras import garantir_regra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validacao",
        description="Carga e verificação das bases do coeficiente de participação",
    )
    parser.add_argument("--banco", type=Path, default=None, help="Arquivo SQLite")
    subcomandos = parser.add_subparsers(dest="comando", required=True)

    iniciar = subcomandos.add_parser("init", help="Cria o banco e o catálogo")
    iniciar.set_defaults(func=_init)

    carregar = subcomandos.add_parser("carregar", help="Carrega as quatro planilhas da pasta")
    carregar.add_argument("--pasta", type=Path, default=None, help="Pasta das planilhas")
    carregar.set_defaults(func=_carregar)

    residuos = subcomandos.add_parser("residuos", help="Reproduz o valor do SICONFI e grava os resíduos")
    residuos.add_argument("--saida", type=Path, default=None, help="CSV de resíduos")
    residuos.set_defaults(func=_residuos)

    verificar = subcomandos.add_parser("verificar", help="Calcula situações e fonte sugerida")
    verificar.set_defaults(func=_verificar)

    complementar = subcomandos.add_parser(
        "complementar",
        help="Carrega IBGE, CONFAZ, SEFAZ, compensações, Simples, tribunais, MSC, RREO e entrega",
    )
    complementar.add_argument("--pasta", type=Path, default=None, help="Pasta das fontes complementares")
    complementar.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Quantidade máxima de exercícios sem abertura consultados na API",
    )
    complementar.set_defaults(func=_complementar)

    congelar = subcomandos.add_parser("congelar", help="Congela a base liberada")
    congelar.add_argument("--responsavel", type=int, required=True, help="Id do usuário responsável")
    congelar.set_defaults(func=_congelar)

    apurar_cmd = subcomandos.add_parser("apurar", help="Apura os coeficientes de uma base congelada")
    apurar_cmd.add_argument("--base", type=int, required=True)
    apurar_cmd.add_argument("--formula", type=int, default=None)
    apurar_cmd.set_defaults(func=_apurar)

    exportar_cmd = subcomandos.add_parser("exportar", help="Exporta a base e os coeficientes")
    exportar_cmd.add_argument("--base", type=int, required=True)
    exportar_cmd.add_argument("--formula", type=int, default=None)
    exportar_cmd.add_argument("--formato", choices=("csv", "xlsx"), default="csv")
    exportar_cmd.set_defaults(func=_exportar)

    backup = subcomandos.add_parser("backup", help="Copia o banco para a pasta de segurança")
    backup.set_defaults(func=_backup)

    servir = subcomandos.add_parser("servir", help="Abre a central de conferência neste computador")
    servir.add_argument("--porta", type=int, default=int(os.environ.get("CONFERENCIA_PORTA", "8000")))
    servir.add_argument("--host", default=os.environ.get("CONFERENCIA_HOST", "127.0.0.1"),
                        help="Endereço de escuta (0.0.0.0 num servidor ou no Docker)")
    servir.set_defaults(func=_servir)

    servir_v1 = subcomandos.add_parser("servir-v1", help="Abre as telas da versão 1 (legado)")
    servir_v1.add_argument("--porta", type=int, default=8001)
    servir_v1.set_defaults(func=_servir_v1)

    migrar_cmd = subcomandos.add_parser("migrar", help="Copia dados e decisões da versão 1 para a conferência (com backup)")
    migrar_cmd.set_defaults(func=_migrar)

    validar_cmd = subcomandos.add_parser("validar", help="Executa as regras de qualidade da conferência")
    validar_cmd.set_defaults(func=_validar)

    pacote_cmd = subcomandos.add_parser("importar-siconfi", help="Importa o pacote de RREO e MSC (base_validacao_coeficiente.csv e anexos)")
    pacote_cmd.add_argument("--pasta", type=Path, default=None, help="Pasta dos arquivos (padrão: Bases Conjuntas)")
    pacote_cmd.add_argument("--sem-validar", action="store_true", help="Não executa a validação após importar")
    pacote_cmd.set_defaults(func=_importar_siconfi)

    planilha_cmd = subcomandos.add_parser("planilha-central", help="Gera a planilha completa da central (2019 a 2025) com abas explicativas")
    planilha_cmd.add_argument("--saida", type=Path, default=None, help="Arquivo .xlsx de saída (padrão: relatorios/central_inconsistencias_<data>.xlsx)")
    planilha_cmd.set_defaults(func=_planilha_central)

    manut_cmd = subcomandos.add_parser("manutencao", help="Atualiza as estatísticas do banco e o compacta (VACUUM)")
    manut_cmd.add_argument("--sem-vacuum", action="store_true", help="Só atualiza as estatísticas")
    manut_cmd.set_defaults(func=_manutencao)

    arquivar_cmd = subcomandos.add_parser("arquivar-v1", help="Move as tabelas da versão 1 para um arquivo à parte (libera ~130 MB)")
    arquivar_cmd.add_argument("--confirmar", action="store_true", help="Necessário para executar")
    arquivar_cmd.set_defaults(func=_arquivar_v1)

    nuvem_cmd = subcomandos.add_parser("copia-nuvem", help="Grava a cópia compactada diária do banco na pasta do OneDrive")
    nuvem_cmd.add_argument("--forcar", action="store_true", help="Grava mesmo que a última cópia tenha menos de 24 h")
    nuvem_cmd.set_defaults(func=_copia_nuvem)

    tarefa_cmd = subcomandos.add_parser("tarefa", help="(uso interno) executa uma tarefa em segundo plano")
    tarefa_cmd.add_argument("tarefa_id")
    tarefa_cmd.set_defaults(func=_tarefa)

    demo_cmd = subcomandos.add_parser("demo", help="Cria a base de demonstração com dados fictícios")
    demo_cmd.add_argument("--destino", type=Path, default=None)
    demo_cmd.add_argument("--porta", type=int, default=None, help="Se informada, abre a demonstração")
    demo_cmd.set_defaults(func=_demo)

    argumentos = parser.parse_args(argv)
    if argumentos.banco is None and argumentos.comando not in ("demo", "tarefa"):
        from validacao.db import mover_para_pasta_local

        mover_para_pasta_local(progresso=lambda m: print(m, flush=True))
    return argumentos.func(argumentos)


def _init(argumentos) -> int:
    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    with Session(engine) as session:
        semear(session)
        garantir_regra(session)
        session.commit()
    print(f"Banco pronto em {argumentos.banco or BANCO_PADRAO}")
    return 0


def _carregar(argumentos) -> int:
    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    pasta = argumentos.pasta or PASTA_PLANILHAS
    try:
        carregar_pasta(engine, pasta)
    except Exception as erro:
        _registrar_erro(argumentos, None, str(erro))
        print(erro)
        return 1
    return _verificar(argumentos)


def _complementar(argumentos) -> int:
    from sqlalchemy import select

    from validacao.cargas.complementar import carregar_complementares
    from validacao.modelos import Verificacao

    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    with Session(engine) as session:
        garantir_regra(session)
        session.commit()
        tem_verificacao = session.scalar(select(Verificacao.id).limit(1))
    if tem_verificacao is None:
        _verificar(argumentos)
    try:
        carregar_complementares(engine, argumentos.pasta, argumentos.limite)
    except Exception as erro:
        _registrar_erro(argumentos, None, str(erro))
        print(erro)
        return 1
    return _verificar(argumentos)


def _registrar_erro(argumentos, fonte_id: str | None, mensagem: str) -> None:
    from validacao.operacao import registrar_evento_engine

    registrar_evento_engine(criar_engine(argumentos.banco or BANCO_PADRAO), fonte_id, "ERRO", mensagem)


def _congelar(argumentos) -> int:
    from validacao.apuracao import CongelamentoRecusado, congelar

    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    with Session(engine) as session:
        try:
            versao = congelar(session, argumentos.responsavel)
            session.commit()
        except CongelamentoRecusado as erro:
            print(erro)
            return 1
    print(f"Base {versao.id} congelada. Hash {versao.hash}")
    return 0


def _apurar(argumentos) -> int:
    from validacao.apuracao import apurar

    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    with Session(engine) as session:
        linhas = apurar(session, argumentos.base, argumentos.formula)
        session.commit()
    print(f"{len(linhas)} coeficientes")
    return 0


def _exportar(argumentos) -> int:
    from validacao.apuracao import exportar, garantir_formula

    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    with Session(engine) as session:
        formula_id = argumentos.formula or garantir_formula(session).id
        destino = exportar(session, argumentos.base, formula_id, argumentos.formato)
        session.commit()
    print(destino)
    return 0


def _backup(argumentos) -> int:
    from validacao.operacao import copiar_banco

    origem = argumentos.banco or BANCO_PADRAO
    destino = copiar_banco(origem)
    print(destino)
    return 0


def _verificar(argumentos) -> int:
    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    criar_tabelas(engine)
    with Session(engine) as session:
        garantir_regra(session)
        session.commit()
        contagem, linhas = reprocessar(session)
        reabertos = reabrir_alterados(session)
        session.commit()
    print(texto_verificacao(contagem, linhas))
    if reabertos:
        print(f"Reabertos: {reabertos}")
    return 0


def _servir(argumentos) -> int:
    import uvicorn

    import json as _json

    from validacao.conferencia.execucao import componentes_ativos, executar, ultima_execucao
    from validacao.conferencia.migracao import atualizar_estrutura, esta_migrado, precisa_atualizar
    from validacao.conferencia.web import criar_app

    banco = argumentos.banco or BANCO_PADRAO
    engine = criar_engine(banco)
    if not esta_migrado(engine):
        _migrar(argumentos)
    elif precisa_atualizar(engine):
        from validacao.operacao import copiar_banco

        print(f"Cópia de segurança antes da atualização: {copiar_banco(banco)}", flush=True)
        print(f"Atualização da estrutura: {atualizar_estrutura(engine)}", flush=True)
    with Session(engine) as session:
        ultima = ultima_execucao(session)
        precisa_validar = ultima is None or set(componentes_ativos(session)) - set(_json.loads(ultima.componentes or "[]"))
    if precisa_validar:
        print("Executando a validação de todos os componentes...", flush=True)
        executar(engine, "sistema")
    local = argumentos.host in ("127.0.0.1", "localhost")
    porta = _porta_livre(argumentos.porta) if local else argumentos.porta  # num servidor a porta é fixa
    if porta != argumentos.porta:
        print(f"A porta {argumentos.porta} está em uso por outro programa; usando a porta {porta}.", flush=True)
    print(f"Banco em uso: {banco}", flush=True)
    if os.environ.get("CONFERENCIA_COPIA_NUVEM", "1") != "0":
        try:
            from validacao.conferencia import tarefas

            tarefas.iniciar(banco, "copia_nuvem", {}, "sistema")  # em segundo plano; no máximo uma vez por dia
        except Exception as erro:  # a cópia é auxiliar
            print(f"Cópia compactada não iniciada: {erro}", flush=True)
    endereco = "127.0.0.1" if local else argumentos.host
    print(f"Conferência disponível em http://{endereco}:{porta}", flush=True)
    uvicorn.run(criar_app(banco), host=argumentos.host, port=porta, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("CONFERENCIA_PROXIES", "127.0.0.1"))
    return 0


def _porta_livre(preferida: int, tentativas: int = 20) -> int:
    import socket

    for porta in range(preferida, preferida + tentativas):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as teste:
            try:
                teste.bind(("127.0.0.1", porta))
                return porta
            except OSError:
                continue
    raise SystemExit(f"Nenhuma porta livre entre {preferida} e {preferida + tentativas - 1}. Use --porta.")


def _servir_v1(argumentos) -> int:
    import uvicorn

    from validacao.web import criar_app

    porta = _porta_livre(argumentos.porta)
    print(f"Telas da versão 1 em http://127.0.0.1:{porta}", flush=True)
    uvicorn.run(criar_app(argumentos.banco or BANCO_PADRAO), host="127.0.0.1", port=porta)
    return 0


def _migrar(argumentos) -> int:
    from validacao.conferencia.migracao import migrar
    from validacao.operacao import copiar_banco

    banco = argumentos.banco or BANCO_PADRAO
    if Path(banco).exists():
        copia = copiar_banco(banco)
        print(f"Cópia de segurança antes da migração: {copia}", flush=True)
    resumo = migrar(criar_engine(banco))
    print(
        f"Migração: {resumo['lotes']} lotes, {resumo['registros']} registros e {resumo['decisoes']} decisões copiados "
        "(tabelas da versão 1 preservadas).",
        flush=True,
    )
    return 0


def _importar_siconfi(argumentos) -> int:
    from validacao.conferencia.execucao import executar
    from validacao.conferencia.migracao import atualizar_estrutura, precisa_atualizar
    from validacao.conferencia.pacote_siconfi import importar_pacote

    banco = argumentos.banco or BANCO_PADRAO
    engine = criar_engine(banco)
    if precisa_atualizar(engine):
        atualizar_estrutura(engine)
    pasta = argumentos.pasta or PASTA_PLANILHAS
    resultado = importar_pacote(engine, pasta, "linha de comando", Path(banco).parent, progresso=print)
    for chave in ("SICONFI_RREO", "SICONFI_MSC"):
        item = resultado.get(chave, {})
        print(f"{chave}: lote {item.get('lote')} — {item.get('situacao')}, {item.get('novos', 0)} novos, "
              f"{item.get('novas_versoes', 0)} versões, {item.get('repetidos', 0)} iguais")
    print(f"Consultas: {resultado.get('status', 0)} · cobertura: {resultado.get('cobertura', 0)} · ocorrências: {len(resultado['ocorrencias'])}")
    if not argumentos.sem_validar:
        print(f"Validação: execução {executar(engine, 'linha de comando', progresso=print)}")
    return 0


def _validar(argumentos) -> int:
    from validacao.conferencia.execucao import executar

    execucao = executar(criar_engine(argumentos.banco or BANCO_PADRAO), "linha de comando", progresso=print)
    print(f"Validação concluída: execução {execucao}")
    return 0


def _demo(argumentos) -> int:
    from validacao.conferencia.demo import construir

    destino = argumentos.destino or (BANCO_PADRAO.parent / "demonstracao.sqlite")
    construir(destino)
    print(f"Base de demonstração (dados fictícios) criada em {destino}")
    if argumentos.porta:
        import uvicorn

        from validacao.conferencia.web import criar_app

        porta = _porta_livre(argumentos.porta)
        print(f"Demonstração disponível em http://127.0.0.1:{porta}", flush=True)
        uvicorn.run(criar_app(destino), host="127.0.0.1", port=porta)
    return 0


def _residuos(argumentos) -> int:
    engine = criar_engine(argumentos.banco or BANCO_PADRAO)
    with Session(engine) as session:
        contagem = gerar_relatorio(session, argumentos.saida)
    print(texto_resumo(contagem))
    return 0 if metas_atingidas(contagem) else 1


def _planilha_central(args) -> None:
    from datetime import datetime

    from validacao.conferencia.planilha_central import gerar
    from validacao.db import BANCO_PADRAO

    banco = getattr(args, "banco", None) or BANCO_PADRAO
    from validacao.db import PASTA_RELATORIOS

    saida = args.saida or (PASTA_RELATORIOS / f"central_inconsistencias_{datetime.now():%Y%m%d_%H%M}.xlsx")
    resultado = gerar(criar_engine(banco), saida, progresso=lambda m: print(m, flush=True))
    print(f"Planilha gerada: {resultado['arquivo']} ({resultado['casos']} casos, {resultado['colunas']} colunas)")


def _tarefa(argumentos) -> int:
    from validacao.conferencia.tarefas import executar_tarefa

    return executar_tarefa(argumentos.banco or BANCO_PADRAO, argumentos.tarefa_id)


def _manutencao(argumentos) -> int:
    from validacao.conferencia.manutencao import manter

    resultado = manter(criar_engine(argumentos.banco or BANCO_PADRAO), progresso=lambda m: print(m, flush=True),
                       vacuum=not argumentos.sem_vacuum)
    print(resultado)
    return 0


def _arquivar_v1(argumentos) -> int:
    from validacao.conferencia.manutencao import arquivar_v1, manter
    from validacao.operacao import copiar_banco

    if not argumentos.confirmar:
        print("Copia as tabelas da versão 1 (verificacao, valor_observado) para data/arquivo_v1_<data>.sqlite e as esvazia "
              "no banco principal. A tela antiga (servir-v1) deixa de mostrar esse histórico. Rode de novo com --confirmar.")
        return 1
    banco = argumentos.banco or BANCO_PADRAO
    print(f"Cópia de segurança: {copiar_banco(banco)}", flush=True)
    destino = arquivar_v1(banco, progresso=lambda m: print(m, flush=True))
    if destino:
        print(f"Arquivo: {destino}", flush=True)
        manter(criar_engine(banco), progresso=lambda m: print(m, flush=True))
    return 0


def _copia_nuvem(argumentos) -> int:
    from validacao.operacao import copia_nuvem

    destino = copia_nuvem(argumentos.banco or BANCO_PADRAO, intervalo_horas=0 if argumentos.forcar else 24)
    print(destino or "Nada a fazer (banco numa pasta sincronizada ou cópia com menos de 24 h).")
    return 0
