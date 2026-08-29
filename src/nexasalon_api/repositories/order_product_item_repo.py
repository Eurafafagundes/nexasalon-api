import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.models.enums import OrderProductItemKind
from nexasalon_api.models.order import OrderProductItem


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    order_id: uuid.UUID,
    product_id: uuid.UUID,
    product_name: str,
    quantity: Decimal,
    unit_price: Decimal,
    item_type: OrderProductItemKind = OrderProductItemKind.SALE,
) -> OrderProductItem:
    item = OrderProductItem(
        organization_id=organization_id,
        order_id=order_id,
        product_id=product_id,
        product_name=product_name,
        quantity=quantity,
        unit_price=unit_price,
        item_type=item_type,
    )
    session.add(item)
    session.flush()
    return item


def delete(session: Session, item: OrderProductItem) -> None:
    session.delete(item)
    session.flush()
