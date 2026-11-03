import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from validacao.catalogo import COMPONENTES, ROTULOS_INDEPENDENCIA
from validacao.decisoes import exige_revisao, fecha_limpo, siconfi_bloqueado, situacoes_de
from validacao.modelos import Carga, Decisao, Ente, EntregaDeclaracao, Fonte, Usuario, ValorObservado, Verificacao
from validacao.motor import BLOCOS_DA_FONTE, BLOCOS_POR_ESFERA, FONTES, NAO_APLICA, TOTAL_DO_BLOCO, VERMELHAS, _so_bruto
from validacao.referencia import iguais
from validacao.regras import garantir_regra, parametros_de

ROTULOS = {
    "ISS": "ISS",
    "COTA_RECEBIDA": "cota-parte",
    "ICMS": "ICMS",
    "COTA_REPASSADA": "cota repassada",
    "COMPENSACAO": "compensação",
    "ISS_SIMPLES": "ISS do Simples",
}
ARTIGOS = {
    "ISS": "O",
    "COTA_RECEBIDA": "A",
    "ICMS": "O",
    "COTA_REPASSADA": "A",
    "COMPENSACAO": "A",
    "ISS_SIMPLES": "O",
}
SITUACOES = {
    "FECHA": "fecha",
    "DIVERGE_1_5": "difere até 5%",
    "DIVERGE_ACIMA_5": "difere acima de 5%",
    "SEM_ABERTURA": "sem abertura",
    "VALOR_REPETIDO": "valor repetido",
    "SALTO_SERIE": "salto na série",
    "VAZIO_COM_VALOR_EXTERNO": "vazio com valor externo",
    "NEGATIVO": "negativo",
    "SEM_DECLARACAO": "sem declaração",
    "ITENS_NAO_SOMAM": "itens não somam",
    "FONTE_LIQUIDA_FUNDEB": "líquido de FUNDEB",
    "FORA_COBERTURA": "fora da cobertura",
    "NAO_SE_APLICA": "não se aplica",
    "UF_NAO_FECHA": "UF não fecha",
}
MARCAS = {"SOMA": "entra", "SUBTRAI": "sai", "INFORMATIVO": "permanece"}
PREFIXOS = (
    "Prefeitura Municipal de ",
    "Governo do Estado do ",
    "Governo do Estado de ",
    "Governo do ",
)


def reais(valor) -> str:
    if valor is None:
        return "—"
    sinal = "-" if valor < 0 else ""
    texto = f"{abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {sinal}{texto}"


def nome_curto(nome: str) -> str:
    texto = nome
    for prefixo in PREFIXOS:
        if texto.startswith(prefixo):
            texto = texto[len(prefixo) :]
            break
    if " - " in texto:
        texto = texto.split(" - ", 1)[0]
    return texto


def pergunta(bloco: str, nome: str, ano: int) -> str:
    return f"{ARTIGOS[bloco]} {ROTULOS[bloco]} de {nome} em {ano} entra no cálculo?"


def rotulos_situacao(situacoes: set[str]) -> list[str]:
    return [SITUACOES[codigo] for codigo in SITUACOES if codigo in situacoes]


