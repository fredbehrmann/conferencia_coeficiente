import json
from contextlib import asynccontextmanager
from math import ceil
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from validacao.apresentacao import ROTULOS, SITUACOES, montar_ficha, nome_curto, reais
from validacao.apuracao import (
    BLOCOS_INICIAIS,
    CongelamentoRecusado,
    apurar,
    comparar,
    congelar,
    exportar,
    garantir_formula,
    gravar_formula,
    impedimentos,
)
from validacao.cargas import carregar_pasta
from validacao.catalogo import semear
from validacao.db import BANCO_PADRAO, PASTA_FONTES, PASTA_PLANILHAS, criar_engine, criar_tabelas
from validacao.decisoes import (
    TAMANHO_PAGINA,
    DecisaoRecusada,
    aceitar_lote,
    cancelar_regra,
    confirmar,
    confirmar_regra,
    consultar_fila,
    consultar_revisao,
    contar_lote,
    prever_regra,
    reabrir_alterados,
    registrar,
    situacoes_de,
)
from validacao.modelos import Apuracao, BaseLinha, BaseVersao, Decisao, Ente, Usuario, Verificacao
from validacao.motor import VERMELHAS, reprocessar
from validacao.operacao import definir_intervalo, garantir_agenda, painel as montar_painel
from validacao.regras import PARAMETROS_INICIAIS, garantir_regra, parametros_de

PASTA_TEMPLATES = Path(__file__).resolve().parent / "templates"
PERFIS = ("ANALISTA", "REVISOR", "ADMINISTRADOR")
ESFERAS = (("MUNICIPIO", "Município"), ("ESTADO", "Estado"), ("DF", "Distrito Federal"))


