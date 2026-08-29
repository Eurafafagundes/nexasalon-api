import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin


class AppointmentCustomStatus(Base, UUIDPKMixin, TimestampMixin):
    """Etiqueta colorida OPCIONAL de agendamento, criada livremente por
    organização (Configurações > Status Personalizados) — ex.: "Retorno",
    "VIP", "Aguardando cabelo". Deliberadamente uma tabela NOVA, não um
    reaproveitamento de `Tag`/`AppointmentTag` (migration 0002): aquelas
    são um modelo many-to-many nunca implementado (sem repo/service/
    router/UI em nenhum dos dois repositórios), pensado pra múltiplas
    tags por entidade; aqui o produto pede UM status personalizado
    opcional por agendamento — `Appointment.custom_status_id` é uma FK
    simples, não uma tabela de junção.

    ORTOGONAL ao enum `AppointmentStatus`/`appointment_state_machine.py`
    — mesmo raciocínio de isolamento de `AppointmentStatusStyle`: nenhum
    código de transição de status lê esta tabela, e trocar/remover o
    status personalizado de um agendamento nunca muda seu `status`
    operacional real.

    Nunca hard-delete de uma linha em uso (`is_active=False` apenas some
    da lista de seleção para NOVAS atribuições — um agendamento que já
    tinha essa etiqueta continua mostrando ela até ser trocada
    manualmente)."""

    __tablename__ = "appointment_custom_statuses"
    __table_args__ = (
        UniqueConstraint("organization_id", "name"),
        CheckConstraint(
            "color_hex ~ '^#[0-9A-Fa-f]{6}$'", name="ck_appointment_custom_statuses_color_hex_format"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # Mesmo padrão de `AppointmentStatusStyle.color_hex`: String(7) +
    # CHECK "#RRGGBB" no próprio banco.
    color_hex: Mapped[str] = mapped_column(String(7), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