def montar_ficha(session: Session, id_ente: int) -> dict | None:
    ente = session.get(Ente, id_ente)
    if ente is None:
        return None
    regra = garantir_regra(session)
    params = parametros_de(regra)
    verificacoes = {
        (linha.ano, linha.bloco): linha
        for linha in session.scalars(
            select(Verificacao).where(
                Verificacao.regra_versao_id == regra.id,
                Verificacao.id_ente == id_ente,
            )
        )
    }
    historico = list(
        session.execute(
            select(Decisao, Usuario.nome)
            .join(Usuario, Usuario.id == Decisao.usuario_id)
            .where(Decisao.id_ente == id_ente)
            .order_by(Decisao.data.desc(), Decisao.id.desc())
        ).all()
    )
    valores = _valores_do_ente(session, id_ente)
    independencias = {
        fonte.id: ROTULOS_INDEPENDENCIA.get(fonte.independencia, fonte.independencia)
        for fonte in session.scalars(select(Fonte))
    }
    entregas = {
        (linha.id_ente, linha.ano): linha.entregue
        for linha in session.scalars(
            select(EntregaDeclaracao).where(
                EntregaDeclaracao.id_ente == id_ente,
                EntregaDeclaracao.declaracao == "DCA",
            )
        )
    }
    blocos = list(BLOCOS_POR_ESFERA[ente.esfera]) + list(NAO_APLICA.get(ente.esfera, ()))
    curto = nome_curto(ente.nome)
    secoes = []
    for ano in params["anos"]:
        blocos_ano = []
        for bloco in blocos:
            linha = verificacoes.get((ano, bloco))
            blocos_ano.append(
                _bloco(ente, curto, ano, bloco, linha, historico, valores, params, independencias, entregas)
            )
        secoes.append(
            {
                "ano": ano,
                "blocos": blocos_ano,
                "pendentes": sum(
                    1
                    for item in blocos_ano
                    if item.get("aplicavel") and not item.get("ausente") and not item.get("vigente")
                ),
                "decididos": sum(1 for item in blocos_ano if item.get("vigente")),
            }
        )
    estado = None
    if ente.esfera == "MUNICIPIO":
        estado = session.scalar(
            select(Ente).where(Ente.esfera == "ESTADO", Ente.uf == ente.uf)
        )
    return {
        "ente": ente,
        "curto": curto,
        "secoes": secoes,
        "pendentes": sum(secao["pendentes"] for secao in secoes),
        "estado": estado,
        "estado_curto": nome_curto(estado.nome) if estado else None,
    }


def frase_fonte(fonte, info, referencia, situacoes, params, entregue=None, msc_sem_itens=False) -> str:
    if "NAO_SE_APLICA" in situacoes:
        return "não se aplica"
    if fonte == "SICONFI" and entregue is False:
        return "não entregou a DCA"
    if fonte == "MSC" and msc_sem_itens and "SEM_ABERTURA" in situacoes:
        return "a MSC também não traz abertura"
    if info is None:
        if fonte == "TCE" and "FORA_COBERTURA" in situacoes:
            return "fora da cobertura"
        return "não declarou"
    valor = info["valor"]
    if fonte == "SICONFI" and "SEM_ABERTURA" in situacoes:
        return "sem abertura"
    if fonte == "SICONFI":
        return "valor de referência"
    if _liquido(valor, referencia, params):
        return "líquido de FUNDEB"
    if referencia is not None and iguais(valor, referencia):
        return "igual"
    relativa = info.get("diferenca_relativa")
    if relativa is None:
        return "sem referência do SICONFI"
    percentual = relativa * 100
    sentido = "acima" if valor >= (referencia or 0) else "abaixo"
    return f"{percentual:.1f}% {sentido} do SICONFI"


