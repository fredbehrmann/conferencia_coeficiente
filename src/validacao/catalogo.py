from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.modelos import Componente, Fonte


@dataclass(frozen=True)
class FonteDef:
    id: str
    nome: str
    independencia: str
    granularidade: str
    natureza_padrao: str


@dataclass(frozen=True)
class ComponenteDef:
    id: str
    bloco: str
    item: str
    esferas: tuple[str, ...]
    papel: str


FONTES = (
    FonteDef("SICONFI", "SICONFI DCA", "MESMO_DECLARANTE", "ITENS", "BRUTO"),
    FonteDef("MSC", "SICONFI MSC", "MESMO_DECLARANTE", "ITENS", "BRUTO"),
    FonteDef("RREO", "SICONFI RREO", "MESMO_DECLARANTE", "TOTAL", "BRUTO"),
    FonteDef("ENTREGA", "SICONFI entrega", "MESMO_DECLARANTE", "TOTAL", "SEM_CLASSIFICACAO"),
    FonteDef("SIOPS", "SIOPS", "OUTRO_SISTEMA", "TOTAL", "BRUTO"),
    FonteDef("SIOPE", "SIOPE", "OUTRO_SISTEMA", "ITENS", "BRUTO"),
    FonteDef("TCE", "Tribunais de Contas", "TERCEIRO", "ITENS", "SEM_CLASSIFICACAO"),
    FonteDef("CONFAZ", "CONFAZ", "TERCEIRO", "TOTAL", "BRUTO"),
    FonteDef("SEFAZ_UF", "SEFAZ da UF", "OUTRO_SISTEMA", "TOTAL", "BRUTO"),
    FonteDef("TESOURO", "Tesouro Transparente", "TERCEIRO", "ITENS", "BRUTO"),
    FonteDef("ANP", "ANP", "TERCEIRO", "TOTAL", "BRUTO"),
    FonteDef("ANM", "ANM", "TERCEIRO", "TOTAL", "BRUTO"),
    FonteDef("SIMPLES", "ISS do Simples", "TERCEIRO", "TOTAL", "BRUTO"),
)

ROTULOS_INDEPENDENCIA = {
    "MESMO_DECLARANTE": "mesmo declarante",
    "OUTRO_SISTEMA": "outro sistema",
    "TERCEIRO": "terceiro",
}

_MUN_DF = ("MUNICIPIO", "DF")
_EST_DF = ("ESTADO", "DF")
_TODAS = ("MUNICIPIO", "ESTADO", "DF")

