"""Catálogo inicial: declarações, componentes, mapeamentos, enquadramento e pendências.

Tudo o que aqui está marcado como HIPOTESE é hipótese técnica de trabalho e
precisa de validação metodológica antes de ser tratado como regra aprovada.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from validacao.conferencia.modelos import Componente, Declaracao, Mapeamento

EXERCICIOS = list(range(2019, 2027))

# ----------------------------------------------------------------------------
# Declarações e bases
# ----------------------------------------------------------------------------
DECLARACOES = (
    {
        "id": "SICONFI_DCA",
        "sigla": "DCA",
        "nome": "SICONFI — Declaração de Contas Anuais (Anexo I-C, receitas orçamentárias)",
        "sistema": "SICONFI",
        "grupo_independencia": "SICONFI",
        "papel": "PRINCIPAL",
        "descricao": "Receita realizada por natureza (família 1114.51 — ISS). Valor principal de conferência.",
        "base_legal": "LC 227/2026, art. 116, § 2º; LRF, art. 51.",
        "ordem": 1,
    },
    {
        "id": "SICONFI_RREO",
        "sigla": "RREO",
        "nome": "SICONFI — Relatório Resumido da Execução Orçamentária (Anexo 3, RCL)",
        "sistema": "SICONFI",
        "grupo_independencia": "SICONFI",
        "papel": "PRINCIPAL",
        "descricao": "Declaração do mesmo declarante no SICONFI. Não é fonte independente da DCA.",
        "base_legal": "LC 227/2026, art. 116, §§ 2º e 3º, III; LRF, arts. 52 e 53.",
        "ordem": 2,
    },
    {
        "id": "SICONFI_MSC",
        "sigla": "MSC",
        "nome": "SICONFI — Matriz de Saldos Contábeis",
        "sistema": "SICONFI",
        "grupo_independencia": "SICONFI",
        "papel": "AUXILIAR",
        "descricao": "Mesmo declarante. Útil para abrir o total por natureza quando a DCA vem sem abertura.",
        "base_legal": "LC 227/2026, art. 116, § 2º.",
        "ordem": 3,
    },
    {
        "id": "SIOPE",
        "sigla": "SIOPE",
        "nome": "SIOPE — Sistema de Informações sobre Orçamentos Públicos em Educação",
        "sistema": "SIOPE/FNDE",
        "grupo_independencia": "SIOPE",
        "papel": "AUXILIAR",
        "descricao": "Declaração do ente a outro sistema federal. Traz a abertura principal, multas e dívida ativa.",
        "base_legal": "LC 227/2026, art. 116, § 3º (outras fontes legais).",
        "ordem": 4,
    },
    {
        "id": "SIOPS",
        "sigla": "SIOPS",
        "nome": "SIOPS — Sistema de Informações sobre Orçamentos Públicos em Saúde",
        "sistema": "SIOPS/MS",
        "grupo_independencia": "SIOPS",
        "papel": "AUXILIAR",
        "descricao": "Declaração do ente a outro sistema federal. Informa o total do imposto.",
        "base_legal": "LC 227/2026, art. 116, § 3º (outras fontes legais).",
        "ordem": 5,
    },
    {
        "id": "TCE",
        "sigla": "TCE",
        "nome": "Tribunais de Contas — bases de arrecadação municipal",
        "sistema": "Tribunais de Contas",
        "grupo_independencia": "TERCEIRO",
        "papel": "AUXILIAR",
        "descricao": "Compilação das bases publicadas pelos tribunais. Conceito (bruto/líquido) varia por tribunal.",
        "base_legal": "LC 227/2026, art. 116, §§ 2º e 3º.",
        "ordem": 6,
    },
    {
        "id": "BALANCO",
        "sigla": "Balanço",
        "nome": "Balanço oficial do ente",
        "sistema": "Ente",
        "grupo_independencia": "ENTE",
        "papel": "AUXILIAR",
        "descricao": "Dados fiscais constantes dos balanços oficiais. Importação manual.",
        "base_legal": "LC 227/2026, art. 116, § 2º.",
        "ordem": 7,
    },
    {
        "id": "INFO_UF",
        "sigla": "Info. UF",
        "nome": "Informação do Estado ao CGIBS (fundos estaduais)",
        "sistema": "Estado/CGIBS",
        "grupo_independencia": "ENTE",
        "papel": "PRINCIPAL",
        "descricao": "Normas instituidoras e valores das contribuições a fundos (art. 115, I, b), com documentação.",
        "base_legal": "LC 227/2026, art. 116, §§ 5º e 6º.",
        "ordem": 9,
    },
    {
        "id": "SIMPLES",
        "sigla": "Simples",
        "nome": "Simples Nacional — ISS informado pelo banco arrecadador",
        "sistema": "Receita Federal/Banco",
        "grupo_independencia": "TERCEIRO",
        "papel": "AUXILIAR",
        "descricao": "Parcela do ISS recolhida pelo Simples Nacional. Ainda não carregada.",
        "base_legal": "LC 227/2026, art. 115, § 1º, I; art. 116, § 3º, I.",
        "ordem": 8,
    },
)

SIGLAS = {item["id"]: item["sigla"] for item in DECLARACOES}

# ----------------------------------------------------------------------------
# Componentes (art. 115 da LC 227/2026)
# ----------------------------------------------------------------------------
COMPONENTES = (
    {
        "id": "ISS_MUN",
        "nome": "ISS municipal",
        "esfera": "MUNICIPIO",
        "fundamento": "LC 227/2026, art. 115, III, a, e § 1º (inclui Simples Nacional, art. 82 do ADCT, juros, multas e dívida ativa).",
        "ativo": True,
        "situacao": "Implementado.",
        "ordem": 1,
    },
    {
        "id": "COTA_ICMS_MUN",
        "nome": "Cota-parte do ICMS creditada ao município",
        "esfera": "MUNICIPIO",
        "fundamento": "LC 227/2026, art. 115, III, b.",
        "ativo": True,
        "situacao": "Implementado. Conceito de referência: cota creditada deduzidas outras deduções, antes da dedução do FUNDEB (pendência P12).",
        "ordem": 2,
    },
    {
        "id": "ICMS_UF",
        "nome": "ICMS estadual",
        "esfera": "ESTADO",
        "fundamento": "LC 227/2026, art. 115, I, a.",
        "ativo": True,
        "situacao": "Implementado sobre o ICMS arrecadado antes da repartição; a dedução da cota-parte (art. 158, IV, a) é conciliada com a soma das cotas municipais (pendência P11).",
        "ordem": 3,
    },
    {
        "id": "FUNDOS_UF",
        "nome": "Contribuições a fundos estaduais (2021–2023)",
        "esfera": "ESTADO",
        "fundamento": "LC 227/2026, art. 115, I, b, e § 3º.",
        "ativo": True,
        "situacao": "Implementado para 2021–2023; aguarda a informação dos Estados ao CGIBS (art. 116, §§ 5º e 6º), que pode ser importada como 'Informação do Estado'.",
        "ordem": 4,
    },
    {
        "id": "ICMS_DF",
        "nome": "ICMS do Distrito Federal",
        "esfera": "DF",
        "fundamento": "LC 227/2026, art. 115, II, a.",
        "ativo": True,
        "situacao": "Implementado (ente 53).",
        "ordem": 5,
    },
    {
        "id": "ISS_DF",
        "nome": "ISS do Distrito Federal",
        "esfera": "DF",
        "fundamento": "LC 227/2026, art. 115, II, b.",
        "ativo": True,
        "situacao": "Implementado (ente 53).",
        "ordem": 6,
    },
)

COMPONENTE_DO_LEGADO = {
    "ISS": "ISS_MUN",
    "COTA_RECEBIDA": "COTA_ICMS_MUN",
    "ICMS": "ICMS_UF",
    "COTA_REPASSADA": "ICMS_UF",
}
# No DF, ISS e ICMS são componentes próprios (art. 115, II).
COMPONENTE_DF = {"ISS_MUN": "ISS_DF", "ICMS_UF": "ICMS_DF"}
COMPONENTES_MIGRADOS = ("ISS_MUN", "COTA_ICMS_MUN", "ICMS_UF")
ROTULO_CURTO = {
    "ISS_MUN": "ISS", "COTA_ICMS_MUN": "Cota ICMS", "ICMS_UF": "ICMS", "FUNDOS_UF": "Fundos",
    "ICMS_DF": "ICMS DF", "ISS_DF": "ISS DF",
}

# ----------------------------------------------------------------------------
# Conceitos e parcelas
# ----------------------------------------------------------------------------
CONCEITOS = {
    "LIQUIDO": "Receita realizada deduzidas restituições e outras deduções",
    "LIQUIDO_FUNDEB": "Líquido também da dedução para o FUNDEB",
    "BRUTO": "Receita bruta realizada, antes de deduções",
    "INDEFINIDO": "Conceito não informado pela fonte",
    "ITEM": "Parcela da composição (principal, multas, dívida ativa...)",
    "DEDUCAO": "Dedução da receita",
    "INFORMATIVO": "Valor informativo, não comparado",
}

PARCELAS = {
    "TOTAL": "Total declarado",
    "BRUTO": "Receita bruta",
    "FECOP": "Adicional de fundo de combate à pobreza (art. 82 do ADCT)",
    "PRINCIPAL": "Principal",
    "MULTAS_JUROS": "Multas e juros de mora",
    "DA_PRINCIPAL": "Dívida ativa — principal",
    "DA_MULTAS": "Dívida ativa — multas e juros",
    "OUTROS": "Outros itens / total sem abertura",
    "OUTRAS_DEDUCOES": "Outras deduções (restituições, descontos)",
    "FECOP_OUTRAS_DEDUCOES": "Outras deduções do adicional",
    "FUNDEB": "Dedução para o FUNDEB",
    "DEDUCAO_CONST": "Dedução de transferências constitucionais",
    "FECOP_FUNDEB": "FUNDEB do adicional",
    "FECOP_DEDUCAO_CONST": "Dedução constitucional do adicional",
    "LIQUIDO_FUNDEB": "Líquido após FUNDEB",
    "COTA_REPASSADA": "Cota-parte repassada aos municípios (lançada pelo estado)",
}

# Parcelas que somam o total (composição conhecida) e deduções.
PARCELAS_SOMA = ("PRINCIPAL", "MULTAS_JUROS", "DA_PRINCIPAL", "DA_MULTAS", "OUTROS", "FECOP")
PARCELAS_DEDUCAO = ("OUTRAS_DEDUCOES", "FECOP_OUTRAS_DEDUCOES")

ABRANGENCIA_COMPLETA = {
    "principal": "SIM",
    "multas_juros": "SIM",
    "divida_ativa": "SIM",
    "simples_nacional": "A_CONFIRMAR",
    "adicional_art82": "SIM",
}

# Mapeamento dos campos herdados da versão 1 para (declaração, campo) -> parcela/conceito.
# Chave: (fonte legada, componente legado)
_ISS_DCA = {
    "ISS_TOTAL": ("TOTAL", "LIQUIDO", "Planilha SICONFI, coluna ISS (receita realizada menos outras deduções; inclui adicional)"),
    "ISS_BRUTO": ("BRUTO", "BRUTO", "Planilha SICONFI_BRUTO, coluna ISS_BRUTO (Receitas Brutas Realizadas)"),
    "ISS_FECOP": ("FECOP", "BRUTO", "Planilha SICONFI_BRUTO, coluna ISS_FECOP_BRUTO"),
    "ISS_OUTRAS_DEDUCOES": ("OUTRAS_DEDUCOES", "DEDUCAO", "Planilha SICONFI_BRUTO, coluna ISS_DEDUCOES_OUTRAS"),
    "ISS_FECOP_OUTRAS_DEDUCOES": ("FECOP_OUTRAS_DEDUCOES", "DEDUCAO", "Planilha SICONFI_BRUTO, coluna ISS_FECOP_DEDUCOES_OUTRAS"),
    "ISS_FUNDEB": ("FUNDEB", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ISS_FUNDEB"),
    "ISS_DEDUCAO_CONST": ("DEDUCAO_CONST", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ISS_DEDUCOES_CONST"),
    "ISS_FECOP_FUNDEB": ("FECOP_FUNDEB", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ISS_FECOP_FUNDEB"),
    "ISS_FECOP_DEDUCAO_CONST": ("FECOP_DEDUCAO_CONST", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ISS_FECOP_DEDUCOES_CONST"),
}
_ISS_SIOPE = {
    "ISS_TOTAL": ("TOTAL", "LIQUIDO", "Planilha Municipios, coluna ISS"),
    "ISS_PRINCIPAL": ("PRINCIPAL", "ITEM", "Planilha Municipios, coluna ISS Principal"),
    "ISS_MULTAS_JUROS": ("MULTAS_JUROS", "ITEM", "Planilha Municipios, coluna ISS Multas e Juros Mora"),
    "ISS_DA_PRINCIPAL": ("DA_PRINCIPAL", "ITEM", "Planilha Municipios, coluna ISS DA - Principal"),
    "ISS_DA_MULTAS": ("DA_MULTAS", "ITEM", "Planilha Municipios, coluna ISS DA - Multas e Juros"),
    "ISS_FECOP": ("FECOP", "ITEM", "Planilha Municipios, coluna ISS FECOP"),
}
_ISS_SIOPS = {
    "ISS_TOTAL": ("TOTAL", "LIQUIDO", "Planilha Municipios, coluna ISS"),
}
_ISS_TCE = {
    "ISS_TOTAL": ("TOTAL", "POR_REGISTRO", "Base detalhada, coluna 'ISS — valor de referência' (ou 'Total publicado'); conceito na coluna 'Natureza do valor ISS'"),
    "ISS_PRINCIPAL": ("PRINCIPAL", "ITEM", "Base detalhada, coluna Principal"),
    "ISS_MULTAS_JUROS": ("MULTAS_JUROS", "ITEM", "Base detalhada, coluna Multas e juros"),
    "ISS_DA_PRINCIPAL": ("DA_PRINCIPAL", "ITEM", "Base detalhada, coluna Dívida ativa"),
    "ISS_DA_MULTAS": ("DA_MULTAS", "ITEM", "Base detalhada, coluna Multas e juros da dívida ativa"),
    "ISS_OUTROS": ("OUTROS", "ITEM", "Base detalhada, coluna Outros itens / total sem abertura"),
    "ISS_OUTRAS_DEDUCOES": ("OUTRAS_DEDUCOES", "DEDUCAO", "Base detalhada, coluna Outras deduções"),
    "ISS_FUNDEB": ("FUNDEB", "INFORMATIVO", "Base detalhada, coluna Dedução FUNDEB"),
}

_EVID_SIOPE = (
    "Nos 7.634 município-ano em que a DCA tem 'outras deduções' de ISS, o SIOPE coincide com o ISS líquido da DCA "
    "em 91% dos casos e com o bruto em 49% (tolerância de 0,1%). Medido na base carregada em 29/10/2026."
)
_EVID_SIOPS = (
    "Nos 7.641 município-ano em que a DCA tem 'outras deduções' de ISS, o SIOPS coincide com o ISS líquido da DCA "
    "em 83% dos casos e com o bruto em 44% (tolerância de 0,1%). Medido na base carregada em 29/10/2026."
)
_EVID_DCA = (
    "O total da planilha SICONFI reproduz bruto + adicional − outras deduções em mais de 99% dos município-ano "
    "(relatório de resíduos da versão 1)."
)

_COTA_DCA = {
    "COTA_RECEBIDA_TOTAL": ("TOTAL", "LIQUIDO", "Planilha SICONFI, coluna COTA_MUN (bruto menos outras deduções; FUNDEB permanece)"),
    "COTA_RECEBIDA_BRUTO": ("BRUTO", "BRUTO", "Planilha SICONFI_BRUTO, coluna COTA_RECEBIDA_BRUTO"),
    "COTA_RECEBIDA_OUTRAS_DEDUCOES": ("OUTRAS_DEDUCOES", "DEDUCAO", "Planilha SICONFI_BRUTO, coluna COTA_RECEBIDA_DEDUCOES_OUTRAS"),
    "COTA_RECEBIDA_FUNDEB": ("FUNDEB", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna COTA_RECEBIDA_FUNDEB"),
    "COTA_RECEBIDA_DEDUCAO_CONST": ("DEDUCAO_CONST", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna COTA_RECEBIDA_DEDUCOES_CONST"),
}
_COTA_SIOPE = {"COTA_RECEBIDA_TOTAL": ("TOTAL", "LIQUIDO", "Planilha Municipios, coluna Cota-Parte")}
_COTA_SIOPS = {"COTA_RECEBIDA_TOTAL": ("TOTAL", "LIQUIDO", "Planilha Municipios, coluna Cota-Parte")}
_COTA_TCE = {
    "COTA_RECEBIDA_TOTAL": ("TOTAL", "POR_REGISTRO", "Base detalhada, 'Cota-parte — valor de referência'; conceito na coluna 'Natureza do valor cota-parte'"),
    "COTA_RECEBIDA_BRUTO": ("BRUTO", "BRUTO", "Base detalhada, coluna Bruto"),
    "COTA_RECEBIDA_FUNDEB": ("FUNDEB", "INFORMATIVO", "Base detalhada, coluna Dedução FUNDEB"),
    "COTA_RECEBIDA_OUTRAS_DEDUCOES": ("OUTRAS_DEDUCOES", "DEDUCAO", "Base detalhada, coluna Outras deduções"),
    "COTA_RECEBIDA_LIQUIDO": ("LIQUIDO_FUNDEB", "LIQUIDO_FUNDEB", "Base detalhada, coluna Líquido (após FUNDEB)"),
}
_ICMS_DCA = {
    "ICMS_TOTAL": ("TOTAL", "LIQUIDO", "Planilha SICONFI, coluna ICMS (bruto + adicional − outras deduções, antes da repartição)"),
    "ICMS_BRUTO": ("BRUTO", "BRUTO", "Planilha SICONFI_BRUTO, coluna ICMS_BRUTO"),
    "ICMS_FECOP": ("FECOP", "BRUTO", "Planilha SICONFI_BRUTO, coluna ICMS_FECOP_BRUTO"),
    "ICMS_OUTRAS_DEDUCOES": ("OUTRAS_DEDUCOES", "DEDUCAO", "Planilha SICONFI_BRUTO, coluna ICMS_DEDUCOES_OUTRAS"),
    "ICMS_FECOP_OUTRAS_DEDUCOES": ("FECOP_OUTRAS_DEDUCOES", "DEDUCAO", "Planilha SICONFI_BRUTO, coluna ICMS_FECOP_DEDUCOES_OUTRAS"),
    "ICMS_FUNDEB": ("FUNDEB", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ICMS_FUNDEB"),
    "ICMS_DEDUCAO_CONST": ("DEDUCAO_CONST", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ICMS_DEDUCOES_CONST (cota-parte repassada)"),
    "ICMS_FECOP_FUNDEB": ("FECOP_FUNDEB", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ICMS_FECOP_FUNDEB"),
    "ICMS_FECOP_DEDUCAO_CONST": ("FECOP_DEDUCAO_CONST", "INFORMATIVO", "Planilha SICONFI_BRUTO, coluna ICMS_FECOP_DEDUCOES_CONST"),
    "COTA_REPASSADA": ("COTA_REPASSADA", "INFORMATIVO", "Planilha SICONFI, coluna COTA_EST (dedução constitucional lançada)"),
}
_ICMS_SIOPE = {
    "ICMS_TOTAL": ("TOTAL", "LIQUIDO", "Planilha Estados, coluna ICMS"),
    "ICMS_PRINCIPAL": ("PRINCIPAL", "ITEM", "Planilha Estados, coluna ICMS Principal"),
    "ICMS_MULTAS_JUROS": ("MULTAS_JUROS", "ITEM", "Planilha Estados, coluna ICMS Multas e Juros Mora"),
    "ICMS_DA_PRINCIPAL": ("DA_PRINCIPAL", "ITEM", "Planilha Estados, coluna ICMS DA - Principal"),
    "ICMS_DA_MULTAS": ("DA_MULTAS", "ITEM", "Planilha Estados, coluna ICMS DA - Multas e Juros"),
    "ICMS_FECOP": ("FECOP", "ITEM", "Planilha Estados, coluna ICMS FECOP"),
}
_ICMS_SIOPS = {
    "ICMS_TOTAL": ("TOTAL", "LIQUIDO", "Planilha Estados, coluna ICMS"),
    "COTA_REPASSADA": ("COTA_REPASSADA", "INFORMATIVO", "Planilha Estados, coluna Cota-Parte"),
}

_EVID_COTA = (
    "Cota-parte: o SIOPE coincide com o total da DCA em 92% dos município-ano e o SIOPS em 89%; o TCE coincide com "
    "o líquido de FUNDEB em 29% (TCE-SP e TCE-PI sobretudo, embora marcados como bruto). Base de 29/10/2026."
)
_EVID_ICMS = "ICMS estadual: o SIOPE coincide com o total da DCA em 75% dos estado-ano e o SIOPS em 65%. Base de 29/10/2026."
_EVID_TCE = "Conceito informado linha a linha pela compilação dos tribunais."

MAPEAMENTOS_LEGADO: dict[tuple[str, str], tuple[str, str, str, str, str, str, str]] = {}


def _registrar(fonte: str, declaracao: str, componente: str, campos: dict, evidencia: str, status: str) -> None:
    for campo, (parcela, conceito, detalhe) in campos.items():
        MAPEAMENTOS_LEGADO[(fonte, campo)] = (declaracao, parcela, conceito, detalhe, evidencia, status, componente)


_registrar("SICONFI", "SICONFI_DCA", "ISS_MUN", _ISS_DCA, _EVID_DCA, "HIPOTESE")
_registrar("SIOPE", "SIOPE", "ISS_MUN", _ISS_SIOPE, _EVID_SIOPE, "HIPOTESE")
_registrar("SIOPS", "SIOPS", "ISS_MUN", _ISS_SIOPS, _EVID_SIOPS, "HIPOTESE")
_registrar("TCE", "TCE", "ISS_MUN", _ISS_TCE, _EVID_TCE, "HIPOTESE")
_registrar("SICONFI", "SICONFI_DCA", "COTA_ICMS_MUN", _COTA_DCA, _EVID_DCA, "HIPOTESE")
_registrar("SIOPE", "SIOPE", "COTA_ICMS_MUN", _COTA_SIOPE, _EVID_COTA, "HIPOTESE")
_registrar("SIOPS", "SIOPS", "COTA_ICMS_MUN", _COTA_SIOPS, _EVID_COTA, "HIPOTESE")
_registrar("TCE", "TCE", "COTA_ICMS_MUN", _COTA_TCE, _EVID_TCE, "HIPOTESE")
_registrar("SICONFI", "SICONFI_DCA", "ICMS_UF", _ICMS_DCA, _EVID_DCA, "HIPOTESE")
_registrar("SIOPE", "SIOPE", "ICMS_UF", _ICMS_SIOPE, _EVID_ICMS, "HIPOTESE")
_registrar("SIOPS", "SIOPS", "ICMS_UF", _ICMS_SIOPS, _EVID_ICMS, "HIPOTESE")

DECLARACAO_DO_LEGADO = {"SICONFI": "SICONFI_DCA", "SIOPE": "SIOPE", "SIOPS": "SIOPS", "TCE": "TCE", "MSC": "SICONFI_MSC", "RREO": "SICONFI_RREO"}

NATUREZA_TCE = {
    "Bruto publicado": "BRUTO",
    "Líquido publicado": "LIQUIDO",
    "Valor único (sem classificação)": "INDEFINIDO",
    "Bruto reconstituído (líquido + FUNDEB)": "LIQUIDO",
}

# ----------------------------------------------------------------------------
# Enquadramento normativo (fonte e versão)
# ----------------------------------------------------------------------------
NORMAS = (
    {
        "ref": "LC 227/2026, art. 114",
        "versao": "Redação original (DOU 13/01/2026), consultada em 29/10/2026 em transcrição secundária",
        "tipo": "Requisito normativo",
        "texto": "De 2029 a 2077 a parcela retida é distribuída pelo coeficiente de participação: receita média de referência do ente ÷ soma das receitas médias de todos os entes.",
    },
    {
        "ref": "LC 227/2026, art. 115, III, a e b; § 1º",
        "versao": "Redação original, consultada em 29/10/2026 em transcrição secundária",
        "tipo": "Requisito normativo",
        "texto": "Municípios: arrecadação do ISS e cota-parte do ICMS creditada (art. 158, IV, a, CF). O ISS inclui Simples Nacional (LC 123/2006), adicional do art. 82 do ADCT e a arrecadação total, incluídos juros e multas, inscritos ou não em dívida ativa.",
    },
    {
        "ref": "LC 227/2026, art. 115, § 2º e § 6º",
        "versao": "Redação original, consultada em 29/10/2026 em transcrição secundária",
        "tipo": "Requisito normativo",
        "texto": "Valores anuais de 2019 a 2026, atualizados até 2026 pela variação nominal da arrecadação total de ICMS e ISS de todos os entes. Receita média do município = soma das médias de III-a e III-b.",
    },
    {
        "ref": "LC 227/2026, art. 116, §§ 1º a 4º",
        "versao": "Redação original, consultada em 29/10/2026 (admdireito.com.br)",
        "tipo": "Requisito normativo",
        "texto": "CGIBS divulga coeficientes até 31/08/2027 com valores e fontes. Usa o Siconfi, sem prejuízo dos balanços oficiais; pode considerar outras fontes legais com metodologia uniforme (Simples pelo banco arrecadador, cota-parte pela fonte pagadora, relatórios da LRF). Estimativa só para ente sem contas ou com informação comprovadamente inconsistente, sem outra fonte legal e com critérios divulgados previamente.",
    },
    {
        "ref": "LC 227/2026, art. 116, §§ 7º e 8º",
        "versao": "Redação original, consultada em 29/10/2026",
        "tipo": "Requisito normativo",
        "texto": "Contestação fundamentada em 30 dias da publicação; resposta do CGIBS em 90 dias.",
    },
    {
        "ref": "Atos do CGIBS sobre a metodologia",
        "versao": "Não localizados até 29/10/2026",
        "tipo": "Metodologia — pendente",
        "texto": "O detalhamento previsto no art. 116, § 1º, II, depende de ato do CGIBS. Nenhuma regra de seleção desta aplicação está aprovada como metodologia.",
    },
)

# ----------------------------------------------------------------------------
# Pendências de definição de negócio
# ----------------------------------------------------------------------------
PENDENCIAS = (
    ("P1", "Precedência DCA × RREO", "Qual declaração do SICONFI fornece o valor principal por componente e exercício, e o que fazer quando divergem. Configuração atual: DCA, como hipótese."),
    ("P2", "Conceito de 'arrecadação' do ISS", "Se o valor do art. 115 é bruto ou líquido de restituições e outras deduções. Configuração atual: líquido (como o total da DCA), como hipótese."),
    ("P3", "Simples Nacional", "Se o ISS do Simples já está nas naturezas 1114.51 da DCA de todos os entes ou se deve ser somado a partir do banco arrecadador (art. 115, § 1º, I). Base do Simples ainda não carregada."),
    ("P4", "Conceito de SIOPE, SIOPS e TCE", "Mapeamentos marcados como hipótese, inferidos da coincidência com a DCA. Confirmar com o manual de cada sistema e com os tribunais."),
    ("P5", "Exercício de 2026", "A DCA de 2026 só será exigível em 30/04/2027 (LRF, art. 51). Definir se haverá valor parcial (RREO) e como será tratado."),
    ("P6", "Atualização monetária (art. 115, § 2º, II)", "Índice de variação nominal da arrecadação total de ICMS e ISS ainda não carregado. Comparações usam valores nominais."),
    ("P7", "Estimativa (art. 116, § 4º)", "Critérios objetivos ainda não divulgados. A aplicação não estima valores."),
    ("P8", "Tolerâncias e limites das regras", "Todos os parâmetros iniciais são hipóteses de trabalho."),
    ("P9", "Universo de entes", "Tratamento de Fernando de Noronha (ISS arrecadado por Pernambuco) e de municípios instalados no período. Anos de instalação do IBGE ainda não carregados."),
    ("P10", "Fórmula e universo do coeficiente", "Simulação do impacto no coeficiente depende de fórmula validada, atualização do § 2º e universo definidos."),
    ("P11", "ICMS após a cota-parte (art. 115, I, a)", "Os valores comparados são o ICMS antes da repartição. Definir se a dedução da cota-parte usa o valor lançado pelo estado, 25% do arrecadado ou a soma das cotas municipais."),
    ("P12", "Cota-parte antes ou depois do FUNDEB", "Configuração atual: cota creditada deduzidas outras deduções, antes do FUNDEB, como na DCA. Bases líquidas de FUNDEB são identificadas e comparadas com ressalva."),
    ("P13", "Fundos estaduais (art. 115, I, b)", "Depende da informação dos Estados ao CGIBS; sem dados, os casos aparecem como aguardando informação."),
)

# ----------------------------------------------------------------------------
# Configuração inicial (versionada junto com as regras)
# ----------------------------------------------------------------------------
_NOTA_P1 = "Pendência P1: precedência DCA × RREO ainda não definida formalmente."
SELECAO_INICIAL = {
    "ISS_MUN": {"precedencia": ["SICONFI_DCA", "SICONFI_RREO"], "conceito_referencia": "LIQUIDO", "status": "HIPOTESE", "nota": _NOTA_P1},
    "COTA_ICMS_MUN": {"precedencia": ["SICONFI_DCA", "SICONFI_RREO"], "conceito_referencia": "LIQUIDO", "status": "HIPOTESE",
                      "nota": _NOTA_P1 + " Pendência P12: valor antes ou depois do FUNDEB."},
    "ICMS_UF": {"precedencia": ["SICONFI_DCA", "SICONFI_RREO"], "conceito_referencia": "LIQUIDO", "status": "HIPOTESE",
                "nota": _NOTA_P1 + " Pendência P11: o art. 115, I, a, considera o ICMS após a cota-parte municipal."},
    "ICMS_DF": {"precedencia": ["SICONFI_DCA", "SICONFI_RREO"], "conceito_referencia": "LIQUIDO", "status": "HIPOTESE", "nota": _NOTA_P1},
    "ISS_DF": {"precedencia": ["SICONFI_DCA", "SICONFI_RREO"], "conceito_referencia": "LIQUIDO", "status": "HIPOTESE", "nota": _NOTA_P1},
    "FUNDOS_UF": {"precedencia": ["INFO_UF"], "conceito_referencia": "LIQUIDO", "status": "HIPOTESE",
                  "nota": "Valores informados pelos Estados ao CGIBS, com documentação (art. 116, §§ 5º e 6º)."},
}

# Universo e calendário de cada componente.
ESCOPO_COMPONENTE = {
    "ISS_MUN": {"esfera": "MUNICIPIO", "exercicios": list(range(2019, 2027)), "exigibilidade": "LRF"},
    "COTA_ICMS_MUN": {"esfera": "MUNICIPIO", "exercicios": list(range(2019, 2027)), "exigibilidade": "LRF"},
    "ICMS_UF": {"esfera": "ESTADO", "exercicios": list(range(2019, 2027)), "exigibilidade": "LRF"},
    "ICMS_DF": {"esfera": "DF", "exercicios": list(range(2019, 2027)), "exigibilidade": "LRF"},
    "ISS_DF": {"esfera": "DF", "exercicios": list(range(2019, 2027)), "exigibilidade": "LRF"},
    "FUNDOS_UF": {"esfera": "ESTADO", "exercicios": [2021, 2022, 2023], "exigibilidade": "CGIBS"},
}

EXCLUSOES_UNIVERSO = {
    "2605459": "Distrito estadual de Fernando de Noronha: o ISS é arrecadado por Pernambuco (pendência P9).",
}

# Prazo de exigibilidade da declaração principal: DCA do exercício N até 30/04 de N+1 (LRF, art. 51).
PRAZO_DECLARACAO = {"mes": 4, "dia": 30}


def semear_catalogo(session: Session) -> None:
    existentes = set(session.scalars(select(Declaracao.id)))
    for item in DECLARACOES:
        if item["id"] not in existentes:
            session.add(Declaracao(**item))
    existentes = {c.id: c for c in session.scalars(select(Componente))}
    for item in COMPONENTES:
        atual = existentes.get(item["id"])
        if atual is None:
            session.add(Componente(**item))
        else:  # o catálogo do código é a referência para situação e ativação
            for campo, valor in item.items():
                setattr(atual, campo, valor)
    session.flush()
    existentes = {
        (declaracao, campo)
        for declaracao, campo in session.execute(select(Mapeamento.declaracao_id, Mapeamento.campo))
    }
    for (fonte, campo), (declaracao, parcela, conceito, detalhe, evidencia, status, componente) in MAPEAMENTOS_LEGADO.items():
        if (declaracao, campo) in existentes:
            continue
        session.add(
            Mapeamento(
                declaracao_id=declaracao,
                campo=campo,
                componente_id=componente,
                exercicio_inicio=2019,
                exercicio_fim=2026,
                parcela=parcela,
                conceito=conceito,
                origem_detalhe=detalhe,
                abrangencia=json.dumps(ABRANGENCIA_COMPLETA if parcela == "TOTAL" else {}, ensure_ascii=False),
                status=status,
                evidencia=evidencia,
                versao=1,
            )
        )
    session.flush()


def mapa_mapeamentos(session: Session) -> dict[tuple[str, str], Mapeamento]:
    resultado = {}
    for item in session.scalars(select(Mapeamento).order_by(Mapeamento.versao)):
        resultado[(item.declaracao_id, item.campo)] = item
    return resultado
