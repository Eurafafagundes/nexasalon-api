import uuid
from datetime import time

from sqlalchemy import CheckConstraint, ForeignKey, SmallInteger, String, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import OrganizationStatus, pg_enum


class Organization(Base, UUIDPKMixin, TimestampMixin):
    """Tenant raiz. Cada salão/empresa é uma Organization.

    Sem `owner_id`: propriedade é expressa via
    `OrganizationMembership.role = owner` — um segundo ponteiro de "dono"
    seria mais uma fonte de verdade duplicada (mesmo problema que o
    ajuste 1 resolveu para Professional/Membership).
    """

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    document: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="America/Sao_Paulo")
    # Texto livre de propósito, NUNCA um enum fechado — serve só de
    # metadado pra onboarding/templates/linguagem de interface (ex.:
    # "barbearia", "salao_beleza", "estetica", "nail_designer", "spa").
    # Nenhuma regra de negócio, serviço ou funcionalidade pode ficar
    # condicionada a este valor: uma barbearia pode cadastrar "Massagem",
    # um salão pode cadastrar "Barba" — o sistema nunca impede.
    business_type: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[OrganizationStatus] = mapped_column(
        pg_enum(OrganizationStatus, "organization_status"),
        nullable=False,
        server_default=OrganizationStatus.TRIAL.value,
    )

    branches: Mapped[list["Branch"]] = relationship(back_populates="organization")


class Branch(Base, UUIDPKMixin, TimestampMixin):
    """Unidade física de uma Organization.

    `agenda_view_start`/`agenda_view_end`/`agenda_slot_minutes` são
    configuração de APRESENTAÇÃO da grade da Agenda principal (que
    janela de horas desenhar, em que granularidade) — cada unidade
    define a sua, nada fixo no código. Isto é puramente visual: NÃO
    substitui `WorkingHours` (que continua sendo a única fonte de
    disponibilidade real de cada profissional) nem afeta duração de
    serviço/buffer. Os defaults (`07:00`–`21:00`, 30 min) existem só
    como valor inicial de compatibilidade para unidades já cadastradas
    antes desta migration — não são uma regra de negócio."""

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug"),
        CheckConstraint("agenda_view_start < agenda_view_end", name="agenda_view_start_before_end"),
        CheckConstraint("agenda_slot_minutes IN (15, 30)", name="agenda_slot_minutes_allowed_values"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    address_line: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(2))
    zip_code: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(32))
    timezone: Mapped[str | None] = mapped_column(String(64))  # herda o da Organization quando nulo
    agenda_view_start: Mapped[time] = mapped_column(Time, nullable=False, server_default="07:00:00")
    agenda_view_end: Mapped[time] = mapped_column(Time, nullable=False, server_default="21:00:00")
    agenda_slot_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="30")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")

    organization: Mapped["Organization"] = relationship(back_populates="branches")
