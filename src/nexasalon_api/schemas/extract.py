"""Schemas do Extrato — ver `services/extract.py` para o raciocínio de
domínio (unidade = Comanda, não item/pagamento)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from nexasalon_api.models.enums import CashMovementType, OrderStatus, PaymentMethod
from nexasalon_api.models.order import Order


class ExtractSaleRow(BaseModel):
    """Uma linha = uma Comanda, nunca um serviço/pagamento isolado —
    "Manutenção + Mechas" numa linha só de R$ 800, não duas linhas de
    R$ 800 (item 18)."""

    order_id: uuid.UUID
    order_number: int
    date: datetime
    client_id: uuid.UUID
    client_name: str
    services_summary: str  # "Manutenção + Mechas"
    professionals_summary: str  # "Ianka + Ingrid"
    payment_methods_summary: str  # "Pix" ou "Pix + Crédito"
    total: Decimal
    status: OrderStatus

    @classmethod
    def from_order(cls, order: Order, client_name: str) -> "ExtractSaleRow":
        total = sum((item.price for item in order.items), Decimal("0"))
        return cls(
            order_id=order.id,
            order_number=order.order_number,
            date=order.closed_at or order.created_at,
            client_id=order.client_id,
            client_name=client_name,
            services_summary=" + ".join(dict.fromkeys(i.service_name for i in order.items)) or "—",
            professionals_summary=" + ".join(dict.fromkeys(i.professional_name for i in order.items)) or "—",
            payment_methods_summary=" + ".join(dict.fromkeys(p.method.value for p in order.payments)) or "—",
            total=total,
            status=order.status,
        )


class ExtractMovementRow(BaseModel):
    id: uuid.UUID
    type: CashMovementType
    amount: Decimal
    category: str | None
    description: str
    method: PaymentMethod
    created_by_name: str
    created_at: datetime


class ExtractResponse(BaseModel):
    date_from: datetime | None
    date_to: datetime | None
    revenue_total: Decimal
    expense_total: Decimal
    result: Decimal
    sales: list[ExtractSaleRow]
    movements: list[ExtractMovementRow]
