"""Schemas de `Financeiro > Caixa > Configurações do Caixa` (Etapa H).
Um único `PUT` sempre substitui o estado efetivo inteiro (mesmo padrão
de `AppointmentStatusStyleUpdate`) — o frontend sempre envia os 8
campos com o valor atual de cada toggle, nunca um PATCH parcial."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CashRegisterConfigUpdate(BaseModel):
    require_open_register_for_order: bool
    require_open_register_for_payment: bool
    require_open_register_for_appointment: bool
    block_if_previous_day_open: bool
    require_close_previous_before_opening_today: bool
    single_open_register_per_branch: bool
    allow_admin_open_close: bool
    allow_receptionist_open_close: bool


class CashRegisterConfigRead(CashRegisterConfigUpdate):
    model_config = ConfigDict(from_attributes=True)

    # `None`/`None` quando a organização ainda não gravou nenhuma
    # configuração (está operando 100% nos defaults de fábrica).
    updated_at: datetime | None
    updated_by_name: str | None
