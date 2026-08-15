"""Schemas da Comanda/Pagamento — ver `models/order.py` para o
raciocínio de domínio (3 camadas de preço, `Payment` como lista)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexasalon_api.models.enums import CardBrand, OrderStatus, PaymentMethod
from nexasalon_api.models.order import Order

_CARD_METHODS = frozenset({PaymentMethod.DEBIT, PaymentMethod.CREDIT})


class OrderCreate(BaseModel):
    appointment_id: uuid.UUID


class OrderItemPriceUpdate(BaseModel):
    """Único campo editável de uma linha da comanda — nunca reescreve
    `AppointmentItem.price` nem `Service.default_price` (ver
    `services/orders.py`)."""

    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class PaymentCreate(BaseModel):
    method: PaymentMethod
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    # Obrigatório só pra débito/crédito (bandeira) — validado abaixo,
    # mesmo padrão de consistência condicional do `ScheduleBlockCreate`.
    card_brand: CardBrand | None = None
    # Preparado pro futuro (parcelas de crédito) — só aceito quando
    # method=credit, nunca usado pra calcular nada nesta versão.
    installments: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_card_brand(self) -> "PaymentCreate":
        needs_brand = self.method in _CARD_METHODS
        if needs_brand and self.card_brand is None:
            raise ValueError("card_brand é obrigatório para pagamento em débito ou crédito.")
        if not needs_brand and self.card_brand is not None:
            raise ValueError("card_brand só é aceito para pagamento em débito ou crédito.")
        return self

    @model_validator(mode="after")
    def _check_installments(self) -> "PaymentCreate":
        if self.installments is not None and self.method != PaymentMethod.CREDIT:
            raise ValueError("installments só é aceito para pagamento em crédito.")
        return self


class OrderClose(BaseModel):
    """`payments` é uma LISTA — mesmo com a UI desta primeira versão só
    criando um lançamento por fechamento, o domínio já suporta
    pagamento misto (ex.: parte Pix + parte Crédito) sem precisar de
    outra migration depois."""

    payments: list[PaymentCreate] = Field(min_length=1)


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    appointment_item_id: uuid.UUID | None
    service_id: uuid.UUID
    professional_id: uuid.UUID
    duration_minutes: int
    price: Decimal
    created_at: datetime
    updated_at: datetime


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    method: PaymentMethod
    card_brand: CardBrand | None
    installments: int | None
    amount: Decimal
    created_by: uuid.UUID | None
    created_at: datetime


class OrderRead(BaseModel):
    """`subtotal`/`total` são CALCULADOS (soma de `items[].price`), nunca
    colunas persistidas — evita a comanda ficar com um total
    dessincronizado depois de uma edição de preço. Hoje `total ==
    subtotal` (sem desconto: nenhuma estrutura de desconto existia no
    domínio antes desta rodada, e criar uma do zero ficou fora do
    escopo pedido — "sem inventar Financeiro completo"). Os dois campos
    já existem separados pra não quebrar o schema quando desconto for
    modelado depois."""

    model_config = ConfigDict(from_attributes=False)

    id: uuid.UUID
    organization_id: uuid.UUID
    appointment_id: uuid.UUID
    branch_id: uuid.UUID
    client_id: uuid.UUID
    status: OrderStatus
    subtotal: Decimal
    total: Decimal
    items: list[OrderItemRead]
    payments: list[PaymentRead]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    created_by: uuid.UUID | None
    closed_by: uuid.UUID | None

    @classmethod
    def from_order(cls, order: Order) -> "OrderRead":
        subtotal = sum((item.price for item in order.items), Decimal("0"))
        return cls(
            id=order.id,
            organization_id=order.organization_id,
            appointment_id=order.appointment_id,
            branch_id=order.branch_id,
            client_id=order.client_id,
            status=order.status,
            subtotal=subtotal,
            total=subtotal,
            items=[OrderItemRead.model_validate(item) for item in order.items],
            payments=[PaymentRead.model_validate(payment) for payment in order.payments],
            created_at=order.created_at,
            updated_at=order.updated_at,
            closed_at=order.closed_at,
            created_by=order.created_by,
            closed_by=order.closed_by,
        )
