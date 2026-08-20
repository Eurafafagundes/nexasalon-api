import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.stock import StockTransfer


def get(session: Session, organization_id: uuid.UUID, transfer_id: uuid.UUID) -> StockTransfer | None:
    stmt = select(StockTransfer).where(
        StockTransfer.id == transfer_id, StockTransfer.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_for_org(session: Session, organization_id: uuid.UUID) -> list[StockTransfer]:
    stmt = (
        select(StockTransfer)
        .where(StockTransfer.organization_id == organization_id)
        .order_by(StockTransfer.created_at.desc())
    )
    return list(session.scalars(stmt).all())


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    origin_branch_id: uuid.UUID,
    destination_branch_id: uuid.UUID,
    quantity: Decimal,
    created_by: uuid.UUID,
    created_by_name: str,
    observation: str | None = None,
) -> StockTransfer:
    transfer = StockTransfer(
        organization_id=organization_id,
        product_id=product_id,
        origin_branch_id=origin_branch_id,
        destination_branch_id=destination_branch_id,
        quantity=quantity,
        observation=observation,
        created_by=created_by,
        created_by_name=created_by_name,
    )
    session.add(transfer)
    session.flush()
    session.refresh(transfer)  # normaliza `quantity` pra escala da coluna
    return transfer
