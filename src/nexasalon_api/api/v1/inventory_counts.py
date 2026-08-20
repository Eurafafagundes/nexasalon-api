"""Rotas do fluxo de Inventário (`/api/v1/inventory-counts`)."""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import InventoryCountStatus
from nexasalon_api.repositories import inventory_count_repo
from nexasalon_api.schemas.inventory_count import (
    InventoryCountCreate,
    InventoryCountDetail,
    InventoryCountItemCount,
    InventoryCountItemReadOut,
    InventoryCountRead,
)
from nexasalon_api.services import inventory_counts as inventory_counts_service

router = APIRouter(prefix="/inventory-counts", tags=["inventory-counts"])

_view = require_permission("inventory.view")
_manage = require_permission("inventory.manage")


def _to_detail(session: Session, actor: ActorContext, count) -> InventoryCountDetail:
    items = inventory_count_repo.list_items(session, actor.organization_id, count.id)
    return InventoryCountDetail(
        id=count.id,
        branch_id=count.branch_id,
        status=count.status,
        notes=count.notes,
        created_by=count.created_by,
        created_by_name=count.created_by_name,
        closed_at=count.closed_at,
        closed_by=count.closed_by,
        closed_by_name=count.closed_by_name,
        created_at=count.created_at,
        items=[InventoryCountItemReadOut.from_model(item) for item in items],
    )


@router.post("", response_model=InventoryCountDetail, status_code=status.HTTP_201_CREATED, summary="Abrir inventário de uma unidade")
def open_inventory_count(
    payload: InventoryCountCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> InventoryCountDetail:
    count = inventory_counts_service.open_count(session, actor, branch_id=payload.branch_id, notes=payload.notes)
    return _to_detail(session, actor, count)


@router.get("", response_model=list[InventoryCountRead], summary="Listar inventários")
def list_inventory_counts(
    status_filter: InventoryCountStatus | None = Query(None, alias="status"),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[InventoryCountRead]:
    counts = inventory_counts_service.list_counts(session, actor, status=status_filter)
    return [InventoryCountRead.model_validate(c) for c in counts]


@router.get("/{count_id}", response_model=InventoryCountDetail, summary="Detalhar inventário (com itens)")
def get_inventory_count(
    count_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> InventoryCountDetail:
    count = inventory_counts_service.get_count(session, actor, count_id)
    return _to_detail(session, actor, count)


@router.put(
    "/{count_id}/items/{product_id}",
    response_model=InventoryCountDetail,
    summary="Registrar a contagem real de um produto",
)
def set_inventory_count_item(
    count_id: uuid.UUID,
    product_id: uuid.UUID,
    payload: InventoryCountItemCount,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> InventoryCountDetail:
    inventory_counts_service.set_item_count(session, actor, count_id, product_id, payload.counted_quantity)
    count = inventory_counts_service.get_count(session, actor, count_id)
    return _to_detail(session, actor, count)


@router.post("/{count_id}/close", response_model=InventoryCountDetail, summary="Fechar inventário e aplicar ajustes")
def close_inventory_count(
    count_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> InventoryCountDetail:
    count = inventory_counts_service.close_count(session, actor, count_id)
    return _to_detail(session, actor, count)
