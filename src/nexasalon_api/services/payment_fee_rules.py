"""Camada de negócio de `PaymentFeeRule` (Etapa N3, Configurações >
Taxas de Pagamento) — cada organização cadastra as próprias, nenhuma
taxa fixa no código.

Pix (Etapa N3.1) entra na MESMA entidade/fluxo de débito/crédito —
nunca um cálculo ou cadastro paralelo. A única diferença de domínio é
que Pix não tem bandeira: a API aceita/expõe `card_brand=None` pra Pix
(`schemas/payment_fee_rule.py`), e aqui na borda do serviço isso é
convertido pro sentinela `CardBrand.NOT_APPLICABLE` (coluna `NOT NULL`
no banco — ver docstring de `models/order.py::PaymentFeeRule`) antes de
persistir."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.enums import AuditAction, CardBrand, PaymentMethod
from nexasalon_api.models.order import PaymentFeeRule
from nexasalon_api.repositories import audit_log_repo, payment_fee_rule_repo
from nexasalon_api.schemas.payment_fee_rule import (
    PaymentFeeRuleCreate,
    PaymentFeeRuleUpdate,
)

_ENTITY_TYPE = "payment_fee_rule"


def list_rules(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[PaymentFeeRule]:
    return payment_fee_rule_repo.list_all(session, organization_id, include_inactive)


def get_rule(session: Session, organization_id: uuid.UUID, rule_id: uuid.UUID) -> PaymentFeeRule:
    rule = payment_fee_rule_repo.get(session, organization_id, rule_id)
    if rule is None:
        raise NotFoundError("Taxa de pagamento não encontrada.")
    return rule


def _stored_card_brand(method: PaymentMethod, card_brand: CardBrand | None) -> CardBrand:
    """Mapeia o `None` da API (Pix sem bandeira) pro sentinela interno —
    a única função que conhece essa conversão; o resto do serviço/repo
    só lida com `CardBrand` concreto."""
    return CardBrand.NOT_APPLICABLE if method == PaymentMethod.PIX else card_brand  # type: ignore[return-value]


def _audit_values(rule: PaymentFeeRule) -> dict[str, str | bool | None]:
    """Só configuração (forma/bandeira/parcelas/percentual/status) —
    nunca dado de pagamento/cliente."""
    return {
        "method": rule.method.value,
        "card_brand": None if rule.card_brand == CardBrand.NOT_APPLICABLE else rule.card_brand.value,
        "installments": str(rule.installments),
        "fee_percent": str(rule.fee_percent),
        "is_active": rule.is_active,
    }


def create_rule(session: Session, actor: ActorContext, data: PaymentFeeRuleCreate) -> PaymentFeeRule:
    installments = 1 if data.method in (PaymentMethod.DEBIT, PaymentMethod.PIX) else data.installments
    card_brand = _stored_card_brand(data.method, data.card_brand)
    existing = payment_fee_rule_repo.find_matching(
        session, actor.organization_id, method=data.method, card_brand=card_brand, installments=installments
    )
    if existing is not None:
        raise ConflictError(
            "Já existe uma taxa cadastrada para esta combinação de forma, bandeira e parcelas "
            "(ative-a ou edite o percentual em vez de criar outra)."
        )
    rule = payment_fee_rule_repo.create(
        session, actor.organization_id,
        method=data.method, card_brand=card_brand, installments=installments, fee_percent=data.fee_percent,
    )
    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id,
        entity_type=_ENTITY_TYPE, entity_id=rule.id, action=AuditAction.CREATE,
        new_values=_audit_values(rule),
    )
    return rule


def update_rule(
    session: Session, actor: ActorContext, rule_id: uuid.UUID, data: PaymentFeeRuleUpdate
) -> PaymentFeeRule:
    """Só `fee_percent` — ver docstring de `PaymentFeeRuleUpdate`. Nunca
    recalcula pagamentos já criados com o percentual anterior (o
    snapshot em `Payment` é congelado no momento da venda)."""
    rule = get_rule(session, actor.organization_id, rule_id)
    old_values = _audit_values(rule)
    rule.fee_percent = data.fee_percent
    saved = payment_fee_rule_repo.save(session, rule)
    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id,
        entity_type=_ENTITY_TYPE, entity_id=saved.id, action=AuditAction.UPDATE,
        old_values=old_values, new_values=_audit_values(saved),
    )
    return saved


def set_rule_active(session: Session, actor: ActorContext, rule_id: uuid.UUID, is_active: bool) -> PaymentFeeRule:
    """Desativar (não apagar) — uma regra desativada passa a se
    comportar como "sem regra" pra vendas NOVAS (taxa não configurada),
    mas pagamentos antigos que já a usaram mantêm o snapshot intacto
    (`payment_fee_rule_id` continua apontando pra ela)."""
    rule = get_rule(session, actor.organization_id, rule_id)
    old_values = _audit_values(rule)
    rule.is_active = is_active
    saved = payment_fee_rule_repo.save(session, rule)
    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id,
        entity_type=_ENTITY_TYPE, entity_id=saved.id, action=AuditAction.UPDATE,
        old_values=old_values, new_values=_audit_values(saved),
    )
    return saved
