"""Schemas do Extrato — ver `services/extract.py` para o raciocínio de
domínio (unidade = Comanda, não item/pagamento)."""
import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from nexasalon_api.models.enums import (
    CardBrand,
    CashMovementType,
    OrderStatus,
    PaymentFeeStatus,
    PaymentMethod,
)
from nexasalon_api.models.order import Order
from nexasalon_api.services import order_totals
from nexasalon_api.services import payment_fees as payment_fees_service


class ExtractRowType(str, Enum):
    """Filtro de "o que aparece" no Extrato — item explícito "o que estou
    visualizando é o que será exportado": o MESMO valor é aceito por
    `GET /extract` (tela) e `GET /extract/export` (Excel), nunca duas
    lógicas de filtro divergentes. Não é um enum persistido no banco —
    só recorta quais linhas de `ExtractSummary.sales`/`.movements`
    voltam na resposta; os totais (`revenue_total`/`expense_total`/
    `result`) continuam sempre referentes ao período INTEIRO, igual já
    era o comportamento dos cards de resumo antes deste filtro existir."""

    ALL = "all"
    SALES = "sales"
    SUPPLY = "supply"
    WITHDRAWAL = "withdrawal"


class ExtractSaleItemRow(BaseModel):
    """Etapa N2 — granularidade ANALÍTICA (por serviço/profissional),
    nunca financeira: cada linha aqui é um `OrderItem` já existente,
    exposto tal como está (dado relacional, não string concatenada) —
    "dados relacionais primeiro; strings apenas para apresentação".
    NUNCA some junto com outra comanda pra formar um "faturamento" — a
    unidade financeira continua sendo `ExtractSaleRow.total`."""

    order_item_id: uuid.UUID
    service_id: uuid.UUID
    service_name: str
    professional_id: uuid.UUID
    professional_name: str
    price: Decimal


class ExtractSaleProductItemRow(BaseModel):
    """Ajuste pós-review da Etapa N2 — produto vendido na comanda, numa
    lista SEPARADA de `ExtractSaleRow.items` (serviços): nunca no mesmo
    array, pra uma análise de BI nunca precisar checar um campo
    "item_type" pra saber se está somando serviço ou produto — o tipo já
    é a própria coleção. Mesmo raciocínio de `ExtractSaleItemRow`: dado
    relacional (`product_id`), nunca string concatenada."""

    order_product_item_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    price: Decimal  # quantity * unit_price — já calculado, não recalcular no frontend.


class ExtractPaymentBreakdownRow(BaseModel):
    """Etapa N3 — bruto/taxa/líquido POR `Payment` individual (item
    explícito "pagamento dividido": a taxa nunca é calculada sobre o
    total da comanda, só sobre o valor de cada lançamento). `fee_status`
    é sempre um dos 3 estados estruturados (nunca `None` aqui — já
    resolvido por `services/payment_fees.py::derive_fee_status`, que
    trata pagamento histórico sem dado de taxa corretamente). Quando
    `fee_status=UNCONFIGURED`, `fee_amount`/`net_amount` ficam `None`
    DE PROPÓSITO — nunca um líquido inventado."""

    payment_id: uuid.UUID
    method: PaymentMethod
    card_brand: CardBrand | None
    gross_amount: Decimal  # = payment.amount
    fee_status: PaymentFeeStatus
    fee_amount: Decimal | None
    net_amount: Decimal | None


