import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.stock import StockMovement
from nexasalon_api.models.enums import StockMovementDirection, StockMovementReason


def get(session: Session, organization_id: uuid.UUID, movement_id: uuid.UUID) -> StockMovement | None:
    stmt = select(StockMovement).where(
        StockMovement.id == movement_id, StockMovement.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_for_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    product_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    direction: StockMovementDirection | None = None,
    reason: StockMovementReason | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[StockMovement]:
    stmt = select(StockMovement).where(StockMovement.organization_id == organization_id)
    if product_id is not None:
        stmt = stmt.where(StockMovement.product_id == product_id)
    if branch_id is not None:
        stmt = stmt.where(StockMovement.branch_id == branch_id)
    if direction is not None:
        stmt = stmt.where(StockMovement.direction == direction)
    if reason is not None:
        stmt = stmt.where(StockMovement.reason == reason)
    if date_from is not None:
        stmt = stmt.where(StockMovement.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(StockMovement.created_at <= date_to)
    stmt = stmt.order_by(StockMovement.created_at.desc())
    return list(session.scalars(stmt).all())


def list_for_transfer(session: Session, organization_id: uuid.UUID, transfer_id: uuid.UUID) -> list[StockMovement]:
    stmt = select(StockMovement).where(
        StockMovement.organization_id == organization_id, StockMovement.transfer_id == transfer_id
    ).order_by(StockMovement.created_at)
    return list(session.scalars(stmt).all())


def list_for_inventory_count(
    session: Session, organization_id: uuid.UUID, inventory_count_id: uuid.UUID
) -> list[StockMovement]:
    stmt = select(StockMovement).where(
        StockMovement.organization_id == organization_id,
        StockMovement.inventory_count_id == inventory_count_id,
    ).order_by(StockMovement.created_at)
    return list(session.scalars(stmt).all())


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    direction: StockMovementDirection,
    reason: StockMovementReason,
    quantity: Decimal,
    created_by: uuid.UUID,
    created_by_name: str,
    unit_cost: Decimal | None = None,
    observation: str | None = None,
    order_id: uuid.UUID | None = None,
    transfer_id: uuid.UUID | None = None,
    inventory_count_id: uuid.UUID | None = None,
) -> StockMovement:
    movement = StockMovement(
        organization_id=organization_id,
        product_id=product_id,
        branch_id=branch_id,
        direction=direction,
        reason=reason,
        quantity=quantity,
        unit_cost=unit_cost,
        observation=observation,
        created_by=created_by,
        created_by_name=created_by_name,
        order_id=order_id,
        transfer_id=transfer_id,
        inventory_count_id=inventory_count_id,
    )
    session.add(movement)
    session.flush()
    session.refresh(movement)  # normaliza `quantity`/`unit_cost` pra escala da coluna
    return movement
