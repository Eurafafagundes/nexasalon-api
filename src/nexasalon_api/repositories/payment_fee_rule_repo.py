import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import CardBrand, PaymentMethod
from nexasalon_api.models.order import PaymentFeeRule


def get(session: Session, organization_id: uuid.UUID, rule_id: uuid.UUID) -> PaymentFeeRule | None:
    stmt = select(PaymentFeeRule).where(
        PaymentFeeRule.id == rule_id, PaymentFeeRule.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[PaymentFeeRule]:
    stmt = (
        select(PaymentFeeRule)
        .where(PaymentFeeRule.organization_id == organization_id)
        .order_by(PaymentFeeRule.method, PaymentFeeRule.card_brand, PaymentFeeRule.installments)
    )
    if not include_inactive:
        stmt = stmt.where(PaymentFeeRule.is_active.is_(True))
    return list(session.scalars(stmt).all())


def find_matching(
    session: Session,
    organization_id: uuid.UUID,
    *,
    method: PaymentMethod,
    card_brand: CardBrand | None,
    installments: int,
) -> PaymentFeeRule | None:
    """Resolução da taxa NO MOMENTO DA VENDA (`services/payment_fees.py`)
    — só considera regras ATIVAS (`is_active=True`); uma regra
    desativada passa a se comportar como "sem regra" (taxa não
    configurada), sem apagar histórico nem afetar pagamentos antigos
    que já a usaram (o snapshot deles é imutável)."""
    if card_brand is None:
        return None
    stmt = select(PaymentFeeRule).where(
        PaymentFeeRule.organization_id == organization_id,
        PaymentFeeRule.method == method,
        PaymentFeeRule.card_brand == card_brand,
        PaymentFeeRule.installments == installments,
        PaymentFeeRule.is_active.is_(True),
    )
    return session.scalars(stmt).first()


def create(session: Session, organization_id: uuid.UUID, **fields) -> PaymentFeeRule:
    rule = PaymentFeeRule(organization_id=organization_id, **fields)
    session.add(rule)
    session.flush()
    return rule


def save(session: Session, rule: PaymentFeeRule) -> PaymentFeeRule:
    session.flush()
    return rule