class ExtractSaleRow(BaseModel):
    """Uma linha = uma Comanda, nunca um serviço/pagamento isolado —
    "Manutenção + Mechas" numa linha só de R$ 800, não duas linhas de
    R$ 800 (item 18). `total`/`payment_methods`/`status` são sempre da
    COMANDA inteira — a granularidade por serviço vive só em `items`
    (Etapa N2), que o frontend pode expandir/detalhar sem nunca virar
    uma segunda "venda"."""

    order_id: uuid.UUID
    order_number: int
    date: datetime
    client_id: uuid.UUID
    client_name: str
    services_summary: str  # "Manutenção + Mechas"
    professionals_summary: str  # "Ianka + Ingrid"
    payment_methods_summary: str  # "pix + credit" — LEGADO, ver `payment_methods` abaixo.
    # Lista estruturada dos métodos únicos desta comanda (mesma ordem/
    # dedup de `payment_methods_summary`, sem o "+"-join) — o frontend
    # traduz cada um pra PT-BR (`PAYMENT_METHOD_LABELS`) sem precisar
    # fazer split de string nem duplicar a lógica de tradução.
    payment_methods: list[PaymentMethod]
    # Etapa N2 — granularidade por serviço/profissional (item explícito
    # "evite strings concatenadas quando dado estruturado estiver
    # disponível"). `sum(i.price for i in items)` é a parcela de
    # SERVIÇO de `total` (ver `product_items` abaixo pra parcela de
    # produto — juntas as duas compõem `total`, nunca uma sozinha).
    items: list[ExtractSaleItemRow]
    # Ajuste pós-review N2 — produtos da comanda, SEPARADOS de `items`
    # (nunca misturados numa mesma lista/análise pra BI). Etapa N4.1:
    # `total` abaixo agora SOMA esta lista também (`quantity *
    # unit_price` de cada linha) — produto vendido é venda real (baixa
    # de estoque de verdade no fechamento, ver `services/orders.py::
    # close_order`), nunca deveria ter ficado fora do valor vendido da
    # comanda.
    product_items: list[ExtractSaleProductItemRow]
    total: Decimal
    status: OrderStatus
    # Etapa N3 — Bruto/Taxa/Líquido, calculado a partir dos PAGAMENTOS
    # (`Payment.amount`), NUNCA a partir de `total` (que é a soma dos
    # SERVIÇOS/`OrderItem` — um conceito diferente, ver docstring do
    # módulo). `payments_gross_total` é a soma de `Payment.amount` desta
    # comanda — pode diferir de `total` em casos de troco/sobra do
    # fechamento consolidado; nunca tratados como a mesma coisa aqui.
    payments_breakdown: list[ExtractPaymentBreakdownRow]
    payments_gross_total: Decimal
    payments_known_fee_total: Decimal  # soma só das taxas CONHECIDAS (CALCULATED) — nunca inclui UNCONFIGURED.
    # `None` quando QUALQUER pagamento desta comanda está com taxa não
    # configurada — nunca um "líquido" definitivo fingido (item
    # explícito do pedido: "não apresente um líquido falso").
    payments_net_total: Decimal | None
    has_unconfigured_fee: bool

    @classmethod
    def from_order(cls, order: Order, client_name: str) -> "ExtractSaleRow":
        # Etapa N4.1 — fórmula CANÔNICA compartilhada (`order_totals.py`),
        # a MESMA usada por `close_order`/`OrderRead`/Dashboard: serviço
        # + produto. Corrige um bug em que este `total` (e
        # `revenue_total` do módulo de serviço) somava só `OrderItem`,
        # excluindo produto vendido do valor da linha.
        total = order_totals.order_total(order)
        unique_methods = list(dict.fromkeys(p.method for p in order.payments))

        breakdown = []
        for payment in order.payments:
            fee = payment_fees_service.breakdown_for_display(payment)
            breakdown.append(
                ExtractPaymentBreakdownRow(
                    payment_id=payment.id,
                    method=payment.method,
                    card_brand=payment.card_brand,
                    gross_amount=payment.amount,
                    fee_status=fee.fee_status,
                    fee_amount=fee.fee_amount,
                    net_amount=fee.net_amount,
                )
            )
        has_unconfigured_fee = any(row.fee_status == PaymentFeeStatus.UNCONFIGURED for row in breakdown)
        payments_gross_total = sum((row.gross_amount for row in breakdown), Decimal("0"))
        payments_known_fee_total = sum(
            (row.fee_amount for row in breakdown if row.fee_amount is not None), Decimal("0")
        )
        payments_net_total = None if has_unconfigured_fee else (payments_gross_total - payments_known_fee_total)

        return cls(
            order_id=order.id,
            order_number=order.order_number,
            date=order.closed_at or order.created_at,
            client_id=order.client_id,
            client_name=client_name,
            services_summary=" + ".join(dict.fromkeys(i.service_name for i in order.items)) or "—",
            professionals_summary=" + ".join(dict.fromkeys(i.professional_name for i in order.items)) or "—",
            payment_methods_summary=" + ".join(m.value for m in unique_methods) or "—",
            payment_methods=unique_methods,
            items=[
                ExtractSaleItemRow(
                    order_item_id=i.id,
                    service_id=i.service_id,
                    service_name=i.service_name,
                    professional_id=i.professional_id,
                    professional_name=i.professional_name,
                    price=i.price,
                )
                for i in order.items
            ],
            product_items=[
                ExtractSaleProductItemRow(
                    order_product_item_id=p.id,
                    product_id=p.product_id,
                    product_name=p.product_name,
                    quantity=p.quantity,
                    unit_price=p.unit_price,
                    price=p.unit_price * p.quantity,
                )
                for p in order.product_items
            ],
            total=total,
            status=order.status,
            payments_breakdown=breakdown,
            payments_gross_total=payments_gross_total,
            payments_known_fee_total=payments_known_fee_total,
            payments_net_total=payments_net_total,
            has_unconfigured_fee=has_unconfigured_fee,
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
