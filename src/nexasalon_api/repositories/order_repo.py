import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.enums import OrderStatus
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


def _next_order_number(session: Session, organization_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(Order.order_number), 0) + 1).where(
        Order.organization_id == organization_id
    )
    return session.scalar(stmt) or 1


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
        order_number=_next_order_number(session, organization_id),
        appointment_id=appointment_id,
        branch_id=branch_id,
        client_id=client_id,
        created_by=created_by,
    )
    session.add(order)
    session.flush()
    return order


def list_for_client(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[Order]:
    """Histórico do cliente (item "universal, derivado de Comandas") —
    só comandas FECHADAS (venda concluída), mais recente primeiro."""
    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(
            Order.organization_id == organization_id,
            Order.client_id == client_id,
            Order.status == OrderStatus.CLOSED,
        )
        .order_by(Order.closed_at.desc())
    )
    return list(session.scalars(stmt).all())


def list_for_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    status: OrderStatus | None = None,
    client_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
    order_number: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[Order]:
    """Comandas Abertas/Finalizadas (Financeiro > Comandas) com filtros
    simples — profissional filtra por qualquer item da comanda conter
    aquele profissional."""
    stmt = select(Order).options(selectinload(Order.items), selectinload(Order.payments)).where(
        Order.organization_id == organization_id
    )
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if client_id is not None:
        stmt = stmt.where(Order.client_id == client_id)
    if order_number is not None:
        stmt = stmt.where(Order.order_number == order_number)
    if date_from is not None:
        stmt = stmt.where(Order.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Order.created_at <= date_to)
    if professional_id is not None:
        stmt = stmt.where(Order.items.any(professional_id=professional_id))
    return list(session.scalars(stmt.order_by(Order.created_at.desc())).all())
