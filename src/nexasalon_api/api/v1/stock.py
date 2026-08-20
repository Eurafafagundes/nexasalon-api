"""Rotas de Movimentação, Transferência e Visão Geral de estoque
(`/api/v1/stock-movements`, `/stock-transfers`, `/stock/overview`).

Mesma regra de `api/v1/products.py` pra custo: nenhuma rota que possa
incluir `unit_cost`/`stock_value` declara `response_model` — a escolha
de schema (com ou sem custo) acontece em Python, sobre a instância já
pronta, nunca reprocessada pelo FastAPI contra uma união de schemas."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import StockMovementDirection, StockMovementReason
from nexasalon_api.models.stock import StockMovement, StockTransfer
from nexasalon_api.repositories import stock_movement_repo
from nexasalon_api.schemas.stock import (
    StockMovementCreate,
    StockMovementRead,
    StockMovementReadWithCost,
    StockOverview,
    StockTransferCreate,
    StockTransferRead,
)
from nexasalon_api.services import stock as stock_service

router = APIRouter(tags=["stock"])

_view = require_permission("inventory.view")
_manage = require_permission("inventory.manage")


def _serialize_movement(movement: StockMovement, actor: ActorContext) -> StockMovementRead | StockMovementReadWithCost:
    if "inventory.view_cost" in actor.permissions:
        return StockMovementReadWithCost.model_validate(movement)
    return StockMovementRead.model_validate(movement)


def _serialize_transfer(transfer: StockTransfer, actor: ActorContext, session: Session) -> StockTransferRead:
    movements = stock_movement_repo.list_for_transfer(session, actor.organization_id, transfer.id)
    read = StockTransferRead.model_validate(transfer)
    read.movements = [StockMovementRead.model_validate(m) for m in movements]
    return read


@router.post("/stock-movements", status_code=status.HTTP_201_CREATED, summary="Registrar entrada ou saída de estoque")
def create_stock_movement(
    payload: StockMovementCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
):
    movement = stock_service.record_movement(
        session,
        actor,
        product_id=payload.product_id,
        branch_id=payload.branch_id,
        direction=payload.direction,
        reason=payload.reason,
        quantity=payload.quantity,
        unit_cost=payload.unit_cost,
        observation=payload.observation,
    )
    return _serialize_movement(movement, actor)


@router.get("/stock-movements", summary="Listar movimentações de estoque")
def list_stock_movements(
    product_id: uuid.UUID | None = Query(None),
    branch_id: uuid.UUID | None = Query(None),
    direction: StockMovementDirection | None = Query(None),
    reason: StockMovementReason | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
):
    movements = stock_service.list_movements(
        session,
        actor,
        product_id=product_id,
        branch_id=branch_id,
        direction=direction,
        reason=reason,
        date_from=date_from,
        date_to=date_to,
    )
    return [_serialize_movement(m, actor) for m in movements]


@router.post("/stock-transfers", response_model=StockTransferRead, status_code=status.HTTP_201_CREATED, summary="Transferir produto entre unidades")
def create_stock_transfer(
    payload: StockTransferCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> StockTransferRead:
    transfer = stock_service.create_transfer(
        session,
        actor,
        product_id=payload.product_id,
        origin_branch_id=payload.origin_branch_id,
        destination_branch_id=payload.destination_branch_id,
        quantity=payload.quantity,
        observation=payload.observation,
    )
    return _serialize_transfer(transfer, actor, session)


@router.get("/stock-transfers", response_model=list[StockTransferRead], summary="Listar transferências entre unidades")
def list_stock_transfers(
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[StockTransferRead]:
    transfers = stock_service.list_transfers(session, actor)
    return [_serialize_transfer(t, actor, session) for t in transfers]


@router.get("/stock-transfers/{transfer_id}", response_model=StockTransferRead, summary="Detalhar transferência")
def get_stock_transfer(
    transfer_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> StockTransferRead:
    transfer = stock_service.get_transfer(session, actor, transfer_id)
    return _serialize_transfer(transfer, actor, session)


@router.get(
    "/stock/overview",
    response_model=StockOverview,
    summary="Visão Geral do Estoque — KPIs, estoque baixo, mais consumidos, fluxo",
)
def get_stock_overview(
    branch_id: uuid.UUID | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
):
    include_cost = "inventory.view_cost" in actor.permissions
    data = stock_service.get_overview(
        session, actor, branch_id=branch_id, date_from=date_from, date_to=date_to, include_cost=include_cost
    )
    return StockOverview(**data)
