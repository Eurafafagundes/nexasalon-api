"""Central de Recuperação de Clientes — fila operacional de recuperação
de clientes "sem retorno" (Clientes → Sem retorno).

Duas tabelas, nunca uma só:

  - `ClientRecoveryCycle`: estado OPERACIONAL ATUAL de um ciclo de
    recuperação (1 linha por ciclo). Só pode haver 1 ciclo ATIVO
    (`status IN ('pending', 'in_progress')`) por cliente/organização —
    garantido por índice único parcial, nunca só por regra de serviço.
    `status='scheduled'` só é alcançado com `linked_appointment_id`
    preenchido (nunca manualmente, ver `services/client_recovery.py::
    schedule_return`); `status='recovered'` só quando esse agendamento é
    efetivamente atendido. Concorrência otimista (`version`, mesmo
    padrão de `ClientObservation`/`Order.observation_version`): nasce em
    `0`, nunca `NULL`.

  - `ClientRecoveryEvent`: histórico IMUTÁVEL (append-only, nunca
    update/delete pela aplicação) de cada ação sobre um ciclo —
    MESMO espírito de `AuditLog`, mas dedicado (nunca reaproveita
    `AuditLog` aqui: o volume/formato de "registrar contato" é
    específico demais — canal, resultado, próximo contato — pra caber
    bem no `old_values`/`new_values` genérico). `actor_user_name` é
    snapshot (nunca join a `users.name`) — sobrevive a renomeação/
    remoção do usuário, mesmo padrão de `AuditLog.user_name`.
    `actor_user_id IS NULL` identifica uma ação do SISTEMA (nunca de uma
    pessoa) — ex.: reabertura automática por cancelamento/falta do
    agendamento de retorno vinculado.

    Nunca armazena telefone/CPF — só IDs e snapshots de nome (item
    explícito do pedido "nenhum armazenamento desnecessário de telefone
    ou CPF nos eventos")."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import (
    RecoveryContactChannel,
    RecoveryContactResult,
    RecoveryCycleStatus,
    RecoveryEventType,
    pg_enum,
)


class ClientRecoveryCycle(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "client_recovery_cycles"
    __table_args__ = (
        Index("ix_client_recovery_cycles_org_client", "organization_id", "client_id"),
        Index("ix_client_recovery_cycles_org_assigned", "organization_id", "assigned_user_id"),
        Index("ix_client_recovery_cycles_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[RecoveryCycleStatus] = mapped_column(
        pg_enum(RecoveryCycleStatus, "recovery_cycle_status"),
        nullable=False,
        server_default=RecoveryCycleStatus.PENDING.value,
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_user_name: Mapped[str | None] = mapped_column(String(160))
    # Comanda FECHADA que tornou o cliente elegível (referência, nunca
    # recalculada depois — o "contador de dias" em si continua sempre
    # derivado na leitura, ver docstring de `services/client_recovery.py`).
    reference_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )
    # Agendamento de RETORNO real — só preenchido via "Agendar retorno"
    # dentro de um ciclo já aberto pela recepção (nunca um agendamento
    # espontâneo do cliente, ver docstring do módulo de serviço).
    linked_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )
    next_contact_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Pausa de "sem interesse" (item 12/20 do pedido) — enquanto
    # `now() < suppressed_until`, o cliente não reentra automaticamente
    # na fila mesmo que a elegibilidade "dias sem retorno" já bata.
    suppressed_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ClientRecoveryEvent(Base):
    """Append-only — sem `TimestampMixin` (não tem `updated_at`, nunca é
    atualizado) e sem `UUIDPKMixin` (BigInteger sequencial, mesmo padrão
    de `AuditLog` — alto volume, facilita particionamento futuro)."""

    __tablename__ = "client_recovery_events"
    __table_args__ = (
        Index("ix_client_recovery_events_org_cycle", "organization_id", "recovery_cycle_id"),
        Index("ix_client_recovery_events_org_client", "organization_id", "client_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    recovery_cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("client_recovery_cycles.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_user_name: Mapped[str | None] = mapped_column(String(160))
    event_type: Mapped[RecoveryEventType] = mapped_column(
        pg_enum(RecoveryEventType, "recovery_event_type"), nullable=False
    )
    contact_channel: Mapped[RecoveryContactChannel | None] = mapped_column(
        pg_enum(RecoveryContactChannel, "recovery_contact_channel")
    )
    contact_result: Mapped[RecoveryContactResult | None] = mapped_column(
        pg_enum(RecoveryContactResult, "recovery_contact_result")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    previous_status: Mapped[RecoveryCycleStatus | None] = mapped_column(
        pg_enum(RecoveryCycleStatus, "recovery_cycle_status")
    )
    new_status: Mapped[RecoveryCycleStatus | None] = mapped_column(
        pg_enum(RecoveryCycleStatus, "recovery_cycle_status")
    )
    next_contact_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Agendamento de retorno vinculado NESTE evento especificamente (item
    # "Agendamento vinculado" da timeline, seção 15 do pedido) — só
    # preenchido no evento que de fato linkou (`event_type=
    # appointment_linked`/`contact_logged` com resultado "retorno
    # agendado"); `ondelete=SET NULL` porque o histórico deste evento
    # continua válido mesmo que o agendamento seja apagado por baixo
    # (não há rota de delete físico de Appointment hoje, mas a trava
    # fica correta mesmo se isso mudar).
    linked_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class OrganizationRecoverySettings(Base, UUIDPKMixin, TimestampMixin):
    """Config 1:1 esparsa por organização — mesmo padrão de
    `CashRegisterConfig`: linha só nasce no 1º PUT, leitura sem linha
    devolve os defaults abaixo em memória (`services/client_recovery.py::
    get_effective_settings`, nunca grava sozinha numa leitura)."""

    __tablename__ = "organization_recovery_settings"
    __table_args__ = (
        CheckConstraint("default_no_return_days IS NULL OR default_no_return_days > 0", name="ck_default_no_return_days_positive"),
        CheckConstraint("default_follow_up_days IS NULL OR default_follow_up_days > 0", name="ck_default_follow_up_days_positive"),
        CheckConstraint("default_snooze_days IS NULL OR default_snooze_days > 0", name="ck_default_snooze_days_positive"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    default_no_return_days: Mapped[int | None] = mapped_column(Integer)
    default_follow_up_days: Mapped[int | None] = mapped_column(Integer)
    default_snooze_days: Mapped[int | None] = mapped_column(Integer)
    whatsapp_message_template: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
