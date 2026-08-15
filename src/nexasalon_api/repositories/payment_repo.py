import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.models.enums import CardBrand, PaymentMethod
from nexasalon_api.models.order import Payment


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    order_id: uuid.UUID,
    method: PaymentMethod,
    card_brand: CardBrand | None,
    installments: int | None,
    amount: Decimal,
    created_by: uuid.UUID | None,
) -> Payment:
    payment = Payment(
        organization_id=organization_id,
        order_id=order_id,
        method=method,
        card_brand=card_brand,
        installments=installments,
        amount=amount,
        created_by=created_by,
    )
    session.add(payment)
    session.flush()
    return payment