def _bloco(ente, curto, ano, bloco, linha, historico, valores, params, independencias, entregas) -> dict:
    if linha is None:
        return {
            "codigo": bloco,
            "rotulo": ROTULOS[bloco],
            "ano": ano,
            "pergunta": pergunta(bloco, curto, ano),
            "ausente": True,
            "aplicavel": True,
            "fontes": [],
            "itens": [],
            "trilha": [],
            "pode_decidir": False,
            "exige_revisao": False,
        }
    situacoes = situacoes_de(linha.situacoes)
    diferencas = json.loads(linha.diferencas)
    aplicavel = "NAO_SE_APLICA" not in situacoes
    bloqueado = siconfi_bloqueado(situacoes)
    total = TOTAL_DO_BLOCO[bloco]
    partes_msc = {
        componente.id: valores[("MSC", ano, componente.id)]
        for componente in COMPONENTES
        if componente.bloco == bloco
        and componente.id != total
        and ("MSC", ano, componente.id) in valores
    }
    msc_sem_itens = not partes_msc or _so_bruto(partes_msc, bloco)
    entregue = entregas.get((ente.id_ente, ano))
    fontes = []
    for fonte in FONTES:
        if bloco not in BLOCOS_DA_FONTE[fonte]:
            continue
        info = diferencas.get(fonte)
        fontes.append(
            {
                "id": fonte,
                "independencia": independencias.get(fonte, ""),
                "valor": None if info is None else info["valor"],
                "frase": frase_fonte(
                    fonte,
                    info,
                    linha.valor_referencia,
                    situacoes,
                    params,
                    entregue=entregue,
                    msc_sem_itens=msc_sem_itens,
                ),
                "escolhivel": aplicavel
                and info is not None
                and fonte != linha.fonte_sugerida
                and not (fonte == "SICONFI" and bloqueado),
                "bloqueada": fonte == "SICONFI" and bloqueado,
            }
        )
    trilha = [
        {
            "id": decisao.id,
            "acao": decisao.acao,
            "fonte": decisao.fonte,
            "valor": decisao.valor,
            "valor_anterior": decisao.valor_anterior,
            "motivo": decisao.motivo,
            "usuario": nome,
            "data": decisao.data,
            "estado": decisao.estado,
            "revisor_id": decisao.revisor_id,
            "data_revisao": decisao.data_revisao,
        }
        for decisao, nome in historico
        if decisao.ano == ano and decisao.bloco == bloco
    ]
    return {
        "codigo": bloco,
        "rotulo": ROTULOS[bloco],
        "ano": ano,
        "pergunta": pergunta(bloco, curto, ano),
        "ausente": False,
        "aplicavel": aplicavel,
        "situacoes": rotulos_situacao(situacoes),
        "graves": rotulos_situacao(situacoes & VERMELHAS),
        "limpo": fecha_limpo(situacoes),
        "fontes": fontes,
        "itens": _itens(
            valores,
            ano,
            bloco,
            _fontes_dos_itens(linha.fonte_sugerida, bloco, msc_sem_itens),
            situacoes,
        ),
        "sugerida": linha.fonte_sugerida,
        "valor_sugerido": linha.valor_sugerido,
        "pode_usar_sugerida": aplicavel
        and linha.fonte_sugerida is not None
        and not (linha.fonte_sugerida == "SICONFI" and bloqueado),
        "siconfi_bloqueado": bloqueado,
        "trilha": trilha,
        "vigente": next((item for item in trilha if item["estado"] == "VIGENTE"), None),
        "pode_decidir": aplicavel,
        "exige_revisao": aplicavel
        and exige_revisao(
            ente,
            next((item["valor"] for item in trilha if item["estado"] == "VIGENTE"), linha.valor_referencia),
            params.get("revisao_corte"),
        ),
    }


def _fontes_dos_itens(sugerida: str | None, bloco: str, msc_sem_itens: bool) -> list[str]:
    fontes = []
    if not msc_sem_itens:
        fontes.append("MSC")
    fontes.append(sugerida or "SICONFI")
    for extra in ("TESOURO", "ANP", "ANM"):
        if bloco in BLOCOS_DA_FONTE[extra]:
            fontes.append(extra)
    return fontes


def _itens(valores, ano, bloco, fontes, situacoes) -> list[dict]:
    if isinstance(fontes, str):
        fontes = [fontes]
    total = TOTAL_DO_BLOCO[bloco]
    itens = []
    vistos = set()
    for fonte in fontes:
        for componente in COMPONENTES:
            if componente.bloco != bloco or componente.id == total:
                continue
            if componente.papel not in MARCAS:
                continue
            chave = (fonte, componente.id)
            if chave in vistos:
                continue
            encontrado = valores.get((fonte, ano, componente.id))
            if encontrado is None:
                continue
            vistos.add(chave)
            itens.append(
                {
                    "item": componente.item,
                    "valor": encontrado,
                    "marca": MARCAS[componente.papel],
                    "fonte": fonte,
                }
            )
    if not itens and "SEM_ABERTURA" in situacoes:
        itens.append(
            {
                "item": "Abertura no SICONFI",
                "valor": None,
                "marca": "inexistente",
                "fonte": "SICONFI",
            }
        )
    return itens


def _liquido(valor, referencia, params) -> bool:
    if valor is None or referencia is None or referencia <= 0 or valor <= 0:
        return False
    razao = valor / referencia
    return params["fundeb_liquido_min"] <= razao <= params["fundeb_liquido_max"]


def _valores_do_ente(session: Session, id_ente: int) -> dict:
    vigentes = (
        select(Carga.fonte_id, func.max(Carga.id).label("carga_id"))
        .where(Carga.situacao == "CONCLUIDA")
        .group_by(Carga.fonte_id)
        .subquery()
    )
    linhas = session.execute(
        select(
            vigentes.c.fonte_id,
            ValorObservado.ano,
            ValorObservado.componente_id,
            ValorObservado.valor,
        )
        .join(ValorObservado, ValorObservado.carga_id == vigentes.c.carga_id)
        .where(ValorObservado.id_ente == id_ente)
    )
    return {(fonte, ano, componente): valor for fonte, ano, componente, valor in linhas}