COMPONENTES = (
    ComponenteDef("ISS_BRUTO", "ISS", "Bruto", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_PRINCIPAL", "ISS", "Principal", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_MULTAS_JUROS", "ISS", "Multas e juros", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_DA_PRINCIPAL", "ISS", "Dívida ativa", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_DA_MULTAS", "ISS", "Multas e juros da dívida ativa", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_FECOP", "ISS", "FECOP", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_OUTROS", "ISS", "Outros itens ou total sem abertura", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_OUTRAS_DEDUCOES", "ISS", "Outras deduções", _MUN_DF, "SUBTRAI"),
    ComponenteDef("ISS_FECOP_OUTRAS_DEDUCOES", "ISS", "Outras deduções do FECOP", _MUN_DF, "SUBTRAI"),
    ComponenteDef("ISS_FUNDEB", "ISS", "FUNDEB", _MUN_DF, "INFORMATIVO"),
    ComponenteDef("ISS_DEDUCAO_CONST", "ISS", "Dedução constitucional", _MUN_DF, "INFORMATIVO"),
    ComponenteDef("ISS_FECOP_FUNDEB", "ISS", "FUNDEB do FECOP", _MUN_DF, "INFORMATIVO"),
    ComponenteDef("ISS_FECOP_DEDUCAO_CONST", "ISS", "Dedução constitucional do FECOP", _MUN_DF, "INFORMATIVO"),
    ComponenteDef("ISS_TOTAL", "ISS", "Total declarado", _MUN_DF, "SOMA"),
    ComponenteDef("ISS_SIMPLES", "ISS_SIMPLES", "Total distribuído ao ente", _MUN_DF, "SOMA"),
    ComponenteDef("COTA_RECEBIDA_BRUTO", "COTA_RECEBIDA", "Bruto", ("MUNICIPIO",), "SOMA"),
    ComponenteDef("COTA_RECEBIDA_OUTRAS_DEDUCOES", "COTA_RECEBIDA", "Outras deduções", ("MUNICIPIO",), "SUBTRAI"),
    ComponenteDef("COTA_RECEBIDA_FUNDEB", "COTA_RECEBIDA", "FUNDEB", ("MUNICIPIO",), "INFORMATIVO"),
    ComponenteDef("COTA_RECEBIDA_DEDUCAO_CONST", "COTA_RECEBIDA", "Dedução constitucional", ("MUNICIPIO",), "INFORMATIVO"),
    ComponenteDef("COTA_RECEBIDA_LIQUIDO", "COTA_RECEBIDA", "Líquido publicado", ("MUNICIPIO",), "INFORMATIVO"),
    ComponenteDef("COTA_RECEBIDA_TOTAL", "COTA_RECEBIDA", "Total declarado", ("MUNICIPIO",), "SOMA"),
    ComponenteDef("ICMS_BRUTO", "ICMS", "Bruto", _EST_DF, "SOMA"),
    ComponenteDef("ICMS_PRINCIPAL", "ICMS", "Principal", _EST_DF, "SOMA"),
    ComponenteDef("ICMS_MULTAS_JUROS", "ICMS", "Multas e juros", _EST_DF, "SOMA"),
    ComponenteDef("ICMS_DA_PRINCIPAL", "ICMS", "Dívida ativa", _EST_DF, "SOMA"),
    ComponenteDef("ICMS_DA_MULTAS", "ICMS", "Multas e juros da dívida ativa", _EST_DF, "SOMA"),
    ComponenteDef("ICMS_FECOP", "ICMS", "FECOP", _EST_DF, "SOMA"),
    ComponenteDef("ICMS_OUTRAS_DEDUCOES", "ICMS", "Outras deduções", _EST_DF, "SUBTRAI"),
    ComponenteDef("ICMS_FECOP_OUTRAS_DEDUCOES", "ICMS", "Outras deduções do FECOP", _EST_DF, "SUBTRAI"),
    ComponenteDef("ICMS_FUNDEB", "ICMS", "FUNDEB", _EST_DF, "INFORMATIVO"),
    ComponenteDef("ICMS_DEDUCAO_CONST", "ICMS", "Dedução constitucional", _EST_DF, "INFORMATIVO"),
    ComponenteDef("ICMS_FECOP_FUNDEB", "ICMS", "FUNDEB do FECOP", _EST_DF, "INFORMATIVO"),
    ComponenteDef("ICMS_FECOP_DEDUCAO_CONST", "ICMS", "Dedução constitucional do FECOP", _EST_DF, "INFORMATIVO"),
    ComponenteDef("ICMS_TOTAL", "ICMS", "Total declarado", _EST_DF, "SOMA"),
    ComponenteDef("COTA_REPASSADA", "COTA_REPASSADA", "Dedução constitucional lançada", ("ESTADO",), "SOMA"),
    ComponenteDef("COMPENSACAO", "COMPENSACAO", "Total declarado", _TODAS, "SOMA"),
    ComponenteDef("COMPENSACAO_BRUTO", "COMPENSACAO", "Bruto", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_DEDUCAO_CONST", "COMPENSACAO", "Dedução constitucional", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_FUNDEB", "COMPENSACAO", "FUNDEB", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_OUTRAS_DEDUCOES", "COMPENSACAO", "Outras deduções", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_REPASSADA", "COMPENSACAO", "Cota de compensação lançada pelo estado", ("ESTADO",), "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_RECEBIDA_BRUTO", "COMPENSACAO", "Bruto da cota recebida", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_RECEBIDA_DEDUCAO_CONST", "COMPENSACAO", "Dedução constitucional da cota recebida", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_RECEBIDA_FUNDEB", "COMPENSACAO", "FUNDEB da cota recebida", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_RECEBIDA_OUTRAS_DEDUCOES", "COMPENSACAO", "Outras deduções da cota recebida", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_LC87", "COMPENSACAO", "LC 87/96", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_IPI_EXP", "COMPENSACAO", "IPI-Exportação", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_FEX", "COMPENSACAO", "FEX", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_ROYALTIES", "COMPENSACAO", "Royalties e participação especial", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_CFEM", "COMPENSACAO", "CFEM", _TODAS, "INFORMATIVO"),
    ComponenteDef("COMPENSACAO_OUTRAS_TIPOS", "COMPENSACAO", "Outras compensações", _TODAS, "INFORMATIVO"),
)

POR_ID = {item.id: item for item in COMPONENTES}


def permitidos(esfera: str) -> set[str]:
    return {item.id for item in COMPONENTES if esfera in item.esferas}


def semear(session: Session) -> None:
    fontes = set(session.scalars(select(Fonte.id)))
    for definicao in FONTES:
        if definicao.id in fontes:
            continue
        session.add(
            Fonte(
                id=definicao.id,
                nome=definicao.nome,
                independencia=definicao.independencia,
                granularidade=definicao.granularidade,
                natureza_padrao=definicao.natureza_padrao,
            )
        )
    componentes = set(session.scalars(select(Componente.id)))
    for definicao in COMPONENTES:
        if definicao.id in componentes:
            continue
        session.add(
            Componente(
                id=definicao.id,
                bloco=definicao.bloco,
                item=definicao.item,
                esferas_aplicaveis=",".join(definicao.esferas),
                papel=definicao.papel,
            )
        )
