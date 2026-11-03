from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Fonte(Base):
    __tablename__ = "fonte"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    nome: Mapped[str] = mapped_column(String(160), nullable=False)
    independencia: Mapped[str] = mapped_column(String(30), nullable=False)
    granularidade: Mapped[str] = mapped_column(String(20), nullable=False)
    natureza_padrao: Mapped[str] = mapped_column(String(30), nullable=False)


class Componente(Base):
    __tablename__ = "componente"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    bloco: Mapped[str] = mapped_column(String(40), nullable=False)
    item: Mapped[str] = mapped_column(String(160), nullable=False)
    esferas_aplicaveis: Mapped[str] = mapped_column(String(40), nullable=False)
    papel: Mapped[str] = mapped_column(String(20), nullable=False)


class Ente(Base):
    __tablename__ = "ente"

    id_ente: Mapped[int] = mapped_column(Integer, primary_key=True)
    esfera: Mapped[str] = mapped_column(String(20), nullable=False)
    uf: Mapped[str] = mapped_column(String(2), nullable=False)
    nome: Mapped[str] = mapped_column(String(300), nullable=False)
    capital: Mapped[bool] = mapped_column(nullable=False, default=False)
    tribunal_competente: Mapped[str] = mapped_column(String(20), nullable=False)
    ano_instalacao: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ano_extincao: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EnteCodigoExterno(Base):
    __tablename__ = "ente_codigo_externo"
    __table_args__ = (UniqueConstraint("fonte_id", "codigo_externo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fonte_id: Mapped[str] = mapped_column(ForeignKey("fonte.id"), nullable=False)
    codigo_externo: Mapped[str] = mapped_column(String(20), nullable=False)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False)


class Carga(Base):
    __tablename__ = "carga"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fonte_id: Mapped[str] = mapped_column(ForeignKey("fonte.id"), nullable=False)
    arquivo_ou_endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    data_extracao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_carga: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exercicios: Mapped[str] = mapped_column(String(80), nullable=False)
    usuario: Mapped[str] = mapped_column(String(80), nullable=False)
    situacao: Mapped[str] = mapped_column(String(20), nullable=False)


class ValorObservado(Base):
    __tablename__ = "valor_observado"
    __table_args__ = (
        UniqueConstraint("carga_id", "id_ente", "ano", "componente_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    carga_id: Mapped[int] = mapped_column(ForeignKey("carga.id"), nullable=False, index=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False, index=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    componente_id: Mapped[str] = mapped_column(ForeignKey("componente.id"), nullable=False)
    valor: Mapped[float] = mapped_column(Float, nullable=False)
    natureza: Mapped[str | None] = mapped_column(String(80), nullable=True)
    conta_origem: Mapped[str | None] = mapped_column(String(120), nullable=True)
    linha_origem: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RegraVersao(Base):
    __tablename__ = "regra_versao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vigente_desde: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parametros: Mapped[str | None] = mapped_column(String, nullable=True)


class Verificacao(Base):
    __tablename__ = "verificacao"
    __table_args__ = (UniqueConstraint("id_ente", "ano", "bloco", "regra_versao_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False, index=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    bloco: Mapped[str] = mapped_column(String(40), nullable=False)
    regra_versao_id: Mapped[int] = mapped_column(ForeignKey("regra_versao.id"), nullable=False, index=True)
    valor_referencia: Mapped[float | None] = mapped_column(Float, nullable=True)
    fonte_sugerida: Mapped[str | None] = mapped_column(String(20), nullable=True)
    valor_sugerido: Mapped[float | None] = mapped_column(Float, nullable=True)
    situacoes: Mapped[str] = mapped_column(String, nullable=False)
    diferencas: Mapped[str] = mapped_column(String, nullable=False)


class Usuario(Base):
    __tablename__ = "usuario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    perfil: Mapped[str] = mapped_column(String(20), nullable=False)


class EntregaDeclaracao(Base):
    __tablename__ = "entrega_declaracao"
    __table_args__ = (UniqueConstraint("id_ente", "ano", "declaracao"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False, index=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    declaracao: Mapped[str] = mapped_column(String(20), nullable=False)
    entregue: Mapped[bool] = mapped_column(nullable=False)
    carga_id: Mapped[int] = mapped_column(ForeignKey("carga.id"), nullable=False)


class Decisao(Base):
    __tablename__ = "decisao"
    __table_args__ = (
        Index(
            "uq_decisao_vigente",
            "id_ente",
            "ano",
            "bloco",
            unique=True,
            sqlite_where=text("estado = 'VIGENTE'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False, index=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    bloco: Mapped[str] = mapped_column(String(40), nullable=False)
    acao: Mapped[str] = mapped_column(String(20), nullable=False)
    fonte: Mapped[str | None] = mapped_column(String(20), nullable=True)
    valor: Mapped[float | None] = mapped_column(Float, nullable=True)
    valor_anterior: Mapped[float | None] = mapped_column(Float, nullable=True)
    motivo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), nullable=False)
    data: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cargas_usadas: Mapped[str] = mapped_column(String, nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False)
    revisor_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"), nullable=True)
    data_revisao: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BaseVersao(Base):
    __tablename__ = "base_versao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    regra_versao_id: Mapped[int] = mapped_column(ForeignKey("regra_versao.id"), nullable=False)
    data_congelamento: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    responsavel_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), nullable=False)


class BaseLinha(Base):
    __tablename__ = "base_linha"
    __table_args__ = (UniqueConstraint("base_versao_id", "id_ente", "ano", "bloco"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_versao_id: Mapped[int] = mapped_column(ForeignKey("base_versao.id"), nullable=False, index=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False, index=True)
    esfera: Mapped[str] = mapped_column(String(20), nullable=False)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    bloco: Mapped[str] = mapped_column(String(40), nullable=False)
    valor: Mapped[float | None] = mapped_column(Float, nullable=True)
    fonte: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decisao_id: Mapped[int] = mapped_column(ForeignKey("decisao.id"), nullable=False)


class FormulaVersao(Base):
    __tablename__ = "formula_versao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vigente_desde: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    descricao: Mapped[str] = mapped_column(String(200), nullable=False)
    parametros: Mapped[str] = mapped_column(String, nullable=False)


class Apuracao(Base):
    __tablename__ = "apuracao"
    __table_args__ = (UniqueConstraint("base_versao_id", "formula_versao_id", "id_ente"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_versao_id: Mapped[int] = mapped_column(ForeignKey("base_versao.id"), nullable=False, index=True)
    formula_versao_id: Mapped[int] = mapped_column(ForeignKey("formula_versao.id"), nullable=False, index=True)
    id_ente: Mapped[int] = mapped_column(ForeignKey("ente.id_ente"), nullable=False, index=True)
    coeficiente: Mapped[float] = mapped_column(Float, nullable=False)
    memoria: Mapped[str] = mapped_column(String, nullable=False)


class CargaEvento(Base):
    __tablename__ = "carga_evento"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quando: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fonte_id: Mapped[str | None] = mapped_column(ForeignKey("fonte.id"), nullable=True)
    situacao: Mapped[str] = mapped_column(String(20), nullable=False)
    mensagem: Mapped[str] = mapped_column(String(1000), nullable=False)


class AgendaFonte(Base):
    __tablename__ = "agenda_fonte"

    fonte_id: Mapped[str] = mapped_column(ForeignKey("fonte.id"), primary_key=True)
    intervalo_dias: Mapped[int] = mapped_column(Integer, nullable=False)