def criar_app(banco: Path | None = None) -> FastAPI:
    destino = Path(banco) if banco is not None else BANCO_PADRAO

    @asynccontextmanager
    async def vida(app: FastAPI):
        engine = criar_engine(destino)
        criar_tabelas(engine)
        with Session(engine) as session:
            semear(session)
            garantir_agenda(session)
            session.commit()
        app.state.engine = engine
        yield

    app = FastAPI(lifespan=vida)
    app.add_middleware(SessionMiddleware, secret_key="validacao-local", same_site="lax")
    modelos = Jinja2Templates(directory=str(PASTA_TEMPLATES))
    modelos.env.filters["reais"] = reais
    modelos.env.filters["inteiro"] = lambda valor: f"{int(valor):,}".replace(",", ".")

    def banco_sessao():
        return Session(app.state.engine)

    def pagina(request: Request, nome: str, status: int = 200, **contexto):
        contexto.setdefault("erro", request.session.pop("erro", None))
        contexto.setdefault("aviso", request.session.pop("aviso", None))
        contexto.setdefault("usuario", contexto.get("usuario"))
        contexto.setdefault("titulo", "Coeficiente")
        return modelos.TemplateResponse(request, nome, contexto, status_code=status)

    def exigir(request: Request, session: Session, perfis: tuple[str, ...] | None = None):
        usuario_id = request.session.get("usuario_id")
        usuario = session.get(Usuario, usuario_id) if usuario_id else None
        if usuario is None:
            return RedirectResponse("/login", status_code=303)
        if perfis and usuario.perfil not in perfis:
            raise HTTPException(status_code=403, detail="Este perfil não faz esta ação.")
        return usuario

    @app.get("/login")
    def login_formulario(request: Request):
        return pagina(request, "login.html", titulo="Entrar")

    @app.post("/login")
    def login(request: Request, nome: str = Form(), perfil: str = Form()):
        nome = nome.strip()
        if not nome or perfil not in PERFIS:
            return pagina(request, "login.html", status=400, titulo="Entrar", erro="Informe o nome e o perfil.")
        with banco_sessao() as session:
            usuario = session.scalar(select(Usuario).where(Usuario.nome == nome))
            if usuario is None:
                usuario = Usuario(nome=nome, perfil=perfil)
                session.add(usuario)
                session.commit()
            elif usuario.perfil != perfil:
                return pagina(
                    request,
                    "login.html",
                    status=400,
                    titulo="Entrar",
                    erro=f"Este nome já entrou como {usuario.perfil}.",
                )
            request.session["usuario_id"] = usuario.id
        return RedirectResponse("/fila", status_code=303)

    @app.post("/sair")
    def sair(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    def inicio():
        return RedirectResponse("/fila", status_code=303)

    @app.get("/fila")
    def fila(
        request: Request,
        esfera: str = "",
        uf: str = "",
        ano: str = "",
        situacao: str = "",
        pagina_n: int = 1,
    ):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            regra = garantir_regra(session)
            session.commit()
            filtros = _filtros(esfera, uf, ano, situacao)
            linhas, total = consultar_fila(session, regra.id, pagina=pagina_n, **filtros)
            for verificacao, _ente in linhas:
                codigos = situacoes_de(verificacao.situacoes)
                verificacao.rotulo_bloco = ROTULOS.get(verificacao.bloco, verificacao.bloco)
                verificacao.chips = [
                    {"rotulo": SITUACOES[codigo], "grave": codigo in VERMELHAS}
                    for codigo in SITUACOES
                    if codigo in codigos
                ]
            lote = contar_lote(session, regra.id, **filtros) if usuario.perfil == "ANALISTA" else 0
            paginas = max(ceil(total / TAMANHO_PAGINA), 1)
            return pagina(
                request,
                "fila.html",
                titulo="Fila",
                usuario=usuario,
                linhas=linhas,
                total=total,
                lote=lote,
                filtros={"esfera": esfera, "uf": uf, "ano": ano, "situacao": situacao},
                esferas=ESFERAS,
                situacoes=SITUACOES.items(),
                pagina=pagina_n,
                paginas=paginas,
                anterior=_link_fila(filtros_texto(esfera, uf, ano, situacao), pagina_n - 1),
                seguinte=_link_fila(filtros_texto(esfera, uf, ano, situacao), pagina_n + 1),
            )

    @app.post("/fila/aceitar")
    def aceitar(
        request: Request,
        esfera: str = Form(""),
        uf: str = Form(""),
        ano: str = Form(""),
        situacao: str = Form(""),
    ):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ANALISTA",))
            if not isinstance(usuario, Usuario):
                return usuario
            regra = garantir_regra(session)
            quantidade = aceitar_lote(session, regra, usuario.id, **_filtros(esfera, uf, ano, situacao))
            session.commit()
        request.session["aviso"] = f"{quantidade} blocos aceitos."
        return RedirectResponse("/fila?" + urlencode(filtros_texto(esfera, uf, ano, situacao)), status_code=303)

    @app.get("/busca")
    def busca(request: Request, q: str = ""):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            termo = q.strip()
            entes = _buscar(session, termo) if termo else []
            if len(entes) == 1:
                return RedirectResponse(f"/ente/{entes[0].id_ente}", status_code=303)
            return pagina(request, "busca.html", titulo="Busca", usuario=usuario, termo=termo, entes=entes)

    @app.get("/ente/{id_ente}")
    def ficha(request: Request, id_ente: int):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            ficha_ente = montar_ficha(session, id_ente)
            if ficha_ente is None:
                raise HTTPException(status_code=404, detail="Ente não encontrado.")
            session.commit()
            return pagina(
                request,
                "ficha.html",
                titulo=ficha_ente["curto"],
                usuario=usuario,
                ficha=ficha_ente,
            )

    @app.post("/ente/{id_ente}/decidir")
    def decidir(
        request: Request,
        id_ente: int,
        ano: int = Form(),
        bloco: str = Form(),
        acao: str = Form(),
        fonte: str = Form(""),
        valor: str = Form(""),
        motivo: str = Form(""),
    ):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ANALISTA",))
            if not isinstance(usuario, Usuario):
                return usuario
            try:
                registrar(
                    session,
                    id_ente,
                    ano,
                    bloco,
                    acao,
                    usuario.id,
                    fonte or None,
                    float(valor) if valor else None,
                    motivo,
                )
                session.commit()
            except DecisaoRecusada as erro:
                request.session["erro"] = str(erro)
                return RedirectResponse(f"/ente/{id_ente}", status_code=303)
        return RedirectResponse(f"/ente/{id_ente}", status_code=303)

    @app.post("/decisao/{decisao_id}/confirmar")
    def confirmar_decisao(request: Request, decisao_id: int, origem: str = Form("")):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("REVISOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            try:
                decisao = confirmar(session, decisao_id, usuario.id)
                session.commit()
                destino = decisao.id_ente
            except DecisaoRecusada as erro:
                request.session["erro"] = str(erro)
                decisao = session.get(Decisao, decisao_id)
                destino = decisao.id_ente if decisao else ""
        if origem == "revisao":
            return RedirectResponse("/revisao", status_code=303)
        return RedirectResponse(f"/ente/{destino}", status_code=303)

    @app.get("/ente/{id_ente}/municipios")
    def municipios(request: Request, id_ente: int, q: str = ""):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            estado = session.get(Ente, id_ente)
            if estado is None or estado.esfera != "ESTADO":
                raise HTTPException(status_code=404, detail="Estado não encontrado.")
            consulta = select(Ente).where(Ente.esfera == "MUNICIPIO", Ente.uf == estado.uf)
            if q.strip():
                consulta = consulta.where(Ente.nome.contains(q.strip()))
            entes = session.scalars(consulta.order_by(Ente.nome).limit(100)).all()
            return pagina(
                request,
                "municipios.html",
                titulo=f"Municípios de {estado.uf}",
                usuario=usuario,
                curto=nome_curto(estado.nome),
                entes=entes,
                termo=q,
            )

    @app.get("/uf")
    def ufs(request: Request):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            regra = garantir_regra(session)
            session.commit()
            pares = session.execute(
                select(Verificacao, Ente)
                .join(Ente, Ente.id_ente == Verificacao.id_ente)
                .where(
                    Verificacao.regra_versao_id == regra.id,
                    Verificacao.bloco == "COTA_REPASSADA",
                    Ente.esfera == "ESTADO",
                    Verificacao.situacoes.contains('"UF_NAO_FECHA"'),
                )
                .order_by(Ente.uf, Verificacao.ano)
            ).all()
            pares_sefaz = []
            publicados = session.execute(
                select(Verificacao, Ente)
                .join(Ente, Ente.id_ente == Verificacao.id_ente)
                .where(
                    Verificacao.regra_versao_id == regra.id,
                    Verificacao.bloco == "COTA_REPASSADA",
                    Ente.esfera == "ESTADO",
                )
                .order_by(Ente.uf, Verificacao.ano)
            ).all()
            for linha, ente in publicados:
                info = json.loads(linha.diferencas).get("SEFAZ_UF")
                if not info or info.get("valor") is None:
                    continue
                pares_sefaz.append((ente, linha.ano, info["valor"], linha.valor_referencia))
            return pagina(
                request,
                "uf.html",
                titulo="UFs",
                usuario=usuario,
                pares=pares,
                pares_sefaz=pares_sefaz,
            )

    @app.get("/uf/{uf}/{ano}")
    def uf_ano(request: Request, uf: str, ano: int):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            regra = garantir_regra(session)
            estado = session.scalar(select(Ente).where(Ente.esfera == "ESTADO", Ente.uf == uf))
            if estado is None:
                raise HTTPException(status_code=404, detail="UF não encontrada.")
            linhas = session.execute(
                select(Verificacao, Ente)
                .join(Ente, Ente.id_ente == Verificacao.id_ente)
                .where(
                    Verificacao.regra_versao_id == regra.id,
                    Verificacao.ano == ano,
                    Verificacao.bloco == "COTA_RECEBIDA",
                    Ente.esfera == "MUNICIPIO",
                    Ente.uf == uf,
                )
                .order_by(Ente.nome)
            ).all()
            session.commit()
            return pagina(
                request,
                "uf_ano.html",
                titulo=f"{uf} {ano}",
                usuario=usuario,
                uf=uf,
                ano=ano,
                estado=estado,
                linhas=linhas,
            )

    @app.get("/regras")
    def regras(request: Request):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            regra = garantir_regra(session)
            session.commit()
            return pagina(
                request,
                "regras.html",
                titulo="Regras",
                usuario=usuario,
                regra=regra,
                params=parametros_de(regra),
                anos_possiveis=PARAMETROS_INICIAIS["anos"],
            )

    @app.post("/regras/prever")
    def prever(
        request: Request,
        fundeb_permanece: str = Form(""),
        compensacao_soma: str = Form(""),
        iss_simples_soma: str = Form(""),
        igualdade_absoluta: float = Form(),
        igualdade_relativa: float = Form(),
        aceite_sugerido: float = Form(),
        fila_obrigatoria: float = Form(),
        fundeb_liquido_min: float = Form(),
        fundeb_liquido_max: float = Form(),
        salto_min: float = Form(),
        salto_max: float = Form(),
        revisao_corte: str = Form(""),
        anos: list[str] | None = Form(default=None),
    ):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            parametros = {
                "anos": [int(ano) for ano in (anos or [])],
                "igualdade_absoluta": igualdade_absoluta,
                "igualdade_relativa": igualdade_relativa,
                "aceite_sugerido": aceite_sugerido,
                "fila_obrigatoria": fila_obrigatoria,
                "fundeb_liquido_min": fundeb_liquido_min,
                "fundeb_liquido_max": fundeb_liquido_max,
                "salto_min": salto_min,
                "salto_max": salto_max,
                "fundeb_permanece": fundeb_permanece == "on",
                "compensacao_soma": compensacao_soma == "on",
                "iss_simples_soma": iss_simples_soma == "on",
                "revisao_corte": float(revisao_corte) if revisao_corte.strip() else None,
            }
            regra, efeito = prever_regra(session, parametros)
            session.commit()
            return pagina(
                request,
                "previsao.html",
                titulo="Regra nova",
                usuario=usuario,
                regra_id=regra.id,
                entram=efeito["entram"],
                saem=efeito["saem"],
            )

    @app.post("/regras/{regra_id}/confirmar")
    def confirmar_versao(request: Request, regra_id: int):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            confirmar_regra(session, regra_id)
            session.commit()
        return RedirectResponse("/regras", status_code=303)

    @app.post("/regras/{regra_id}/cancelar")
    def cancelar_versao(request: Request, regra_id: int):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            cancelar_regra(session, regra_id)
            session.commit()
        return RedirectResponse("/regras", status_code=303)

    @app.post("/carregar")
    def carregar(request: Request):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
        carregar_pasta(app.state.engine, PASTA_PLANILHAS)
        with banco_sessao() as session:
            regra = garantir_regra(session)
            session.commit()
            reprocessar(session, regra)
            reabertos = reabrir_alterados(session)
            session.commit()
        request.session["aviso"] = f"Planilhas relidas. {reabertos} blocos reabertos."
        return RedirectResponse("/fila", status_code=303)

    @app.post("/complementar")
    def complementar(request: Request):
        from validacao.cargas.complementar import carregar_complementares

        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            if session.scalar(select(Verificacao.id).limit(1)) is None:
                regra = garantir_regra(session)
                session.commit()
                reprocessar(session, regra)
                session.commit()
        carregar_complementares(app.state.engine, PASTA_FONTES)
        with banco_sessao() as session:
            regra = garantir_regra(session)
            session.commit()
            reprocessar(session, regra)
            reabertos = reabrir_alterados(session)
            session.commit()
        request.session["aviso"] = f"Fontes complementares carregadas. {reabertos} blocos reabertos."
        return RedirectResponse("/fila", status_code=303)

    @app.get("/revisao")
    def revisao(request: Request, pagina_n: int = 1):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            linhas, total = consultar_revisao(session, pagina_n)
            paginas = max(ceil(total / TAMANHO_PAGINA), 1)
            return pagina(
                request,
                "revisao.html",
                titulo="Revisão",
                usuario=usuario,
                linhas=linhas,
                total=total,
                pagina=pagina_n,
                paginas=paginas,
                anterior=f"/revisao?pagina_n={pagina_n - 1}",
                seguinte=f"/revisao?pagina_n={pagina_n + 1}",
            )

    @app.get("/bases")
    def bases(request: Request):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            formula = garantir_formula(session)
            versoes = session.scalars(select(BaseVersao).order_by(BaseVersao.id.desc())).all()
            contagem = impedimentos(session)
            session.commit()
            return pagina(
                request,
                "bases.html",
                titulo="Bases",
                usuario=usuario,
                versoes=versoes,
                formula=formula,
                impedimentos=contagem,
                anos=PARAMETROS_INICIAIS["anos"],
            )

    @app.post("/bases/congelar")
    def congelar_base(request: Request):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            try:
                versao = congelar(session, usuario.id)
                session.commit()
            except CongelamentoRecusado as erro:
                request.session["erro"] = str(erro)
                return RedirectResponse("/bases", status_code=303)
        request.session["aviso"] = f"Base {versao.id} congelada."
        return RedirectResponse(f"/bases/{versao.id}", status_code=303)

    @app.post("/formulas")
    def salvar_formula(
        request: Request,
        descricao: str = Form(),
        fecha_em: float = Form(),
        casas: int = Form(),
        fatores: str = Form(""),
        incluir_simples: str = Form(""),
        incluir_compensacao: str = Form(""),
        anos: list[str] | None = Form(default=None),
    ):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            try:
                gravar_formula(session, descricao, _parametros_formula(
                    anos, fecha_em, casas, fatores, incluir_simples, incluir_compensacao
                ))
                session.commit()
            except ValueError as erro:
                request.session["erro"] = str(erro)
        return RedirectResponse("/bases", status_code=303)

    @app.get("/bases/{base_id}")
    def base_detalhe(request: Request, base_id: int, pagina_n: int = 1):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            versao = session.get(BaseVersao, base_id)
            if versao is None:
                raise HTTPException(status_code=404, detail="Base não encontrada.")
            consulta = select(BaseLinha).where(BaseLinha.base_versao_id == base_id)
            total = session.scalar(select(func.count()).select_from(consulta.subquery())) or 0
            inicio = max(pagina_n - 1, 0) * TAMANHO_PAGINA
            linhas = session.scalars(
                consulta.order_by(BaseLinha.id_ente, BaseLinha.ano, BaseLinha.bloco).offset(inicio).limit(TAMANHO_PAGINA)
            ).all()
            return pagina(
                request,
                "base_versao.html",
                titulo=f"Base {base_id}",
                usuario=usuario,
                versao=versao,
                linhas=linhas,
                pagina=pagina_n,
                paginas=max(ceil(total / TAMANHO_PAGINA), 1),
            )

    @app.get("/bases/{base_id}/exportar")
    def exportar_base(request: Request, base_id: int, formato: str = "csv"):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            formula = garantir_formula(session)
            try:
                destino = exportar(session, base_id, formula.id, formato)
                session.commit()
            except ValueError as erro:
                request.session["erro"] = str(erro)
                return RedirectResponse(f"/bases/{base_id}", status_code=303)
        tipo = "text/csv" if formato == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return FileResponse(destino, media_type=tipo, filename=destino.name)

    @app.get("/apuracao/{base_id}")
    def coeficientes(request: Request, base_id: int):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            base = session.get(BaseVersao, base_id)
            if base is None:
                raise HTTPException(status_code=404, detail="Base não encontrada.")
            formula = garantir_formula(session)
            coeficientes_ente = session.execute(
                select(Apuracao, Ente)
                .join(Ente, Ente.id_ente == Apuracao.id_ente)
                .where(Apuracao.base_versao_id == base_id, Apuracao.formula_versao_id == formula.id)
                .order_by(Ente.esfera, Ente.nome)
            ).all()
            session.commit()
            return pagina(
                request,
                "coeficientes.html",
                titulo="Coeficientes",
                usuario=usuario,
                base=base,
                formula=formula,
                coeficientes=coeficientes_ente,
            )

    @app.post("/apuracao/{base_id}")
    def apurar_base(request: Request, base_id: int):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            formula = garantir_formula(session)
            apurar(session, base_id, formula.id)
            session.commit()
        return RedirectResponse(f"/apuracao/{base_id}", status_code=303)

    @app.get("/apuracao/{base_id}/ente/{id_ente}")
    def memoria(request: Request, base_id: int, id_ente: int):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            formula = garantir_formula(session)
            apuracao = session.scalar(
                select(Apuracao).where(
                    Apuracao.base_versao_id == base_id,
                    Apuracao.formula_versao_id == formula.id,
                    Apuracao.id_ente == id_ente,
                )
            )
            ente = session.get(Ente, id_ente)
            if apuracao is None or ente is None:
                raise HTTPException(status_code=404, detail="Coeficiente não encontrado.")
            return pagina(
                request,
                "memoria.html",
                titulo=nome_curto(ente.nome),
                usuario=usuario,
                ente=ente,
                apuracao=apuracao,
                memoria=json.loads(apuracao.memoria),
            )

    @app.get("/comparar")
    def comparar_bases(request: Request, a: int, b: int):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            formula = garantir_formula(session)
            mudancas = comparar(session, a, b, formula.id)
            session.commit()
            return pagina(
                request,
                "comparar.html",
                titulo="Comparação",
                usuario=usuario,
                a=a,
                b=b,
                mudancas=mudancas,
            )

    @app.get("/painel")
    def acompanhamento(request: Request):
        with banco_sessao() as session:
            usuario = exigir(request, session)
            if not isinstance(usuario, Usuario):
                return usuario
            quadro = montar_painel(session)
            session.commit()
            return pagina(request, "painel.html", titulo="Painel", usuario=usuario, painel=quadro)

    @app.post("/painel/agenda")
    def agenda(request: Request, fonte_id: str = Form(), intervalo_dias: int = Form()):
        with banco_sessao() as session:
            usuario = exigir(request, session, ("ADMINISTRADOR",))
            if not isinstance(usuario, Usuario):
                return usuario
            definir_intervalo(session, fonte_id, intervalo_dias)
            session.commit()
        return RedirectResponse("/painel", status_code=303)

    return app


def _parametros_formula(anos, fecha_em, casas, fatores_texto, incluir_simples, incluir_compensacao) -> dict:
    blocos = {esfera: list(itens) for esfera, itens in BLOCOS_INICIAIS.items()}
    if incluir_simples == "on":
        blocos["MUNICIPIO"].append("ISS_SIMPLES")
        blocos["DF"].append("ISS_SIMPLES")
    if incluir_compensacao == "on":
        for esfera in blocos:
            blocos[esfera].append("COMPENSACAO")
    fatores = {}
    for linha in (fatores_texto or "").splitlines():
        if "=" not in linha:
            continue
        ano, fator = linha.split("=", 1)
        fatores[int(ano.strip())] = float(fator.replace(",", ".").strip())
    return {
        "anos": [int(ano) for ano in (anos or [])],
        "media": "aritmetica",
        "fatores": fatores,
        "agregacao": "por_esfera",
        "fecha_em": float(fecha_em),
        "casas": int(casas),
        "blocos": blocos,
    }


def _filtros(esfera: str, uf: str, ano: str, situacao: str) -> dict:
    return {
        "esfera": esfera or None,
        "uf": uf.strip().upper() or None,
        "ano": int(ano) if str(ano).isdigit() else None,
        "situacao": situacao or None,
    }


def filtros_texto(esfera: str, uf: str, ano: str, situacao: str) -> dict:
    return {chave: valor for chave, valor in {"esfera": esfera, "uf": uf, "ano": ano, "situacao": situacao}.items() if valor}


def _link_fila(filtros: dict, pagina_n: int) -> str:
    dados = dict(filtros)
    dados["pagina_n"] = pagina_n
    return "/fila?" + urlencode(dados)


def _buscar(session: Session, termo: str) -> list[Ente]:
    if termo.isdigit():
        ente = session.get(Ente, int(termo))
        if ente is not None:
            return [ente]
    return list(
        session.scalars(select(Ente).where(Ente.nome.contains(termo)).order_by(Ente.nome).limit(30)).all()
    )
