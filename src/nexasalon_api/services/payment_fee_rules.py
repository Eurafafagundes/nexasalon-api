"""Camada de negócio de `PaymentFeeRule` (Etapa N3, Configurações >
Taxas de Pagamento) — cada organização cadastra as próprias, nenhuma
taxa fixa no código."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.enums import PaymentMethod
from nexasalon_api.models.order import PaymentFeeRule
from nexasalon_api.repositories import payment_fee_rule_repo
from nexasalon_api.schemas.payment_fee_rule import (
    PaymentFeeRuleCreate,
    PaymentFeeRuleUpdate,
)


def list_rules(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[PaymentFeeRule]:
    return payment_fee_rule_repo.list_all(session, organization_id, include_inactive)


def get_rule(session: Session, organization_id: uuid.UUID, rule_id: uuid.UUID) -> PaymentFeeRule:
    rule = payment_fee_rule_repo.get(session, organization_id, rule_id)
    if rule is None:
        raise NotFoundError("Taxa de pagamento não encontrada.")
    return rule


def create_rule(session: Session, organization_id: uuid.UUID, data: PaymentFeeRuleCreate) -> PaymentFeeRule:
    installments = 1 if data.method == PaymentMethod.DEBIT else data.installments
    existing = payment_fee_rule_repo.find_matching(
        session, organization_id, method=data.method, card_brand=data.card_brand, installments=installments
    )
    if existing is not None:
        raise ConflictError(
            "Já existe uma taxa cadastrada para esta combinação de forma, bandeira e parcelas "
            "(ative-a ou edite o percentual em vez de criar outra)."
        )
    return payment_fee_rule_repo.create(
        session, organization_id,
        method=data.method, card_brand=data.card_brand, installments=installments, fee_percent=data.fee_percent,
    )


def update_rule(
    session: Session, organization_id: uuid.UUID, rule_id: uuid.UUID, data: PaymentFeeRuleUpdate
) -> PaymentFeeRule:
    """Só `fee_percent` — ver docstring de `PaymentFeeRuleUpdate`. Nunca
    recalcula pagamentos já criados com o percentual anterior (o
    snapshot em `Payment` é congelado no momento da venda)."""
    rule = get_rule(session, organization_id, rule_id)
    rule.fee_percent = data.fee_percent
    return payment_fee_rule_repo.save(session, rule)


def set_rule_active(session: Session, organization_id: uuid.UUID, rule_id: uuid.UUID, is_active: bool) -> PaymentFeeRule:
    """Desativar (não apagar) — uma regra desativada passa a se
    comportar como "sem regra" pra vendas NOVAS (taxa não configurada),
    mas pagamentos antigos que já a usaram mantêm o snapshot intacto
    (`payment_fee_rule_id` continua apontando pra ela)."""
    rule = get_rule(session, organization_id, rule_id)
    rule.is_active = is_active
    return payment_fee_rule_repo.save(session, rule)
