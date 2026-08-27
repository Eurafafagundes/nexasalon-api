import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import CommissionStatus, OrderStatus
from nexasalon_api.models.order import Order, OrderItem


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    order_id: uuid.UUID,
    appointment_item_id: uuid.UUID | None,
    service_id: uuid.UUID,
    professional_id: uuid.UUID,
    duration_minutes: int,
    price: Decimal,
    service_name: str,
    professional_name: str,
) -> OrderItem:
    item = OrderItem(
        organization_id=organization_id,
        order_id=order_id,
        appointment_item_id=appointment_item_id,
        service_id=service_id,
        professional_id=professional_id,
        duration_minutes=duration_minutes,
        price=price,
        service_name=service_name,
        professional_name=professional_name,
    )
    session.add(item)
    session.flush()
    return item


def lock_pending_commission_items(
    session: Session,
    organization_id: uuid.UUID,
    professional_id: uuid.UUID,
    *,
    date_from: datetime,
    date_to: datetime,
) -> list[OrderItem]:
    """Etapa C4 — candidatos a "Registrar pagamento": comissão
    CALCULADA, ainda não linkada a nenhum settlement, dentro do
    período (competência = `Order.closed_at`, mesma coluna de
    `services/commissions.py::get_overview`). `not_configured` e
    histórico (`commission_status IS NULL`) nunca aparecem aqui — já
    excluídos pelo `WHERE`, reforçado também por CHECK no banco (ver
    migration 0038).

    `SELECT ... FOR UPDATE OF order_items` (mesmo raciocínio de
    concorrência de `order_repo.get_for_update`): trava só a linha do
    ITEM, não a `Order` (que não é alterada aqui). Sob READ COMMITTED
    (padrão do Postgres), uma segunda chamada concorrente pro MESMO
    profissional+período bloqueia até a primeira commitar e, ao ser
    liberada, reavalia o `WHERE` (`commission_settlement_id IS NULL`)
    contra o dado JÁ atualizado pela primeira — os itens que a
    primeira já reivindicou somem naturalmente do resultado da
    segunda. A trava E a leitura são o mesmo `SELECT`: não existe
    janela entre "travar" e "reverificar" pra explorar."""
    stmt = (
        select(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.organization_id == organization_id,
            Order.status == OrderStatus.CLOSED,
            OrderItem.professional_id == professional_id,
            OrderItem.commission_status == CommissionStatus.CALCULATED,
            OrderItem.commission_settlement_id.is_(None),
            Order.closed_at >= date_from,
            Order.closed_at <= date_to,
        )
        .order_by(Order.closed_at)
        .with_for_update(of=OrderItem)
    )
    return list(session.scalars(stmt).all())
