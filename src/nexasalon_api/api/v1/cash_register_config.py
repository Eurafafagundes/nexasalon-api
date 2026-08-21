from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.cash_register_config import CashRegisterConfigRead, CashRegisterConfigUpdate
from nexasalon_api.services import cash_register_config as service

router = APIRouter(prefix="/cash-register-config", tags=["cash-register-config"])

# Reaproveita `settings.manage` (migration 0007) — mesma permissão de
# `appointment_status_styles.py`, não cria chave nova. Só OWNER/ADMIN
# têm por padrão: quem só abre/fecha/movimenta caixa (`finance.*`) não
# reconfigura as regras do Caixa.
_manage = require_permission("settings.manage")


@router.get(
    "", response_model=CashRegisterConfigRead,
    summary="Configurações do Caixa (Financeiro > Caixa > Configurações do Caixa)",
)
def get_config(session: Session = Depends(get_db), actor: ActorContext = Depends(_manage)) -> CashRegisterConfigRead:
    return service.get_config_for_display(session, actor)


@router.put("", response_model=CashRegisterConfigRead, summary="Atualizar as Configurações do Caixa")
def update_config(
    payload: CashRegisterConfigUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> CashRegisterConfigRead:
    return service.update_config(session, actor, payload)
