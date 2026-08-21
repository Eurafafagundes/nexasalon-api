"""Configurações do Caixa (Etapa H — `Financeiro > Caixa >
Configurações do Caixa`) — uma linha por ORGANIZAÇÃO (não por unidade;
as regras valem pra toda a organização, mesmo quando avaliadas por
unidade em tempo de execução, ex.: "apenas um caixa aberto por
unidade"). Linha SPARSE de propósito, mesmo padrão de
`AppointmentStatusStyle`: só existe depois do primeiro `PUT`
(`services/cash_register_config.py::get_effective_config` devolve os
defaults abaixo em memória pra qualquer organização sem linha ainda,
nunca grava sozinho numa leitura)."""
import uuid

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin


class CashRegisterConfig(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "cash_register_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # --- "Exigir caixa aberto para..." (gates aplicados em
    # `services/orders.py::create_order`/`close_order` e
    # `services/appointments.py::create_appointment`, via
    # `services/cash_register.py::assert_operational_prerequisites`).
    require_open_register_for_order: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    require_open_register_for_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    require_open_register_for_appointment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    # --- Caixa de dia anterior ainda aberto.
    block_if_previous_day_open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    require_close_previous_before_opening_today: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    # --- Um caixa aberto por unidade — TOGGLE (por padrão preserva a
    # regra que já existia antes da Etapa H; desligar permite mais de
    # um caixa aberto simultâneo na mesma unidade).
    single_open_register_per_branch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # --- Quem pode abrir/fechar caixa, além do Proprietário (que nunca
    # é bloqueado por este toggle).
    allow_admin_open_close: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    allow_receptionist_open_close: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
