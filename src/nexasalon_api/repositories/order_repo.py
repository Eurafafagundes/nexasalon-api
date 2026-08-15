import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.order import Order


def get(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order | None:
    # `populate_existing=True` — mesmo motivo de `appointment_repo.get`:
    # este repo é consultado de novo logo depois de editar preço/fechar
    # a comanda dentro da mesma transação/identity map.
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.id == order_id, Order.organization_id == organization_id)
        .execution_options(populate_existing=True)
    )
    return session.scalars(stmt).first()


def get_by_appointment(session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID) -> Order | None:
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.appointment_id == appointment_id, Order.organization_id == organization_id)
        .execution_options(populate_existing=True)
    )
    return session.scalars(stmt).first()


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    appointment_id: uuid.UUID,
    branch_id: uuid.UUID,
    client_id: uuid.UUID,
    created_by: uuid.UUID | None,
) -> Order:
    order = Order(
        organization_id=organization_id,
        appointment_id=appointment_id,
        branch_id=branch_id,
        client_id=client_id,
        created_by=created_by,
    )
    session.add(order)
    session.flush()
    return order
