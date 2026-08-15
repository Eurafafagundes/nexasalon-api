import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.models.order import OrderItem


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
) -> OrderItem:
    item = OrderItem(
        organization_id=organization_id,
        order_id=order_id,
        appointment_item_id=appointment_item_id,
        service_id=service_id,
        professional_id=professional_id,
        duration_minutes=duration_minutes,
        price=price,
    )
    session.add(item)
    session.flush()
    return item
