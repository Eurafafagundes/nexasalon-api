import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import ScheduleBlockScope, ScheduleBlockType, pg_enum


class Professional(Base, UUIDPKMixin, TimestampMixin):
    """Quem presta serviço. Pode existir sem login (`user_id IS NULL`).

    `user_id` é a única FK canônica do vínculo com login — ver ajuste 1 e
    o comentário em `OrganizationMembership.professional` (identity.py).
    """

    __tablename__ = "professionals"
    __table_args__ = (
        Index(
            "uq_professionals_org_user",
            "organization_id",
            "user_id",
            unique=True,
            postgresql_where="user_id IS NOT NULL",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    photo_url: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(32))
    professional_email: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(120))
    agenda_color: Mapped[str] = mapped_column(String(7), nullable=False, server_default="#8B5CF6")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    working_hours: Mapped[list["WorkingHours"]] = relationship(back_populates="professional")


class WorkingHours(Base, UUIDPKMixin, TimestampMixin):
    """Jornada padrão semanal. Múltiplas linhas por (professional, weekday)
    são permitidas de propósito — turno partido = duas linhas. Pausas
    (almoço etc.) não vivem aqui, são ScheduleBlock."""

    __tablename__ = "working_hours"
    __table_args__ = (CheckConstraint("start_time < end_time", name="start_before_end"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=domingo … 6=sábado
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    professional: Mapped["Professional"] = relationship(back_populates="working_hours")


class ScheduleBlock(Base, UUIDPKMixin, TimestampMixin):
    """Bloqueios/exceções (almoço, reunião, folga, férias, manutenção).
    Escopo mutuamente exclusivo por linha: professional | branch | organization.
    """

    __tablename__ = "schedule_blocks"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        CheckConstraint(
            "(scope = 'professional' AND professional_id IS NOT NULL) OR "
            "(scope = 'branch' AND branch_id IS NOT NULL AND professional_id IS NULL) OR "
            "(scope = 'organization' AND branch_id IS NULL AND professional_id IS NULL)",
            name="scope_consistency",
        ),
        Index("ix_schedule_blocks_org_range", "organization_id", "start_at", "end_at"),
        Index("ix_schedule_blocks_professional_range", "professional_id", "start_at", "end_at"),
        Index("ix_schedule_blocks_branch_range", "branch_id", "start_at", "end_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    scope: Mapped[ScheduleBlockScope] = mapped_column(
        pg_enum(ScheduleBlockScope, "schedule_block_scope"), nullable=False
    )
    professional_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="CASCADE")
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE")
    )
    block_type: Mapped[ScheduleBlockType] = mapped_column(
        pg_enum(ScheduleBlockType, "schedule_block_type"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(255))
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    recurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recurrences.id", ondelete="SET NULL")
    )
