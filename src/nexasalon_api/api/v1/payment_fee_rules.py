"""Rotas de Taxas de Pagamento (`/api/v1/payment-fee-rules`) — Etapa N3.

Reaproveita a permission `organization.manage` já existente (mesma
fronteira de autorização de Configurações > Informações do
Estabelecimento / Agendamento Online) — nenhuma permission nova."""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.payment_fee_rule import (
    PaymentFeeRuleCreate,
    PaymentFeeRuleRead,
    PaymentFeeRuleUpdate,
)
from nexasalon_api.services import payment_fee_rules as payment_fee_rules_service

router = APIRouter(prefix="/payment-fee-rules", tags=["payment-fee-rules"])

_view = require_permission("organization.manage")
_manage = require_permission("organization.manage")


@router.get("", response_model=list[PaymentFeeRuleRead], summary="Listar taxas de pagamento")
def list_payment_fee_rules(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[PaymentFeeRuleRead]:
    rules = payment_fee_rules_service.list_rules(session, actor.organization_id, include_inactive)
    return [PaymentFeeRuleRead.model_validate(r) for r in rules]


@router.post(
    "", response_model=PaymentFeeRuleRead, status_code=status.HTTP_201_CREATED, summary="Criar taxa de pagamento"
)
def create_payment_fee_rule(
    payload: PaymentFeeRuleCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> PaymentFeeRuleRead:
    rule = payment_fee_rules_service.create_rule(session, actor.organization_id, payload)
    return PaymentFeeRuleRead.model_validate(rule)


@router.put("/{rule_id}", response_model=PaymentFeeRuleRead, summary="Editar percentual da taxa")
def update_payment_fee_rule(
    rule_id: uuid.UUID,
    payload: PaymentFeeRuleUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> PaymentFeeRuleRead:
    rule = payment_fee_rules_service.update_rule(session, actor.organization_id, rule_id, payload)
    return PaymentFeeRuleRead.model_validate(rule)


@router.patch("/{rule_id}/activate", response_model=PaymentFeeRuleRead, summary="Ativar taxa de pagamento")
def activate_payment_fee_rule(
    rule_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> PaymentFeeRuleRead:
    rule = payment_fee_rules_service.set_rule_active(session, actor.organization_id, rule_id, True)
    return PaymentFeeRuleRead.model_validate(rule)


@router.patch("/{rule_id}/deactivate", response_model=PaymentFeeRuleRead, summary="Desativar taxa de pagamento")
def deactivate_payment_fee_rule(
    rule_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> PaymentFeeRuleRead:
    rule = payment_fee_rules_service.set_rule_active(session, actor.organization_id, rule_id, False)
    return PaymentFeeRuleRead.model_validate(rule)
