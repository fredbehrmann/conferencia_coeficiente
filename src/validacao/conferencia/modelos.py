"""Tabelas da conferência (prefixo cf_).

Convivem com as tabelas da versão 1 no mesmo arquivo SQLite. Nenhuma tabela
anterior é alterada; a migração copia os dados para cá preservando a origem.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from validacao.modelos import Base


class Declaracao(Base):
    """Sistema/declaração de origem (DCA, RREO, SIOPS, SIOPE, TCE...)."""

    __tablename__ = "cf_declaracao"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    sigla: Mapped[str] = mapped_column(String(30), nullable=False)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    sistema: Mapped[str] = mapped_column(String(40), nullable=False)
    grupo_independencia: Mapped[str] = mapped_column(String(40), nullable=False)
    papel: Mapped[str] = mapped_column(String(20), nullable=False)  # PRINCIPAL | AUXILIAR
    descricao: Mapped[str] = mapped_column(Text, nullable=False, default="")
    base_legal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ordem: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Componente(Base):
    """Componente de receita analisado (ex.: ISS municipal, art. 115, III, a)."""

    __tablename__ = "cf_componente"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    esfera: Mapped[str] = mapped_column(String(20), nullable=False)
    fundamento: Mapped[str] = mapped_column(Text, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    situacao: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ordem: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Mapeamento(Base):
    """Como um campo de uma declaração representa o componente em cada exercício."""

    __tablename__ = "cf_mapeamento"
    __table_args__ = (
        UniqueConstraint("declaracao_id", "campo", "exercicio_inicio", "versao"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    declaracao_id: Mapped[str] = mapped_column(ForeignKey("cf_declaracao.id"), nullable=False)
    campo: Mapped[str] = mapped_column(String(80), nullable=False)
    componente_id: Mapped[str] = mapped_column(ForeignKey("cf_componente.id"), nullable=False)
    exercicio_inicio: Mapped[int] = mapped_column(Integer, nullable=False)
    exercicio_fim: Mapped[int] = mapped_column(Integer, nullable=False)
    parcela: Mapped[str] = mapped_column(String(30), nullable=False)
    conceito: Mapped[str] = mapped_column(String(30), nullable=False)
    origem_detalhe: Mapped[str] = mapped_column(Text, nullable=False, default="")
    abrangencia: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    estagio: Mapped[str] = mapped_column(String(30), nullable=False, default="REALIZADA")
    unidade: Mapped[str] = mapped_column(String(10), nullable=False, default="R$")
    base_monetaria: Mapped[str] = mapped_column(String(20), nullable=False, default="NOMINAL")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="HIPOTESE")
    evidencia: Mapped[str] = mapped_column(Text, nullable=False, default="")
    versao: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Lote(Base):
    """Uma importação: arquivo, aba ou consulta, com identificação do conteúdo."""

    __tablename__ = "cf_lote"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    declaracao_id: Mapped[str] = mapped_column(ForeignKey("cf_declaracao.id"), nullable=False)
    origem_tipo: Mapped[str] = mapped_column(String(20), nullable=False)  # ARQUIVO | API | MIGRACAO
    arquivo: Mapped[str | None] = mapped_column(String(600), nullable=True)
    aba: Mapped[str | None] = mapped_column(String(120), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(600), nullable=True)
    parametros: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    hash_conteudo: Mapped[str] = mapped_column(String(64), nullable=False)
    data_extracao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_importacao: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    usuario: Mapped[str] = mapped_column(String(160), nullable=False)
    situacao: Mapped[str] = mapped_column(String(20), nullable=False)
    lidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    novos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repetidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    novas_versoes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    erros: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relatorio: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    carga_legada_id: Mapped[int | None] = mapped_column(ForeignKey("carga.id"), nullable=True)
    ficticio: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observacao: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Registro(Base):
    """Valor original declarado. Nunca é alterado; retificações criam nova versão."""

    __tablename__ = "cf_registro"
    __table_args__ = (
        Index("ix_cf_registro_caso", "componente_id", "exercicio", "id_ente"),
        Index("ix_cf_registro_chave", "declaracao_id", "campo"),
        Index("ix_cf_registro_lote", "lote_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lote_id: Mapped[int] = mapped_column(ForeignKey("cf_lote.id"), nullable=False)
    declaracao_id: Mapped[str] = mapped_column(ForeignKey("cf_declaracao.id"), nullable=False)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False)
    exercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    campo: Mapped[str] = mapped_column(String(80), nullable=False)
    componente_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    parcela: Mapped[str | None] = mapped_column(String(30), nullable=True)
    conceito: Mapped[str | None] = mapped_column(String(30), nullable=True)
    mapeamento_id: Mapped[int | None] = mapped_column(ForeignKey("cf_mapeamento.id"), nullable=True)
    valor_texto: Mapped[str | None] = mapped_column(String(80), nullable=True)
    valor_centavos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estado: Mapped[str] = mapped_column(String(20), nullable=False)  # VALOR | ZERO | AUSENTE | INVALIDO | NAO_APLICAVEL
    periodo: Mapped[str] = mapped_column(String(20), nullable=False, default="ANUAL")
    versao: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    vigente: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    substitui_id: Mapped[int | None] = mapped_column(ForeignKey("cf_registro.id"), nullable=True)
    duplicado: Mapped[str | None] = mapped_column(String(20), nullable=True)  # IGUAL | CONFLITANTE
    procedencia: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    data_extracao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valor_legado_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ConjuntoRegras(Base):
    """Versão dos parâmetros de todas as regras e da seleção do valor principal."""

    __tablename__ = "cf_conjunto_regras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    criado_por: Mapped[str] = mapped_column(String(160), nullable=False)
    parametros: Mapped[str] = mapped_column(Text, nullable=False)
    observacao: Mapped[str] = mapped_column(Text, nullable=False, default="")
    vigente: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Execucao(Base):
    __tablename__ = "cf_execucao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conjunto_id: Mapped[int] = mapped_column(ForeignKey("cf_conjunto_regras.id"), nullable=False)
    iniciada_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    concluida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_corte: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    componentes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    lotes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    usuario: Mapped[str] = mapped_column(String(160), nullable=False)
    situacao: Mapped[str] = mapped_column(String(20), nullable=False)  # EM_ANDAMENTO | CONCLUIDA | ERRO
    casos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alertas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mensagem: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Caso(Base):
    """Resultado da execução para ente × exercício × componente."""

    __tablename__ = "cf_caso"
    __table_args__ = (
        UniqueConstraint("execucao_id", "id_ente", "exercicio", "componente_id"),
        Index("ix_cf_caso_filtro", "execucao_id", "componente_id", "exercicio", "uf"),
        Index("ix_cf_caso_prioridade", "execucao_id", "prioridade"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execucao_id: Mapped[int] = mapped_column(ForeignKey("cf_execucao.id"), nullable=False)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False)
    exercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    componente_id: Mapped[str] = mapped_column(String(30), nullable=False)
    esfera: Mapped[str] = mapped_column(String(20), nullable=False)
    uf: Mapped[str] = mapped_column(String(2), nullable=False)
    nome_ente: Mapped[str] = mapped_column(String(300), nullable=False)
    situacao_exercicio: Mapped[str] = mapped_column(String(20), nullable=False)
    declaracao_principal: Mapped[str | None] = mapped_column(String(30), nullable=True)
    registro_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valor_principal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estado_principal: Mapped[str] = mapped_column(String(20), nullable=False)
    valores: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    comparadores: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    registros: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    par_maior: Mapped[str | None] = mapped_column(String(70), nullable=True)
    maior_dif_assinada: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    maior_dif_abs: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    maior_dif_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    alertas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gravidade_max: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tipos: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    alertas_resumo: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    diagnostico: Mapped[str] = mapped_column(String(30), nullable=False)
    prioridade: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    impressao: Mapped[str] = mapped_column(String(64), nullable=False)
    valor_proposto: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    declaracao_proposta: Mapped[str | None] = mapped_column(String(30), nullable=True)
    registro_proposto_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    motivo_proposta: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confianca: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 a 5; None = não avaliado
    confianca_motivo: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Alerta(Base):
    __tablename__ = "cf_alerta"
    __table_args__ = (Index("ix_cf_alerta_regra", "execucao_id", "regra"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execucao_id: Mapped[int] = mapped_column(ForeignKey("cf_execucao.id"), nullable=False)
    caso_id: Mapped[int] = mapped_column(ForeignKey("cf_caso.id"), nullable=False, index=True)
    regra: Mapped[str] = mapped_column(String(10), nullable=False)
    gravidade: Mapped[int] = mapped_column(Integer, nullable=False)
    declaracao_a: Mapped[str | None] = mapped_column(String(30), nullable=True)
    declaracao_b: Mapped[str | None] = mapped_column(String(30), nullable=True)
    valor_a: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    valor_b: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dif_assinada: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dif_abs: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dif_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mensagem: Mapped[str] = mapped_column(Text, nullable=False)
    evidencias: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class DecisaoCaso(Base):
    """Conclusão do analista. Cada nova decisão substitui a anterior sem apagá-la."""

    __tablename__ = "cf_decisao"
    __table_args__ = (Index("ix_cf_decisao_caso", "componente_id", "exercicio", "id_ente", "vigente"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False)
    exercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    componente_id: Mapped[str] = mapped_column(String(30), nullable=False)
    acao: Mapped[str] = mapped_column(String(30), nullable=False)
    situacao_analise: Mapped[str] = mapped_column(String(30), nullable=False)
    valor_anterior: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    valor_adotado: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    declaracao_adotada: Mapped[str | None] = mapped_column(String(30), nullable=True)
    registro_adotado_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elegivel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    justificativa: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidencias: Mapped[str] = mapped_column(Text, nullable=False, default="")
    usuario: Mapped[str] = mapped_column(String(160), nullable=False)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    execucao_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conjunto_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    impressao: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    registros_analisados: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    vigente: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    substitui_id: Mapped[int | None] = mapped_column(ForeignKey("cf_decisao.id"), nullable=True)
    origem: Mapped[str] = mapped_column(String(30), nullable=False, default="ANALISTA")
    decisao_legada_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Configuracao(Base):
    __tablename__ = "cf_configuracao"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, nullable=False)


class Consulta(Base):
    """Situação de cada consulta de recuperação (status.csv do pacote SICONFI)."""

    __tablename__ = "cf_consulta"
    __table_args__ = (Index("ix_cf_consulta_ente", "id_ente", "ano", "fonte"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fonte: Mapped[str] = mapped_column(String(30), nullable=False)
    id_ente: Mapped[int] = mapped_column(Integer, nullable=False)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    linhas_retidas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linhas_lidas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paginas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detalhe: Mapped[str] = mapped_column(Text, nullable=False, default="")
    erro: Mapped[str] = mapped_column(Text, nullable=False, default="")
    atualizado_em: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    importado_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CoberturaPacote(Base):
    """Situação de recuperação por ente e exercício (cobertura.csv do pacote SICONFI)."""

    __tablename__ = "cf_cobertura_pacote"
    __table_args__ = (UniqueConstraint("id_ente", "ano"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_ente: Mapped[int] = mapped_column(Integer, nullable=False)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    componentes_esperados: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    componentes_rreo: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    componentes_msc: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    consultas_rreo_ok: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    consultas_msc_ok: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    validacoes_ok: Mapped[int | None] = mapped_column(Integer, nullable=True)
    divergencias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ausencias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    situacao: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    importado_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)
