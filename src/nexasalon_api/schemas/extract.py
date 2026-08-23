"""Schemas do Extrato — ver `services/extract.py` para o raciocínio de
domínio (unidade = Comanda, não item/pagamento)."""
import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from nexasalon_api.models.enums import CashMovementType, OrderStatus, PaymentMethod
from nexasalon_api.models.order import Order


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
    # disponível"). `sum(i.price for i in items) == total` sempre — não
    # é uma segunda fonte de valor, é o MESMO `OrderItem.price` que já
    # compõe `total` acima.
    items: list[ExtractSaleItemRow]
    total: Decimal
    status: OrderStatus

    @classmethod
    def from_order(cls, order: Order, client_name: str) -> "ExtractSaleRow":
        total = sum((item.price for item in order.items), Decimal("0"))
        unique_methods = list(dict.fromkeys(p.method for p in order.payments))
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
