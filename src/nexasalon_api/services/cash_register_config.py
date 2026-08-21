"""`Financeiro > Caixa > Configurações do Caixa` (Etapa H).

A LEITURA/ESCRITA da configuração em si é exposta só por
`api/v1/cash_register_config.py`, gated por `settings.manage`
(reaproveitado — mesma permissão de `appointment_status_styles.py` —
só OWNER/ADMIN por padrão; não cria chave nova). As REGRAS DE NEGÓCIO
derivadas daqui (exigir caixa aberto, bloquear dia anterior etc.) são
lidas livremente por `services/cash_register.py`,
`services/orders.py` e `services/appointments.py` via
`get_effective_config` — sem checagem de permissão adicional, não é
uma leitura exposta por rota nenhuma além da própria tela de
configuração.

`get_effective_config` NUNCA grava sozinho: uma organização que nunca
abriu esta tela opera exatamente com os defaults abaixo — os mesmos
valores pedidos pela Etapa H (tudo ON, exceto "exigir caixa aberto
para criar agendamento", que nasce OFF)."""
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.cash_register_config import CashRegisterConfig
from nexasalon_api.repositories import cash_register_config_repo, user_repo
from nexasalon_api.schemas.cash_register_config import CashRegisterConfigRead, CashRegisterConfigUpdate


@dataclass(frozen=True)
class CashRegisterConfigDefaults:
    require_open_register_for_order: bool = True
    require_open_register_for_payment: bool = True
    require_open_register_for_appointment: bool = False
    block_if_previous_day_open: bool = True
    require_close_previous_before_opening_today: bool = True
    single_open_register_per_branch: bool = True
    allow_admin_open_close: bool = True
    allow_receptionist_open_close: bool = True


DEFAULT_CONFIG = CashRegisterConfigDefaults()

EffectiveConfig = CashRegisterConfig | CashRegisterConfigDefaults


def get_effective_config(session: Session, organization_id: uuid.UUID) -> EffectiveConfig:
    return cash_register_config_repo.get(session, organization_id) or DEFAULT_CONFIG


def get_config_for_display(session: Session, actor: ActorContext) -> CashRegisterConfigRead:
    config = get_effective_config(session, actor.organization_id)
    updated_at = None
    updated_by_name = None
    if isinstance(config, CashRegisterConfig):
        updated_at = config.updated_at
        if config.updated_by is not None:
            user = user_repo.get(session, config.updated_by)
            updated_by_name = user.name if user is not None else None
    return CashRegisterConfigRead(
        require_open_register_for_order=config.require_open_register_for_order,
        require_open_register_for_payment=config.require_open_register_for_payment,
        require_open_register_for_appointment=config.require_open_register_for_appointment,
        block_if_previous_day_open=config.block_if_previous_day_open,
        require_close_previous_before_opening_today=config.require_close_previous_before_opening_today,
        single_open_register_per_branch=config.single_open_register_per_branch,
        allow_admin_open_close=config.allow_admin_open_close,
        allow_receptionist_open_close=config.allow_receptionist_open_close,
        updated_at=updated_at,
        updated_by_name=updated_by_name,
    )


def update_config(session: Session, actor: ActorContext, data: CashRegisterConfigUpdate) -> CashRegisterConfigRead:
    cash_register_config_repo.upsert(
        session, actor.organization_id, values=data.model_dump(), updated_by=actor.user_id
    )
    return get_config_for_display(session, actor)
