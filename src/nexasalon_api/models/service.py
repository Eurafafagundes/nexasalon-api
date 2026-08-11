import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import CommissionType, pg_enum


class Service(Base, UUIDPKMixin, TimestampMixin):
    """`category` é string simples nesta etapa, não uma tabela própria —
    caminho de upgrade natural para `ServiceCategory` sem quebra de
    compatibilidade, se um dia precisar de CRUD/reordenação de categorias.
    """

    __tablename__ = "services"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String)
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    default_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    color: Mapped[str | None] = mapped_column(String(7))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


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
