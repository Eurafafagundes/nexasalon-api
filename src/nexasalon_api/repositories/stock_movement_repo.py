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


def get_by_idempotency_key(
    session: Session, organization_id: uuid.UUID, idempotency_key: uuid.UUID
) -> StockMovement | None:
    """Idempotência de correção pós-fechamento (ver docstring de
    `models/stock.py::StockMovement.idempotency_key`) — devolve a
    movimentação JÁ criada por uma tentativa anterior com a mesma
    chave, se existir, pra `services/orders.py::correct_consumption`
    nunca criar uma segunda compensação."""
    stmt = select(StockMovement).where(
        StockMovement.organization_id == organization_id, StockMovement.idempotency_key == idempotency_key
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


def list_for_transfers(
    session: Session, organization_id: uuid.UUID, transfer_ids: list[uuid.UUID]
) -> list[StockMovement]:
    """Busca em LOTE (`WHERE transfer_id IN (...)`) — item de
    performance (Etapa 2A, "quick win 4"): evita 1 `list_for_transfer`
    por transferência quando o chamador precisa dos movimentos de
    VÁRIAS transferências de uma vez (`GET /stock-transfers`, listagem
    — ver `api/v1/stock.py`). Quem chama agrupa por `transfer_id` em
    Python; `list_for_transfer` (acima) continua igual, usada pelos
    endpoints de UMA transferência só (criar/detalhar)."""
    if not transfer_ids:
        return []
    stmt = select(StockMovement).where(
        StockMovement.organization_id == organization_id, StockMovement.transfer_id.in_(transfer_ids)
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
    idempotency_key: uuid.UUID | None = None,
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
        idempotency_key=idempotency_key,
    )
    session.add(movement)
    session.flush()
    session.refresh(movement)  # normaliza `quantity`/`unit_cost` pra escala da coluna
    return movement
