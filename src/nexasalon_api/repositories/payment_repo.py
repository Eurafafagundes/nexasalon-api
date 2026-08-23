import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import CardBrand, PaymentFeeStatus, PaymentMethod
from nexasalon_api.models.order import Payment


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    order_id: uuid.UUID,
    cash_register_id: uuid.UUID,
    method: PaymentMethod,
    card_brand: CardBrand | None,
    installments: int | None,
    amount: Decimal,
    created_by: uuid.UUID | None,
    created_by_name: str | None = None,
    payment_fee_rule_id: uuid.UUID | None = None,
    fee_percent_snapshot: Decimal | None = None,
    fee_amount_snapshot: Decimal | None = None,
    net_amount_snapshot: Decimal | None = None,
    fee_status: PaymentFeeStatus | None = None,
) -> Payment:
    payment = Payment(
        organization_id=organization_id,
        order_id=order_id,
        cash_register_id=cash_register_id,
        method=method,
        card_brand=card_brand,
        installments=installments,
        amount=amount,
        created_by=created_by,
        created_by_name=created_by_name,
        payment_fee_rule_id=payment_fee_rule_id,
        fee_percent_snapshot=fee_percent_snapshot,
        fee_amount_snapshot=fee_amount_snapshot,
        net_amount_snapshot=net_amount_snapshot,
        fee_status=fee_status,
    )
    session.add(payment)
    session.flush()
    return payment


def list_for_register(session: Session, organization_id: uuid.UUID, cash_register_id: uuid.UUID) -> list[Payment]:
    """Pagamentos vinculados a um caixa — é isto que alimenta o resumo
    por forma de pagamento e o faturamento total do Caixa Diário
    (`services/cash_register.py::build_summary`). Não existe uma tabela
    separada duplicando "movimentos de pagamento" dentro do caixa —
    `payments` já é a fonte de verdade."""
    stmt = (
        select(Payment)
        .where(Payment.organization_id == organization_id, Payment.cash_register_id == cash_register_id)
        .order_by(Payment.created_at)
    )
    return list(session.scalars(stmt).all())
