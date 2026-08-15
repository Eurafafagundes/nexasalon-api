import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.order import OrderClose, OrderCreate, OrderItemPriceUpdate, OrderRead
from nexasalon_api.services import orders as orders_service

router = APIRouter(prefix="/orders", tags=["orders"])

_view = require_permission("orders.view")
_manage = require_permission("orders.manage")
_edit_price = require_permission("orders.edit_price")
_register_payment = require_permission("payments.register")


@router.post("", response_model=OrderRead, status_code=status.HTTP_201_CREATED, summary="Abrir comanda de um agendamento")
def create_order(
    payload: OrderCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> OrderRead:
    order = orders_service.create_order(session, actor, payload.appointment_id)
    return OrderRead.from_order(order)


@router.get("/by-appointment/{appointment_id}", response_model=OrderRead | None, summary="Comanda de um agendamento, se existir")
def get_order_by_appointment(
    appointment_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> OrderRead | None:
    order = orders_service.get_order_by_appointment(session, actor, appointment_id)
    return OrderRead.from_order(order) if order is not None else None


@router.get("/{order_id}", response_model=OrderRead, summary="Detalhe da comanda (itens, pagamentos, total)")
def get_order(
    order_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> OrderRead:
    order = orders_service.get_order(session, actor, order_id)
    return OrderRead.from_order(order)


@router.patch("/{order_id}/items/{item_id}", response_model=OrderRead, summary="Editar preço de uma linha da comanda")
def update_item_price(
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: OrderItemPriceUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_edit_price),
) -> OrderRead:
    order = orders_service.update_item_price(session, actor, order_id, item_id, payload)
    return OrderRead.from_order(order)


@router.post("/{order_id}/close", response_model=OrderRead, summary="Registrar pagamento(s) e fechar a comanda")
def close_order(
    order_id: uuid.UUID,
    payload: OrderClose,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_register_payment),
) -> OrderRead:
    order = orders_service.close_order(session, actor, order_id, payload)
    return OrderRead.from_order(order)
