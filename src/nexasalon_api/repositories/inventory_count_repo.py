import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.stock import InventoryCount, InventoryCountItem
from nexasalon_api.models.enums import InventoryCountStatus


def get(session: Session, organization_id: uuid.UUID, count_id: uuid.UUID) -> InventoryCount | None:
    stmt = select(InventoryCount).where(
        InventoryCount.id == count_id, InventoryCount.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_for_org(
    session: Session, organization_id: uuid.UUID, *, status: InventoryCountStatus | None = None
) -> list[InventoryCount]:
    stmt = select(InventoryCount).where(InventoryCount.organization_id == organization_id)
    if status is not None:
        stmt = stmt.where(InventoryCount.status == status)
    stmt = stmt.order_by(InventoryCount.created_at.desc())
    return list(session.scalars(stmt).all())


def get_open_for_branch(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> InventoryCount | None:
    stmt = select(InventoryCount).where(
        InventoryCount.organization_id == organization_id,
        InventoryCount.branch_id == branch_id,
        InventoryCount.status == InventoryCountStatus.OPEN,
    )
    return session.scalars(stmt).first()


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    branch_id: uuid.UUID,
    created_by: uuid.UUID,
    created_by_name: str,
    notes: str | None = None,
) -> InventoryCount:
    count = InventoryCount(
        organization_id=organization_id,
        branch_id=branch_id,
        created_by=created_by,
        created_by_name=created_by_name,
        notes=notes,
    )
    session.add(count)
    session.flush()
    return count


def add_item(
    session: Session,
    organization_id: uuid.UUID,
    inventory_count_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    system_quantity: Decimal,
) -> InventoryCountItem:
    item = InventoryCountItem(
        organization_id=organization_id,
        inventory_count_id=inventory_count_id,
        product_id=product_id,
        system_quantity=system_quantity,
    )
    session.add(item)
    session.flush()
    return item


def get_item(session: Session, organization_id: uuid.UUID, inventory_count_id: uuid.UUID, product_id: uuid.UUID) -> InventoryCountItem | None:
    stmt = select(InventoryCountItem).where(
        InventoryCountItem.organization_id == organization_id,
        InventoryCountItem.inventory_count_id == inventory_count_id,
        InventoryCountItem.product_id == product_id,
    )
    return session.scalars(stmt).first()


def list_items(session: Session, organization_id: uuid.UUID, inventory_count_id: uuid.UUID) -> list[InventoryCountItem]:
    stmt = select(InventoryCountItem).where(
        InventoryCountItem.organization_id == organization_id,
        InventoryCountItem.inventory_count_id == inventory_count_id,
    ).order_by(InventoryCountItem.created_at)
    return list(session.scalars(stmt).all())


def close(
    session: Session,
    count: InventoryCount,
    *,
    closed_by: uuid.UUID,
    closed_by_name: str,
    closed_at: datetime,
) -> InventoryCount:
    count.status = InventoryCountStatus.CLOSED
    count.closed_by = closed_by
    count.closed_by_name = closed_by_name
    count.closed_at = closed_at
    session.flush()
    return count
