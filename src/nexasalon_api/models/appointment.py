import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import AppointmentSource, AppointmentStatus, RecurrenceFrequency, RecurrenceStatus, pg_enum


class Recurrence(Base, UUIDPKMixin, TimestampMixin):
    """Padrão de recorrência reaproveitado por Appointment e ScheduleBlock.

    Modela a REGRA, não gera ocorrências virtuais: cada Appointment ou
    ScheduleBlock recorrente é uma linha própria com `recurrence_id`
    apontando pra cá. O motor que materializa ocorrências futuras é
    trabalho de uma etapa posterior, mas o schema já suporta isso sem
    refatoração.
    """

    __tablename__ = "recurrences"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    frequency: Mapped[RecurrenceFrequency] = mapped_column(
        pg_enum(RecurrenceFrequency, "recurrence_frequency"), nullable=False
    )
    interval: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    days_of_week: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    occurrences_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[RecurrenceStatus] = mapped_column(
        pg_enum(RecurrenceStatus, "recurrence_status"),
        nullable=False,
        server_default=RecurrenceStatus.ACTIVE.value,
    )


class Appointment(Base, UUIDPKMixin, TimestampMixin):
    """Nunca guarda serviço diretamente — cada serviço reservado é um
    AppointmentItem (uma reserva pode ter vários serviços/profissionais).

    `starts_at`/`ends_at` são CACHE DERIVADO dos itens (ajuste 3 aprovado):
    nunca aceitos como entrada do cliente, sempre recalculados pelo
    trigger `recalc_appointment_bounds` (migration 0006) na mesma
    transação em que um AppointmentItem é criado/alterado/removido. Por
    isso ficam nullable aqui — um Appointment sem itens ainda (estado
    transiente dentro de uma transação) não tem bounds definidos.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_org_branch_starts", "organization_id", "branch_id", "starts_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        pg_enum(AppointmentStatus, "appointment_status"),
        nullable=False,
        server_default=AppointmentStatus.SCHEDULED.value,
        index=True,
    )
    source: Mapped[AppointmentSource] = mapped_column(
        pg_enum(AppointmentSource, "appointment_source"),
        nullable=False,
        server_default=AppointmentSource.INTERNAL.value,
    )
    notes: Mapped[str | None] = mapped_column(String)
    # Encaixe (migration 0016) — CARACTERÍSTICA do agendamento, não um
    # status paralelo: convive com qualquer `status` (ex.: um encaixe
    # pode estar `confirmed` normalmente). Só serve pra IDENTIFICAR/
    # MEDIR encaixes hoje — marcar `fit_in=True` não pula nenhuma
    # validação de disponibilidade (jornada/bloqueio/conflito continuam
    # rodando exatamente igual, ver `services/appointments.py`). "Forçar"
    # um encaixe sobre um conflito real é um passo futuro deliberadamente
    # fora de escopo aqui — ver decisão registrada na migration 0016.
    fit_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # cache derivado — ver docstring da classe. Nunca escrito pela API.
    starts_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    recurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recurrences.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    items: Mapped[list["AppointmentItem"]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan"
    )


class AppointmentItem(Base, UUIDPKMixin, TimestampMixin):
    """Cada item = um serviço com um profissional e um horário dentro da
    reserva. `price`/`duration_minutes` são SNAPSHOT no momento da
    reserva (não recalculados de Service/ProfessionalService depois) —
    senão o histórico financeiro muda quando o salão reajusta preços.

    Sem EXCLUDE constraint de sobreposição no banco (ajuste 2 aprovado):
    a permissão `agenda.force_overlap` exige que overlap seja permitido
    em certos casos, o que uma constraint cega ao contexto não suporta.
    A checagem roda no trigger `check_appointment_item_overlap`
    (migration 0006), que usa `pg_advisory_xact_lock` para serializar
    concorrência por profissional dentro da transação — ver seção de
    decisões técnicas.
    """

    __tablename__ = "appointment_items"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        Index("ix_appointment_items_professional_range", "professional_id", "start_at", "end_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="RESTRICT"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[AppointmentStatus | None] = mapped_column(
        pg_enum(AppointmentStatus, "appointment_status")
    )  # nulo herda o status do Appointment pai

    appointment: Mapped["Appointment"] = relationship(back_populates="items")
