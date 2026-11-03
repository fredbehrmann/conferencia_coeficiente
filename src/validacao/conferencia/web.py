"""Interface web da conferência (FastAPI + Jinja, sem dependências externas no navegador)."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from validacao.conferencia import consultas, exportacao, importacao
from validacao.conferencia import modelos as cf
from validacao.conferencia.catalogo import (
    COMPONENTES as COMPONENTES_CATALOGO,
    CONCEITOS,
    ROTULO_CURTO,
    SELECAO_INICIAL,
    EXERCICIOS,
    NORMAS,
    PARCELAS,
    PENDENCIAS,
    SIGLAS,
)
from validacao.conferencia.comparacao import sigla
from validacao.conferencia.confianca import NIVEIS as NIVEIS_CONFIANCA
from validacao.conferencia.decisao import (
    ACOES,
    SITUACOES,
    DecisaoRecusada,
    aceitar_sem_alerta,
    decisao_vigente,
    historico,
    registrar,
    situacao_do_caso,
)
from validacao.conferencia.dinheiro import ValorInvalido, para_centavos, percentual, reais, reais_curto
from validacao.conferencia.execucao import executar, expandir_valores, ultima_execucao
from validacao.conferencia.exportacao import DIAGNOSTICOS
from validacao.conferencia.grafico import CORES, legenda, serie_svg
from validacao.conferencia.migracao import configuracao, garantir_conjunto, preparar


def ESCOPO_EXERCICIOS(componente: str) -> list[int]:
    from validacao.conferencia.catalogo import ESCOPO_COMPONENTE

    return ESCOPO_COMPONENTE.get(componente, {}).get("exercicios", EXERCICIOS)


NOME_COMPONENTES = {}
from validacao.conferencia.regras import GRAVIDADES, ORDEM, REGRAS
from validacao.conferencia import acesso as contas
from validacao.conferencia import tarefas
from validacao.conferencia.cache import CachePorVersao
from validacao.db import BANCO_PADRAO, criar_engine
from validacao.modelos import Ente, Usuario

PASTA = Path(__file__).resolve().parent
PERFIS = contas.PERFIS
UFS = ("AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO").split()
COLUNAS_PADRAO = ["SICONFI_DCA", "SICONFI_RREO", "SICONFI_MSC", "SIOPE", "SIOPS", "TCE"]
ESTADOS = {"VALOR": "Valor", "ZERO": "Zero declarado", "AUSENTE": "Célula vazia", "INVALIDO": "Formato inválido",
           "NAO_APLICAVEL": "Não aplicável", "NAO_EXIGIVEL": "Não exigível"}
COMPARABILIDADE = {
    "REFERENCIA": ("Referência", "ref"),
    "SIM": ("Comparável", "ok"),
    "RESSALVA": ("Comparável com ressalva", "ressalva"),
    "NAO": ("Não comparável diretamente", "nao"),
    "SEM_DADO": ("Sem valor", "vazio"),
    "SEM_REFERENCIA": ("Sem referência", "vazio"),
}
GRAVIDADE_CLASSE = {0: "g0", 1: "g1", 2: "g2", 3: "g3", 4: "g4"}
GRAVIDADE_ICONE = {0: "", 1: "ℹ", 2: "◔", 3: "▲", 4: "⛔"}


def _ambiente(templates: Jinja2Templates) -> None:
    NOME_COMPONENTES.update({c["id"]: c["nome"] for c in COMPONENTES_CATALOGO})
    env = templates.env
    env.filters["reais"] = lambda v: reais(v, casas=0, prefixo=False)
    env.filters["reais2"] = lambda v: reais(v)
    env.filters["curto"] = reais_curto
    env.filters["pct"] = lambda v, casas=1: percentual(v, casas)
    env.filters["pcts"] = lambda v, casas=1: percentual(v, casas, sinal=True)
    env.filters["sigla"] = sigla
    env.filters["milhar"] = lambda v: f"{int(v or 0):,}".replace(",", ".")
    env.filters["data"] = lambda d: "" if d is None else d.strftime("%d/%m/%Y")
    env.filters["datahora"] = lambda d: "" if d is None else d.strftime("%d/%m/%Y %H:%M")
    env.filters["json"] = lambda v: json.loads(v) if isinstance(v, str) and v else (v or {})
    env.filters["nome_arquivo"] = _nome_arquivo
    env.filters["tipos"] = lambda t: [x for x in (t or "").split("|") if x]
    env.globals.update(
        SIGLAS=SIGLAS, REGRAS=REGRAS, ORDEM_REGRAS=ORDEM, GRAVIDADES=GRAVIDADES, GRAVIDADE_CLASSE=GRAVIDADE_CLASSE,
        GRAVIDADE_ICONE=GRAVIDADE_ICONE, SITUACOES=SITUACOES, ACOES=ACOES, ESTADOS=ESTADOS, COMPARABILIDADE=COMPARABILIDADE,
        DIAGNOSTICOS=DIAGNOSTICOS, UFS=UFS, EXERCICIOS=EXERCICIOS, CONCEITOS=CONCEITOS, PARCELAS=PARCELAS,
        ORDENACOES=consultas.ORDENACOES, CORES=CORES, ROTULO_CURTO=ROTULO_CURTO,
        NOME_COMPONENTE={c["id"]: c["nome"] for c in COMPONENTES_CATALOGO},
        FUNDAMENTO_COMPONENTE={c["id"]: c["fundamento"] for c in COMPONENTES_CATALOGO},
        nome_ente=_nome_ente, NIVEIS_CONFIANCA=NIVEIS_CONFIANCA,
    )


def criar_app(banco: Path | None = None) -> FastAPI:
    destino = Path(banco) if banco is not None else BANCO_PADRAO
    cache = CachePorVersao()

    @asynccontextmanager
    async def vida(app: FastAPI):
        engine = criar_engine(destino)
        preparar(engine)
        app.state.engine = engine
        yield

    app = FastAPI(lifespan=vida, title="Conferência do coeficiente de participação")
    segredo = os.environ.get("CONFERENCIA_SEGREDO") or _segredo(destino)
    app.add_middleware(SessionMiddleware, secret_key=segredo, same_site="lax", session_cookie="conferencia",
                       https_only=os.environ.get("CONFERENCIA_COOKIE_SEGURO") == "1", max_age=12 * 3600)
    app.mount("/estatico", StaticFiles(directory=str(PASTA / "estatico")), name="estatico")
    templates = Jinja2Templates(directory=str(PASTA / "templates"))
    _ambiente(templates)

    def sessao() -> Session:
        return Session(app.state.engine)

    def usuario_atual(request: Request, session: Session) -> Usuario | None:
        """Pessoa da sessão, se a conta continua ativa e a senha não mudou desde a entrada."""
        usuario_id = request.session.get("usuario_id")
        if not usuario_id:
            return None
        credencial = contas.acesso_de(session, usuario_id)
        if credencial is None or not credencial.ativo or credencial.versao_sessao != request.session.get("versao"):
            request.session.clear()
            return None
        usuario = session.get(Usuario, usuario_id)
        if usuario is not None:
            usuario.login = credencial.login
            usuario.trocar_senha = credencial.trocar_senha
            usuario.administrador = usuario.perfil == "ADMINISTRADOR"
        return usuario

    def pagina(request: Request, nome: str, session: Session, usuario, status: int = 200, **contexto):
        demo = configuracao(session, "demonstracao", False)
        contexto.update(
            usuario=usuario, demo=demo, aba=contexto.get("aba", ""), aviso=request.session.pop("aviso", None),
            erro=contexto.get("erro") or request.session.pop("erro", None),
            validacao=tarefas.resumo(destino) if usuario else None,
            execucao_atual=(contexto.get("execucao_atual") or ultima_execucao(session)) if usuario else None,
        )
        return templates.TemplateResponse(request, nome, contexto, status_code=status)

    def exigir(request: Request, session: Session):
        usuario = usuario_atual(request, session)
        if usuario is None:
            from urllib.parse import quote

            volta = str(request.url.path) + ("?" + request.url.query if request.url.query else "")
            raise HTTPException(status_code=303, headers={"Location": f"/login?volta={quote(volta)}"})
        if usuario.trocar_senha and request.url.path != "/minha-senha":
            raise HTTPException(status_code=303, headers={"Location": "/minha-senha"})
        return usuario

    def exigir_admin(request: Request, session: Session):
        usuario = exigir(request, session)
        if not usuario.administrador:
            raise HTTPException(status_code=403, detail="Somente administradores.")
        return usuario

    # ------------------------------------------------------------------ login
    @app.get("/saude")
    def saude():
        """Verificação de vida para o Docker e para balanceadores (sem login)."""
        with sessao() as session:
            session.execute(select(1))
        return JSONResponse({"ok": True})

    def _contexto_login(session):
        from validacao.conferencia.demo import LOGIN_DEMO, SENHA_DEMO

        demo = configuracao(session, "demonstracao", False)
        return {"credencial_demo": (LOGIN_DEMO, SENHA_DEMO) if demo else None}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, volta: str = "/visao"):
        with sessao() as session:
            return pagina(request, "login.html", session, None, volta=volta, **_contexto_login(session))

    @app.post("/login")
    def login(request: Request, login: str = Form(""), senha: str = Form(""), volta: str = Form("/visao")):
        with sessao() as session:
            try:
                usuario, credencial = contas.autenticar(session, login, senha)
            except contas.AcessoRecusado as motivo:
                return pagina(request, "login.html", session, None, status=401, volta=volta, login=login,
                              erro=str(motivo), **_contexto_login(session))
            request.session.clear()
            request.session["usuario_id"] = usuario.id
            request.session["versao"] = credencial.versao_sessao
            destino_volta = volta if volta.startswith("/") and not volta.startswith("//") else "/"
            if credencial.trocar_senha:
                destino_volta = "/minha-senha"
        return RedirectResponse(destino_volta, status_code=303)

    @app.post("/sair")
    def sair(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/minha-senha", response_class=HTMLResponse)
    def minha_senha_form(request: Request):
        with sessao() as session:
            usuario = exigir(request, session)
            return pagina(request, "minha_senha.html", session, usuario, minimo=contas.TAMANHO_MINIMO)

    @app.post("/minha-senha")
    def minha_senha(request: Request, atual: str = Form(""), nova: str = Form(""), confirmacao: str = Form("")):
        with sessao() as session:
            usuario = exigir(request, session)
            credencial = contas.acesso_de(session, usuario.id)
            erro = None
            if not contas.conferir_senha(atual, credencial.senha_hash):
                erro = "A senha atual não confere."
            elif nova != confirmacao:
                erro = "A confirmação é diferente da nova senha."
            else:
                try:
                    contas.definir_senha(session, usuario.id, nova, trocar_senha=False)
                except contas.AcessoRecusado as motivo:
                    erro = str(motivo)
            if erro:
                return pagina(request, "minha_senha.html", session, usuario, status=400, erro=erro,
                              minimo=contas.TAMANHO_MINIMO)
            request.session["versao"] = contas.acesso_de(session, usuario.id).versao_sessao
            request.session["aviso"] = "Senha alterada."
        return RedirectResponse("/visao", status_code=303)

    # ------------------------------------------------------------ usuários
    @app.get("/usuarios", response_class=HTMLResponse)
    def usuarios(request: Request):
        with sessao() as session:
            usuario = exigir_admin(request, session)
            senha_gerada = request.session.pop("senha_gerada", None)
            return pagina(request, "usuarios.html", session, usuario, aba="usuarios", contas=contas.contas(session),
                          perfis=PERFIS, descricao_perfil=contas.DESCRICAO_PERFIL, senha_gerada=senha_gerada,
                          sugestao=contas.gerar_senha(), minimo=contas.TAMANHO_MINIMO, agora=datetime.now())

    def _volta_usuarios(request: Request, aviso: str | None = None, erro: str | None = None):
        if aviso:
            request.session["aviso"] = aviso
        if erro:
            request.session["erro"] = erro
        return RedirectResponse("/usuarios", status_code=303)

    @app.post("/usuarios")
    def usuarios_criar(request: Request, login: str = Form(""), nome: str = Form(""), perfil: str = Form("ANALISTA"),
                       senha: str = Form(""), trocar_senha: str = Form("")):
        with sessao() as session:
            autor = exigir_admin(request, session)
            try:
                novo = contas.criar_conta(session, login, nome, perfil, senha, criado_por=autor.nome,
                                          trocar_senha=bool(trocar_senha))
            except contas.AcessoRecusado as motivo:
                return _volta_usuarios(request, erro=str(motivo))
            request.session["senha_gerada"] = {"login": contas.normalizar_login(login), "nome": novo.nome, "senha": senha,
                                               "trocar": bool(trocar_senha)}
            return _volta_usuarios(request, aviso=f"Conta {contas.normalizar_login(login)} criada para {novo.nome}.")

    @app.post("/usuarios/{usuario_id}/senha")
    def usuarios_senha(request: Request, usuario_id: int):
        with sessao() as session:
            exigir_admin(request, session)
            nova = contas.gerar_senha()
            try:
                contas.definir_senha(session, usuario_id, nova, trocar_senha=True)
            except contas.AcessoRecusado as motivo:
                return _volta_usuarios(request, erro=str(motivo))
            credencial = contas.acesso_de(session, usuario_id)
            pessoa = session.get(Usuario, usuario_id)
            request.session["senha_gerada"] = {"login": credencial.login, "nome": pessoa.nome, "senha": nova, "trocar": True}
            return _volta_usuarios(request, aviso=f"Senha de {credencial.login} redefinida.")

    @app.post("/usuarios/{usuario_id}/alterar")
    def usuarios_alterar(request: Request, usuario_id: int, perfil: str = Form(""), ativo: str = Form("")):
        with sessao() as session:
            autor = exigir_admin(request, session)
            try:
                contas.alterar_conta(session, usuario_id, autor.id, perfil=perfil or None,
                                     ativo={"1": True, "0": False}.get(ativo))
            except contas.AcessoRecusado as motivo:
                return _volta_usuarios(request, erro=str(motivo))
            return _volta_usuarios(request, aviso="Conta atualizada.")

    # ------------------------------------------------------------ visão geral
    @app.get("/visao", response_class=HTMLResponse)
    def visao(request: Request):
        from validacao.conferencia.grafico import mini_serie
        from validacao.conferencia.paineis import visao_geral

        with sessao() as session:
            usuario = exigir(request, session)
            dados = cache.obter(session, ("visao",), lambda: visao_geral(session))
            graficos = {}
            if dados.get("execucao"):
                cores = ["var(--serie-1)", "var(--serie-2)", "var(--serie-3)", "var(--serie-4)", "var(--serie-5)", "var(--serie-6)"]
                for indice, comp in enumerate(dados["ordem"]):
                    serie = {ano: celula["valor"] for ano, celula in dados["valores"][comp].items()}
                    graficos[comp] = mini_serie(serie, ESCOPO_EXERCICIOS(comp), cores[indice % len(cores)], NOME_COMPONENTES.get(comp, comp))
            return pagina(request, "visao.html", session, usuario, aba="visao", v=dados, graficos=graficos)

    @app.get("/recuperacao", response_class=HTMLResponse)
    def recuperacao_painel(request: Request, componente: str = "ISS_MUN", uf: str = "", q: str = "", filtro: str = ""):
        from validacao.conferencia.paineis import ESTADOS_CELULA, FILTROS_RECUPERACAO, recuperacao

        try:
            numero_pagina = max(1, int(request.query_params.get("pagina", 1)))
        except ValueError:
            numero_pagina = 1
        with sessao() as session:
            usuario = exigir(request, session)
            dados = cache.obter(session, ("recuperacao", componente, uf, q, filtro, numero_pagina),
                                lambda: recuperacao(session, componente, uf, q, filtro, numero_pagina))
            componentes = list(session.scalars(select(cf.Componente).where(cf.Componente.ativo.is_(True)).order_by(cf.Componente.ordem)))
            from urllib.parse import urlencode

            base_q = urlencode({k: v for k, v in {"componente": componente, "uf": uf, "q": q, "filtro": filtro}.items() if v})
            return pagina(request, "recuperacao.html", session, usuario, aba="recuperacao", r=dados, componentes=componentes,
                          f={"componente": componente, "uf": uf, "q": q, "filtro": filtro}, base_q=base_q,
                          ESTADOS_CELULA=ESTADOS_CELULA, FILTROS=FILTROS_RECUPERACAO)

    # --------------------------------------------------------------- central
    @app.get("/", response_class=HTMLResponse)
    def central(request: Request):
        filtros = consultas.Filtros.de_query(request.query_params)
        with sessao() as session:
            usuario = exigir(request, session)
            declaracoes = list(session.scalars(select(cf.Declaracao).order_by(cf.Declaracao.ordem)))
            com_dados = cache.obter(session, ("com_dados",),
                                    lambda: set(session.scalars(select(cf.Registro.declaracao_id).distinct())))
            if not filtros.colunas:
                filtros.colunas = [c for c in COLUNAS_PADRAO if c in com_dados] or [d.id for d in declaracoes if d.id in com_dados][1:4]
            selecao = _selecao(session)
            resultado = consultas.central(session, filtros, cache=cache)
            componentes = list(session.scalars(select(cf.Componente).order_by(cf.Componente.ordem)))
            return pagina(
                request, "central.html", session, usuario, aba="central", filtros=filtros, resultado=resultado,
                declaracoes=declaracoes, com_dados=com_dados, componentes=componentes, selecao=selecao,
                principal_padrao=(selecao.get("precedencia") or ["SICONFI_DCA"])[0],
            )

    @app.get("/caso/{caso_id}/registros", response_class=HTMLResponse)
    def caso_registros(request: Request, caso_id: int):
        with sessao() as session:
            usuario = exigir(request, session)
            caso = session.get(cf.Caso, caso_id)
            if caso is None:
                raise HTTPException(404)
            info = consultas.detalhe(session, caso)
            return pagina(request, "_registros.html", session, usuario, caso=caso, info=info)

    @app.get("/caso/{caso_id}/linha", response_class=HTMLResponse)
    def caso_linha(request: Request, caso_id: int):
        filtros = consultas.Filtros.de_query(request.query_params)
        with sessao() as session:
            usuario = exigir(request, session)
            caso = session.get(cf.Caso, caso_id)
            if caso is None:
                raise HTTPException(404)
            decisao = decisao_vigente(session, caso.id_ente, caso.exercicio, caso.componente_id)
            linha = {"caso": caso, "decisao": decisao, "situacao": situacao_do_caso(caso, decisao), "valores": expandir_valores(caso.valores)}
            return pagina(request, "_linha.html", session, usuario, l=linha, filtros=filtros,
                          principal_padrao=(_selecao(session).get("precedencia") or ["SICONFI_DCA"])[0])

    @app.get("/caso/{caso_id}", response_class=HTMLResponse)
    def caso_painel(request: Request, caso_id: int, parcial: int = 0):
        with sessao() as session:
            usuario = exigir(request, session)
            caso = session.get(cf.Caso, caso_id)
            if caso is None:
                raise HTTPException(404, "Caso não encontrado (a execução pode ter sido substituída).")
            atual = ultima_execucao(session)
            if atual is not None and caso.execucao_id != atual.id:
                novo = consultas.caso_por_chave(session, caso.id_ente, caso.exercicio, caso.componente_id)
                if novo is not None:
                    return RedirectResponse(f"/caso/{novo.id}?parcial={parcial}&{request.url.query}", status_code=303)
            contexto = _contexto_caso(session, caso)
            nome = "_caso.html" if parcial else "caso.html"
            return pagina(request, nome, session, usuario, aba="central", voltar=request.query_params.get("voltar", ""), **contexto)

    @app.get("/caso/chave/{id_ente}/{exercicio}/{componente}")
    def caso_chave(request: Request, id_ente: int, exercicio: int, componente: str):
        with sessao() as session:
            exigir(request, session)
            caso = consultas.caso_por_chave(session, id_ente, exercicio, componente)
            if caso is None:
                raise HTTPException(404, "Caso não encontrado na execução mais recente.")
            return RedirectResponse(f"/caso/{caso.id}", status_code=303)

    @app.post("/caso/{caso_id}/decidir")
    async def decidir(request: Request, caso_id: int):
        formulario = await request.form()
        with sessao() as session:
            usuario = exigir(request, session)
            caso = session.get(cf.Caso, caso_id)
            if caso is None:
                return JSONResponse({"ok": False, "erro": "Caso não encontrado."}, status_code=404)
            valor = None
            if formulario.get("valor"):
                try:
                    valor = para_centavos(str(formulario.get("valor")))
                except ValorInvalido:
                    return JSONResponse({"ok": False, "erro": "Valor em formato inválido. Use, por exemplo, 1.234.567,89."}, status_code=400)
            registro_id = formulario.get("registro_id")
            try:
                decisao = registrar(
                    session, caso, str(formulario.get("acao", "")), usuario.nome, usuario.id,
                    registro_id=int(registro_id) if registro_id else None, valor=valor,
                    justificativa=str(formulario.get("justificativa", "")), evidencias=str(formulario.get("evidencias", "")),
                )
                session.commit()
            except DecisaoRecusada as erro:
                session.rollback()
                return JSONResponse({"ok": False, "erro": str(erro)}, status_code=400)
            return JSONResponse({"ok": True, "decisao": decisao.id, "mensagem": f"Decisão registrada: {ACOES[decisao.acao]['rotulo']}."})

    # -------------------------------------------------------------- resultado
    @app.get("/resultado", response_class=HTMLResponse)
    def resultado(request: Request):
        filtros = consultas.Filtros.de_query(request.query_params)
        if request.query_params.get("enviado") != "1":
            filtros.somente_alerta = False
        with sessao() as session:
            usuario = exigir(request, session)
            dados = consultas.central(session, filtros, cache=cache)
            execucao = dados["execucao"]
            resumo = cache.obter(session, ("resumo_resultado", filtros.componente),
                                 lambda: _resumo_resultado(session, execucao, filtros.componente)) if execucao else {}
            componentes = list(session.scalars(select(cf.Componente).order_by(cf.Componente.ordem)))
            declaracoes = list(session.scalars(select(cf.Declaracao).order_by(cf.Declaracao.ordem)))
            return pagina(request, "resultado.html", session, usuario, aba="resultado", filtros=filtros, resultado=dados,
                          resumo=resumo, componentes=componentes, declaracoes=declaracoes)

    @app.get("/resultado/exportar")
    def resultado_exportar(request: Request, formato: str = "csv"):
        filtros = consultas.Filtros.de_query(request.query_params)
        with sessao() as session:
            exigir(request, session)
            cabecalho, dados = exportacao.linhas_resultado(session, filtros)
        carimbo = datetime.now().strftime("%Y%m%d-%H%M")
        if formato == "xlsx":
            conteudo = exportacao.xlsx_bytes({"Resultado": (cabecalho, dados)})  # modo write_only
            return Response(conteudo, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="conferencia-{carimbo}.xlsx"'})
        return Response(exportacao.csv_bytes(cabecalho, dados), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="conferencia-{carimbo}.csv"'})

    @app.get("/resultado/pacote")
    def resultado_pacote(request: Request):
        filtros = consultas.Filtros.de_query(request.query_params)
        with sessao() as session:
            usuario = exigir(request, session)
            conteudo = exportacao.pacote(session, filtros, usuario.nome)
        carimbo = datetime.now().strftime("%Y%m%d-%H%M")
        return Response(conteudo, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="pacote-conferencia-{carimbo}.zip"'})

    @app.post("/resultado/aceitar-sem-alerta")
    async def aceitar(request: Request):
        formulario = await request.form()
        filtros = consultas.Filtros.de_query(formulario)
        filtros.somente_alerta = False
        with sessao() as session:
            usuario = exigir(request, session)
            ids = consultas.ids_filtrados(session, filtros)
            quantidade = aceitar_sem_alerta(session, usuario.nome, usuario.id, ids)
            session.commit()
        request.session["aviso"] = f"{quantidade} casos conferidos sem alerta receberam a decisão 'manter valor principal', em seu nome."
        return RedirectResponse(f"/resultado?{filtros.query()}", status_code=303)

    # --------------------------------------------------------------- importar
    pasta_importacoes = destino.parent / "importacoes"

    @app.get("/importar", response_class=HTMLResponse)
    def importar_inicio(request: Request):
        with sessao() as session:
            usuario = exigir(request, session)
            from validacao.conferencia.pacote_siconfi import descrever

            lotes = list(session.scalars(select(cf.Lote).order_by(cf.Lote.id.desc()).limit(50)))
            from validacao.conferencia.paineis import situacao_pacote

            arquivos_pacote = descrever(pasta_pacote())
            return pagina(request, "importar.html", session, usuario, aba="importar", lotes=lotes,
                          bases=_situacao_bases(session), ultima_bases=_ultima_importacao_bases(),
                          pacote=arquivos_pacote, pasta_pacote=str(pasta_pacote()),
                          pacote_enviado=any(a.get("presente") and Path(a["caminho"]).parent == pasta_pacote_enviado
                                             for a in arquivos_pacote[:1]),
                          pacote_info=configuracao(session, "pacote_siconfi"),
                          sp=situacao_pacote(session, arquivos_pacote), **_catalogos(session))

    def _situacao_bases(session):
        from validacao.conferencia.bases_fixas import LAYOUTS, ORDEM

        situacao = []
        for fonte in ORDEM:
            layout = LAYOUTS[fonte]
            ultimo = session.scalar(select(cf.Lote).where(cf.Lote.declaracao_id == layout["declaracao"],
                                                          cf.Lote.situacao == "CONCLUIDO",
                                                          cf.Lote.origem_tipo.in_(("ARQUIVO", "MIGRACAO")))
                                    .order_by(cf.Lote.id.desc()).limit(1))
            vigentes = session.scalar(select(func.count()).select_from(cf.Registro).where(
                cf.Registro.declaracao_id == layout["declaracao"], cf.Registro.vigente.is_(True)))
            situacao.append({"fonte": fonte, **layout, "lote": ultimo, "vigentes": vigentes or 0})
        return situacao

    def _ultima_importacao_bases():
        for dados in tarefas.listar(destino):
            if dados.get("tipo") == "importar_bases":
                return dados
        return None

    @app.post("/importar/arquivo", response_class=HTMLResponse)
    async def importar_arquivo(request: Request, arquivo: UploadFile = File(...)):
        conteudo = await arquivo.read()
        with sessao() as session:
            usuario = exigir(request, session)
            try:
                planilha = importacao.ler(arquivo.filename or "arquivo", conteudo)
            except Exception as erro:  # leitura inválida
                lotes = list(session.scalars(select(cf.Lote).order_by(cf.Lote.id.desc()).limit(50)))
                return pagina(request, "importar.html", session, usuario, aba="importar", lotes=lotes,
                              erro=f"Não foi possível ler o arquivo: {erro}", **_catalogos(session))
            token = secrets.token_hex(8)
            temporaria = pasta_importacoes / "pendentes"
            temporaria.mkdir(parents=True, exist_ok=True)
            (temporaria / f"{token}.bin").write_bytes(conteudo)
            (temporaria / f"{token}.json").write_text(json.dumps({"nome": arquivo.filename}), encoding="utf-8")
            aba = next(iter(planilha.abas))
            return _tela_mapeamento(request, session, usuario, token, planilha, aba, 1, {})

    @app.post("/importar/mapear", response_class=HTMLResponse)
    async def importar_mapear(request: Request):
        formulario = await request.form()
        token = str(formulario.get("token", ""))
        with sessao() as session:
            usuario = exigir(request, session)
            planilha = _planilha_pendente(pasta_importacoes, token)
            aba = str(formulario.get("aba") or next(iter(planilha.abas)))
            try:
                cabecalho = int(formulario.get("linha_cabecalho") or 1)
            except ValueError:
                cabecalho = 1
            acao = formulario.get("acao")
            if acao != "confirmar":
                return _tela_mapeamento(request, session, usuario, token, planilha, aba, cabecalho, dict(formulario))
            try:
                mapa = _mapa_do_formulario(formulario, aba, cabecalho)
                resultado_importacao = importacao.importar(session, planilha, mapa, usuario.nome, pasta_importacoes / "arquivos")
                session.commit()
            except (importacao.ImportacaoRecusada, ValueError) as erro:
                session.rollback()
                return _tela_mapeamento(request, session, usuario, token, planilha, aba, cabecalho, dict(formulario), erro=str(erro))
            return pagina(request, "importar_resultado.html", session, usuario, aba="importar", r=resultado_importacao)

    @app.get("/importar/lote/{lote_id}", response_class=HTMLResponse)
    def importar_lote(request: Request, lote_id: int):
        with sessao() as session:
            usuario = exigir(request, session)
            lote = session.get(cf.Lote, lote_id)
            if lote is None:
                raise HTTPException(404)
            relatorio = json.loads(lote.relatorio or "{}")
            return pagina(request, "lote.html", session, usuario, aba="importar", lote=lote, relatorio=relatorio)

    def _tela_mapeamento(request, session, usuario, token, planilha, aba, cabecalho, valores, erro=None):
        try:
            prev = importacao.previa(planilha, aba, cabecalho)
        except importacao.ImportacaoRecusada as falha:
            prev = {"colunas": [], "linhas": [], "total_linhas": 0}
            erro = erro or str(falha)
        return pagina(request, "importar_mapear.html", session, usuario, aba="importar", token=token, planilha=planilha,
                      aba_sel=aba, cabecalho=cabecalho, previa=prev, v=valores, erro=erro, **_catalogos(session))

    # ------------------------------------------------------------ auxiliares
    @app.get("/dados", response_class=HTMLResponse)
    def dados(request: Request):
        params = {k: v for k, v in request.query_params.items() if v}
        params.setdefault("componente", "ISS_MUN")
        try:
            pagina_atual = max(1, int(params.get("pagina", 1)))
        except ValueError:
            pagina_atual = 1
        with sessao() as session:
            usuario = exigir(request, session)
            resultado_dados = consultas.dados_brutos(session, params, pagina_atual, cache=cache)
            from urllib.parse import urlencode

            base_q = urlencode({k: v for k, v in params.items() if k != "pagina"})
            return pagina(request, "dados.html", session, usuario, aba="dados", p=params, r=resultado_dados,
                          pagina_atual=pagina_atual, base_q=base_q, **_catalogos(session))

    @app.get("/cobertura", response_class=HTMLResponse)
    def cobertura(request: Request, componente: str = "ISS_MUN"):
        with sessao() as session:
            usuario = exigir(request, session)
            dados_cob = cache.obter(session, ("cobertura", componente), lambda: consultas.cobertura(session, componente))
            componentes = list(session.scalars(select(cf.Componente).where(cf.Componente.ativo.is_(True)).order_by(cf.Componente.ordem)))
            return pagina(request, "cobertura.html", session, usuario, aba="cobertura", c=dados_cob, componente=componente,
                          componentes=componentes)

    @app.get("/regras", response_class=HTMLResponse)
    def regras(request: Request):
        with sessao() as session:
            usuario = exigir(request, session)
            conjunto = garantir_conjunto(session)
            conjuntos = list(session.scalars(select(cf.ConjuntoRegras).order_by(cf.ConjuntoRegras.id.desc())))
            execucoes = list(session.scalars(select(cf.Execucao).order_by(cf.Execucao.id.desc()).limit(10)))
            config_regras = json.loads(conjunto.parametros)
            for componente_id, padrao in SELECAO_INICIAL.items():
                config_regras.setdefault("selecao", {}).setdefault(componente_id, padrao)
            return pagina(request, "regras.html", session, usuario, aba="regras", conjunto=conjunto,
                          config=config_regras, conjuntos=conjuntos, contagem=consultas.contagem_regras(session),
                          execucoes=execucoes)

    @app.post("/regras")
    async def regras_salvar(request: Request):
        formulario = await request.form()
        with sessao() as session:
            usuario = exigir(request, session)
            atual = garantir_conjunto(session)
            config = json.loads(atual.parametros)
            for codigo in ORDEM:
                item = config["regras"].setdefault(codigo, {"parametros": {}})
                item["ativa"] = formulario.get(f"{codigo}__ativa") == "1"
                gravidade = formulario.get(f"{codigo}__gravidade")
                if gravidade in ("INFO", "BAIXA", "MEDIA", "ALTA"):
                    item["gravidade"] = gravidade
                for nome, padrao in REGRAS[codigo]["parametros"].items():
                    bruto = formulario.get(f"{codigo}__{nome}")
                    if bruto is None or bruto == "":
                        continue
                    if isinstance(padrao, str):
                        item["parametros"][nome] = str(bruto)
                    elif isinstance(padrao, int) and not isinstance(padrao, bool):
                        item["parametros"][nome] = int(float(str(bruto).replace(",", ".")))
                    else:
                        item["parametros"][nome] = float(str(bruto).replace(",", "."))
            for componente_id, padrao in SELECAO_INICIAL.items():
                precedencia = [p.strip() for p in str(formulario.get(f"precedencia__{componente_id}", "")).split(",") if p.strip()]
                item = config.setdefault("selecao", {}).setdefault(componente_id, json.loads(json.dumps(padrao)))
                if precedencia:
                    item["precedencia"] = precedencia
            corte = str(formulario.get("data_corte", "")).strip()
            config["data_corte"] = corte or None
            observacao = str(formulario.get("observacao", "")).strip() or "Parâmetros alterados pela tela de regras."
            if json.dumps(config, sort_keys=True) != json.dumps(json.loads(atual.parametros), sort_keys=True):
                atual.vigente = False
                session.add(cf.ConjuntoRegras(criado_em=datetime.now().replace(microsecond=0), criado_por=usuario.nome,
                                              parametros=json.dumps(config, ensure_ascii=False), observacao=observacao, vigente=True))
                session.commit()
                request.session["aviso"] = "Nova versão dos parâmetros registrada. Execute a validação para aplicá-la; as decisões existentes são preservadas."
            else:
                request.session["aviso"] = "Nenhum parâmetro mudou."
        return RedirectResponse("/regras", status_code=303)

    @app.post("/validar")
    def validar(request: Request):
        with sessao() as session:
            usuario = exigir(request, session)
            nome = usuario.nome
        try:
            tarefa_id = tarefas.iniciar(destino, "validar", {}, nome)
        except tarefas.Ocupado as erro:
            return JSONResponse({"ok": False, "erro": str(erro)}, status_code=409)
        return JSONResponse({"ok": True, "tarefa": tarefa_id})

    @app.post("/importar/bases")
    async def importar_bases(request: Request):
        """Recebe as planilhas de layout fixo (DCA, SIOPE, SIOPS, TCE) e importa em segundo plano."""
        from validacao.conferencia import bases_fixas

        formulario = await request.form()
        enviados = [f for f in formulario.getlist("arquivos") if getattr(f, "filename", "")]
        with sessao() as session:
            usuario = exigir(request, session)
            nome_usuario = usuario.nome
        if not enviados:
            return JSONResponse({"ok": False, "erro": "Escolha ao menos uma planilha."}, status_code=400)
        arquivos = [(f.filename, await f.read()) for f in enviados]
        try:
            ordenados = bases_fixas.ordenar(arquivos)  # recusa já aqui o que não for reconhecido
        except bases_fixas.LayoutDesconhecido as erro:
            return JSONResponse({"ok": False, "erro": str(erro)}, status_code=400)
        pendentes = pasta_importacoes / "pendentes"
        pendentes.mkdir(parents=True, exist_ok=True)
        lista = []
        for fonte, nome, conteudo in ordenados:
            caminho = pendentes / f"base-{secrets.token_hex(6)}-{fonte}.xlsx"
            caminho.write_bytes(conteudo)
            lista.append({"nome": nome, "caminho": str(caminho), "fonte": fonte})
        try:
            tarefa_id = tarefas.iniciar(destino, "importar_bases", {"arquivos": lista}, nome_usuario)
        except tarefas.Ocupado as erro:
            for item in lista:
                Path(item["caminho"]).unlink(missing_ok=True)
            return JSONResponse({"ok": False, "erro": str(erro)}, status_code=409)
        return JSONResponse({"ok": True, "tarefa": tarefa_id,
                             "reconhecidos": [bases_fixas.LAYOUTS[f]["nome"] for f, _n, _c in ordenados]})

    @app.post("/importar/siconfi/arquivos")
    async def importar_siconfi_arquivos(request: Request):
        """Recebe o pacote do SICONFI pela tela (CSV soltos ou um ZIP) e importa em segundo plano."""
        import shutil
        import tempfile

        from validacao.conferencia.pacote_siconfi import PacoteInvalido, receber

        formulario = await request.form()
        enviados = [f for f in formulario.getlist("arquivos") if getattr(f, "filename", "")]
        with sessao() as session:
            usuario = exigir(request, session)
            nome_usuario = usuario.nome
        if not enviados:
            return JSONResponse({"ok": False, "erro": "Escolha os arquivos do pacote."}, status_code=400)
        if tarefas.ativa_escrita(destino) is not None:
            return JSONResponse({"ok": False, "erro": "Já há uma importação ou validação em andamento."}, status_code=409)
        pasta_pacote_enviado.parent.mkdir(parents=True, exist_ok=True)
        recebidos = Path(tempfile.mkdtemp(prefix="envio-", dir=pasta_pacote_enviado.parent))
        try:
            caminhos = []
            for posicao, arquivo in enumerate(enviados):
                caminho = recebidos / f"{posicao}.bin"
                with open(caminho, "wb") as saida:
                    shutil.copyfileobj(arquivo.file, saida, 1024 * 1024)
                caminhos.append((Path(arquivo.filename).name, caminho))
            try:
                reconhecidos = receber(caminhos, pasta_pacote_enviado)
            except PacoteInvalido as erro:
                return JSONResponse({"ok": False, "erro": str(erro)}, status_code=400)
        finally:
            shutil.rmtree(recebidos, ignore_errors=True)
        try:
            tarefa_id = tarefas.iniciar(destino, "importar_siconfi", {"pasta": str(pasta_pacote_enviado)}, nome_usuario)
        except tarefas.Ocupado as erro:
            return JSONResponse({"ok": False, "erro": str(erro)}, status_code=409)
        from validacao.conferencia.pacote_siconfi import NOMES_ARQUIVO

        return JSONResponse({"ok": True, "tarefa": tarefa_id,
                             "reconhecidos": [NOMES_ARQUIVO[c] for c in ("base", "rreo", "msc", "cobertura", "status") if c in reconhecidos]})

    @app.post("/importar/siconfi")
    def importar_siconfi(request: Request):
        with sessao() as session:
            usuario = exigir(request, session)
            nome = usuario.nome
        try:
            tarefa_id = tarefas.iniciar(destino, "importar_siconfi", {"pasta": str(pasta_pacote())}, nome)
        except tarefas.Ocupado as erro:
            return JSONResponse({"ok": False, "erro": str(erro)}, status_code=409)
        return JSONResponse({"ok": True, "tarefa": tarefa_id})

    @app.post("/resultado/exportar/iniciar")
    async def exportar_iniciar(request: Request):
        formulario = await request.form()
        formato = str(formulario.get("formato", "csv"))
        if formato not in ("csv", "xlsx", "zip"):
            return JSONResponse({"ok": False, "erro": "Formato inválido."}, status_code=400)
        with sessao() as session:
            usuario = exigir(request, session)
            nome = usuario.nome
        tarefa_id = tarefas.iniciar(destino, "exportar", {"formato": formato, "query": str(formulario.get("query", ""))}, nome)
        return JSONResponse({"ok": True, "tarefa": tarefa_id})

    @app.get("/tarefas/{tarefa_id}")
    def tarefa_estado(request: Request, tarefa_id: str):
        with sessao() as session:
            exigir(request, session)
        if not tarefa_id.replace("-", "").isalnum():
            raise HTTPException(404)
        dados = tarefas.estado(destino, tarefa_id)
        if dados is None:
            raise HTTPException(404, "Tarefa não encontrada.")
        return JSONResponse({k: dados.get(k) for k in ("id", "tipo", "rodando", "mensagem", "erro", "execucao", "nome_arquivo")}
                            | {"pronto": bool(dados.get("arquivo")) and not dados.get("rodando")})

    @app.get("/tarefas/{tarefa_id}/arquivo")
    def tarefa_arquivo(request: Request, tarefa_id: str):
        from fastapi.responses import FileResponse

        with sessao() as session:
            exigir(request, session)
        dados = tarefas.estado(destino, tarefa_id) if tarefa_id.replace("-", "").isalnum() else None
        if not dados or not dados.get("arquivo") or not Path(dados["arquivo"]).is_file():
            raise HTTPException(404, "Arquivo não disponível.")
        caminho = Path(dados["arquivo"])
        if caminho.resolve().parent != tarefas.pasta(destino).resolve():
            raise HTTPException(404)
        return FileResponse(caminho, filename=dados.get("nome_arquivo") or caminho.name)

    pasta_pacote_enviado = destino.parent / "pacote_siconfi"

    def pasta_pacote() -> Path:
        """Pasta do pacote: a de bases ou a do último envio pela tela, a que tiver a base mais recente."""
        from validacao.db import PASTA_PLANILHAS
        from validacao.conferencia.pacote_siconfi import localizar, recebido_em

        local = PASTA_PLANILHAS if destino.resolve().parent == BANCO_PADRAO.resolve().parent else destino.parent
        enviado = recebido_em(pasta_pacote_enviado) if "base" in localizar(pasta_pacote_enviado) else None
        base_local = localizar(local).get("base") if local.is_dir() else None
        if enviado is not None and (base_local is None or enviado >= base_local.stat().st_mtime):
            return pasta_pacote_enviado  # o envio pela tela é mais recente que os arquivos da pasta
        return local

    cache_entes: dict = {}

    def lista_entes(session) -> list[dict]:
        if "itens" not in cache_entes:
            itens = []
            for e in session.scalars(select(Ente)):
                nome = _nome_ente(e.nome, e.uf)
                itens.append({"id": e.id_ente, "nome": nome, "uf": e.uf, "esfera": e.esfera, "capital": bool(e.capital),
                              "chave": _sem_acento(nome)})
            cache_entes["itens"] = itens
        return cache_entes["itens"]

    @app.get("/api/entes")
    def api_entes(request: Request, q: str = "", limite: int = 12):
        with sessao() as session:
            exigir(request, session)
            texto = q.strip()
            if not texto:
                return JSONResponse([])
            itens = lista_entes(session)
            if texto.isdigit():
                achados = [i for i in itens if str(i["id"]).startswith(texto)]
                achados.sort(key=lambda i: (len(str(i["id"])), i["id"]))
            else:
                chave = _sem_acento(texto)
                inicio = [i for i in itens if i["chave"].startswith(chave)]
                palavra = [i for i in itens if i not in inicio and f" {chave}" in f" {i['chave']}"]
                meio = [i for i in itens if chave in i["chave"] and i not in inicio and i not in palavra]
                ordem = lambda i: (not i["capital"], i["esfera"] == "MUNICIPIO", len(i["nome"]), i["nome"])  # noqa: E731
                achados = sorted(inicio, key=ordem) + sorted(palavra, key=ordem) + sorted(meio, key=ordem)
            esferas = {"MUNICIPIO": "Município", "ESTADO": "Estado", "DF": "Distrito Federal"}
            return JSONResponse([
                {"id": i["id"], "nome": i["nome"], "uf": i["uf"], "esfera": esferas.get(i["esfera"], i["esfera"]), "capital": i["capital"]}
                for i in achados[:max(1, min(limite, 30))]
            ])

    @app.get("/validar/estado")
    def validar_estado(request: Request):
        with sessao() as session:
            if usuario_atual(request, session) is None:
                return JSONResponse({"erro": "Sessão encerrada."}, status_code=401)
        return JSONResponse(tarefas.resumo(destino))

    @app.get("/fontes", response_class=HTMLResponse)
    def fontes(request: Request):
        with sessao() as session:
            usuario = exigir(request, session)
            declaracoes = list(session.scalars(select(cf.Declaracao).order_by(cf.Declaracao.ordem)))
            mapeamentos = list(session.scalars(select(cf.Mapeamento).order_by(cf.Mapeamento.declaracao_id, cf.Mapeamento.campo, cf.Mapeamento.versao)))
            componentes = list(session.scalars(select(cf.Componente).order_by(cf.Componente.ordem)))
            conjunto = garantir_conjunto(session)
            config_fontes = json.loads(conjunto.parametros)
            for componente_id, padrao in SELECAO_INICIAL.items():
                config_fontes.setdefault("selecao", {}).setdefault(componente_id, padrao)
            return pagina(request, "fontes.html", session, usuario, aba="fontes", declaracoes=declaracoes,
                          mapeamentos=mapeamentos, componentes=componentes, normas=NORMAS, pendencias=PENDENCIAS,
                          config=config_fontes)

    return app


def _sem_acento(texto: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn").lower()


def _nome_ente(nome: str, uf: str) -> str:
    from validacao.conferencia.execucao import nome_exibicao

    return nome_exibicao(nome, uf)


def _nome_arquivo(caminho) -> str:
    from pathlib import PureWindowsPath

    if not caminho:
        return ""
    return PureWindowsPath(str(caminho)).name if "\\" in str(caminho) else Path(str(caminho)).name


def _segredo(destino: Path) -> str:
    arquivo = destino.parent / ".segredo_sessao"
    try:
        if arquivo.exists():
            return arquivo.read_text(encoding="utf-8").strip()
        valor = secrets.token_hex(32)
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(valor, encoding="utf-8")
        return valor
    except OSError:
        return "conferencia-local"


def _selecao(session: Session) -> dict:
    conjunto = garantir_conjunto(session)
    return json.loads(conjunto.parametros).get("selecao", {}).get("ISS_MUN", {}) or SELECAO_INICIAL["ISS_MUN"]


def _catalogos(session: Session) -> dict:
    return {
        "declaracoes": list(session.scalars(select(cf.Declaracao).order_by(cf.Declaracao.ordem))),
        "componentes": list(session.scalars(select(cf.Componente).order_by(cf.Componente.ordem))),
    }


def _planilha_pendente(pasta: Path, token: str):
    if not token.isalnum():
        raise HTTPException(400, "Importação inválida.")
    base = pasta / "pendentes"
    conteudo = (base / f"{token}.bin").read_bytes()
    nome = json.loads((base / f"{token}.json").read_text(encoding="utf-8"))["nome"]
    return importacao.ler(nome, conteudo)


def _mapa_do_formulario(formulario, aba: str, cabecalho: int) -> importacao.Mapeamento:
    campos = []
    for indice in range(1, 9):
        coluna = str(formulario.get(f"valor_coluna_{indice}") or "").strip()
        if not coluna:
            continue
        campo = str(formulario.get(f"valor_campo_{indice}") or "").strip() or coluna
        parcela = str(formulario.get(f"valor_parcela_{indice}") or "TOTAL")
        conceito = str(formulario.get(f"valor_conceito_{indice}") or "INDEFINIDO")
        campos.append(importacao.CampoValor(coluna=coluna, campo=campo.upper().replace(" ", "_")[:80], parcela=parcela, conceito=conceito))
    data_texto = str(formulario.get("data_extracao") or "").strip()
    data_extracao = datetime.fromisoformat(data_texto) if data_texto else None
    exercicio_fixo = str(formulario.get("exercicio_fixo") or "").strip()
    conceitos_texto = {}
    for linha in str(formulario.get("conceitos_por_texto") or "").splitlines():
        if "=" in linha:
            chave, valor = linha.split("=", 1)
            conceitos_texto[chave.strip()] = valor.strip().upper()
    return importacao.Mapeamento(
        declaracao=str(formulario.get("declaracao")), componente=str(formulario.get("componente") or "ISS_MUN"), aba=aba,
        linha_cabecalho=cabecalho, coluna_ente=str(formulario.get("coluna_ente") or ""),
        coluna_exercicio=str(formulario.get("coluna_exercicio") or "") or None,
        exercicio_fixo=int(exercicio_fixo) if exercicio_fixo else None, campos=campos,
        periodo=str(formulario.get("periodo") or "ANUAL"), escala=int(formulario.get("escala") or 1),
        data_extracao=data_extracao, coluna_conceito=str(formulario.get("coluna_conceito") or "") or None,
        conceitos_por_texto=conceitos_texto, observacao=str(formulario.get("observacao") or ""),
    )


def _resumo_resultado(session: Session, execucao, componente: str) -> dict:
    from validacao.conferencia.consultas import expressao_situacao
    from validacao.conferencia.decisao import junta_decisao

    situacao = expressao_situacao()
    linhas = session.execute(
        select(situacao, func.count(), func.sum(cf.Caso.valor_proposto), func.sum(cf.DecisaoCaso.valor_adotado))
        .select_from(cf.Caso)
        .outerjoin(cf.DecisaoCaso, junta_decisao())
        .where(cf.Caso.execucao_id == execucao.id, cf.Caso.diagnostico != "EXCLUIDO",
               *([cf.Caso.componente_id == componente] if componente else []))
        .group_by(situacao)
    ).all()
    por_situacao = {s: {"casos": n, "proposto": p, "adotado": a} for s, n, p, a in linhas}
    diferencas = session.execute(
        select(cf.Caso.exercicio, func.count(), func.sum(cf.DecisaoCaso.valor_adotado - cf.Caso.valor_principal))
        .join(cf.DecisaoCaso, junta_decisao())
        .where(cf.Caso.execucao_id == execucao.id, *([cf.Caso.componente_id == componente] if componente else []),
               cf.DecisaoCaso.valor_adotado.is_not(None), cf.Caso.valor_principal.is_not(None),
               cf.DecisaoCaso.valor_adotado != cf.Caso.valor_principal)
        .group_by(cf.Caso.exercicio)
    ).all()
    return {"por_situacao": por_situacao, "diferencas": diferencas}


def _contexto_caso(session: Session, caso: cf.Caso) -> dict:
    info = consultas.detalhe(session, caso)
    decisao = decisao_vigente(session, caso.id_ente, caso.exercicio, caso.componente_id)
    ordem = [d for d, in session.execute(select(cf.Declaracao.id).order_by(cf.Declaracao.ordem))]
    presentes = [d for d in ordem if d in info["matriz"]]
    execucao = session.get(cf.Execucao, caso.execucao_id)
    conjunto = session.get(cf.ConjuntoRegras, execucao.conjunto_id) if execucao else None
    config = json.loads(conjunto.parametros) if conjunto else {}
    for componente_id, padrao in SELECAO_INICIAL.items():
        config.setdefault("selecao", {}).setdefault(componente_id, padrao)
    totais_vigentes = [
        (r, l) for r, l in info["registros"]
        if r.vigente and r.parcela == "TOTAL" and r.valor_centavos is not None and r.declaracao_id != caso.declaracao_principal
    ]
    principal_padrao = (config.get("selecao", {}).get(caso.componente_id, {}).get("precedencia") or ["SICONFI_DCA"])[0]
    from validacao.conferencia.composicao import composicoes
    from validacao.conferencia.evidencia import dados_alertas
    from validacao.conferencia.pacote_siconfi import detalhes_do_caso

    caminho_detalhes = configuracao(session, "detalhes_siconfi")
    if not caminho_detalhes or not Path(caminho_detalhes).is_file():
        # banco levado para outra máquina ou para o Docker: o arquivo de detalhes fica ao lado do banco
        bind = session.get_bind()
        ao_lado = Path(bind.url.database).parent / "detalhes_siconfi.sqlite" if bind.url.database else None
        caminho_detalhes = str(ao_lado) if ao_lado and ao_lado.is_file() else caminho_detalhes
    detalhes = detalhes_do_caso(caminho_detalhes, caso.id_ente, caso.exercicio, caso.componente_id)
    componente = session.get(cf.Componente, caso.componente_id)
    return {
        "caso": caso,
        "info": info,
        "decisao": decisao,
        "situacao": situacao_do_caso(caso, decisao),
        "historico": historico(session, caso.id_ente, caso.exercicio, caso.componente_id),
        "ordem": presentes,
        "grafico": serie_svg(info["matriz"], EXERCICIOS, caso.exercicio, presentes),
        "legenda": legenda(presentes, info["matriz"]),
        "execucao": execucao,
        "config": config,
        "totais_vigentes": totais_vigentes,
        "principal_padrao": principal_padrao,
        "componente": componente,
        "declaracoes": {d.id: d for d in session.scalars(select(cf.Declaracao))},
        "detalhes": detalhes,
        "dados_alertas": dados_alertas(session, caso, info),
        "composicoes": composicoes(info, detalhes, presentes),
        "consultas_caso": list(session.scalars(select(cf.Consulta).where(cf.Consulta.id_ente == caso.id_ente, cf.Consulta.ano == caso.exercicio))),
    }
