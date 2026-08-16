import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.cash_register import CashMovement
from nexasalon_api.models.enums import CashMovementType, PaymentMethod


def list_for_register(session: Session, organization_id: uuid.UUID, cash_register_id: uuid.UUID) -> list[CashMovement]:
    stmt = (
        select(CashMovement)
        .where(CashMovement.organization_id == organization_id, CashMovement.cash_register_id == cash_register_id)
        .order_by(CashMovement.created_at)
    )
    return list(session.scalars(stmt).all())


def list_for_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    type: CashMovementType | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[CashMovement]:
    """Extrato (item "Movimentações") — todas as entradas/despesas
    manuais da organização num período, independente de qual caixa."""
    stmt = select(CashMovement).where(CashMovement.organization_id == organization_id)
    if type is not None:
        stmt = stmt.where(CashMovement.type == type)
    if date_from is not None:
        stmt = stmt.where(CashMovement.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(CashMovement.created_at <= date_to)
    return list(session.scalars(stmt.order_by(CashMovement.created_at.desc())).all())


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    cash_register_id: uuid.UUID,
    type: CashMovementType,
    amount: Decimal,
    description: str,
    created_by: uuid.UUID,
    created_by_name: str,
    category: str | None = None,
    method: PaymentMethod = PaymentMethod.CASH,
) -> CashMovement:
    movement = CashMovement(
        organization_id=organization_id,
        cash_register_id=cash_register_id,
        type=type,
        amount=amount,
        description=description,
        category=category,
        method=method,
        created_by=created_by,
        created_by_name=created_by_name,
    )
    session.add(movement)
    session.flush()
    return movement
