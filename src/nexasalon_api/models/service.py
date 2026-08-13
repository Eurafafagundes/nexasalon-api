import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import CommissionType, pg_enum


class ServiceCategory(Base, UUIDPKMixin, TimestampMixin):
    """Categoria de serviço — CADA organização cria as suas (ex.: um
    salão pode ter "Mega Hair"/"Cabelo"/"Química", uma barbearia pode ter
    "Corte"/"Barba"/"Combos"; nenhuma delas é fixa no código). Era um
    campo string livre no `Service` (`category`, mantido por
    compatibilidade — ver docstring de `Service`); esta tabela é o
    caminho de upgrade para permitir CRUD/reordenação/cor por categoria
    sem quebrar quem ainda usa só o texto livre.
    """

    __tablename__ = "service_categories"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str | None] = mapped_column(String(7))
    display_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class Service(Base, UUIDPKMixin, TimestampMixin):
    """CAMINHO PREFERENCIAL para categorização: `category_id` (FK pra
    `ServiceCategory`). `category` (string livre) é mantido SOMENTE por
    COMPATIBILIDADE com dados/integrações anteriores à existência de
    `ServiceCategory` — não é a forma recomendada de categorizar um
    serviço novo, mas nenhuma organização é obrigada a migrar pra
    categorias estruturadas pra continuar usando o catálogo. Clientes
    novos da API (frontend, integrações) devem preferir `category_id`;
    `category` só deve ser lido/escrito por quem ainda depende do texto
    livre legado.

    `requires_deposit`, `deposit_type`, `deposit_value`,
    `buffer_before_minutes` e `buffer_after_minutes` são APENAS
    PREPARAÇÃO DE DOMÍNIO nesta etapa — as colunas existem e são
    aceitas/validadas pela API (ver `schemas/service.py`), mas NENHUMA
    lógica de pagamento/sinal e NENHUM cálculo de buffer está
    implementado ainda. Em particular: `services/availability.py` (motor
    de disponibilidade) e `services/appointments.py` (validação de
    conflito) IGNORAM estes 5 campos por completo — um serviço com
    `requires_deposit=true` ou buffers configurados se comporta hoje
    exatamente como um sem nenhum dos dois. Isso só muda quando o
    produto definir a regra exata (Financeiro/Caixa, fora do escopo
    desta etapa).
    """

    __tablename__ = "services"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_categories.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(String)
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    default_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    color: Mapped[str | None] = mapped_column(String(7))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    allow_online_booking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    display_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")

    # Preparação para sinal/depósito — SEM lógica de pagamento ainda
    # (nenhuma rota/serviço lê estes campos por enquanto).
    requires_deposit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deposit_type: Mapped[CommissionType | None] = mapped_column(
        pg_enum(CommissionType, "commission_type")
    )  # reaproveita o enum fixed/percentage já existente — mesmo formato do `commission_type`
    deposit_value: Mapped[float | None] = mapped_column(Numeric(10, 2))

    # Preparação para buffer/intervalo antes-depois do atendimento — o
    # motor de disponibilidade AINDA NÃO lê estes campos (fica para
    # quando o produto definir a regra exata de aplicação do buffer).
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ProfessionalService(Base, UUIDPKMixin, TimestampMixin):
    """N:N Professional <-> Service. Permite duração/preço/comissão
    específicos por profissional, além do padrão do Service.

    A consistência `professional.organization_id == service.organization_id`
    não é garantida por FK simples do Postgres — fica para a camada de
    serviço (Etapa 2C) validar/travar via trigger se necessário.
    """

    __tablename__ = "professional_services"
    __table_args__ = (UniqueConstraint("professional_id", "service_id"),)

    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    duration_override_minutes: Mapped[int | None] = mapped_column(Integer)
    price_override: Mapped[float | None] = mapped_column(Numeric(10, 2))
    commission_type: Mapped[CommissionType | None] = mapped_column(
        pg_enum(CommissionType, "commission_type")
    )
    commission_value: Mapped[float | None] = mapped_column(Numeric(10, 2))
