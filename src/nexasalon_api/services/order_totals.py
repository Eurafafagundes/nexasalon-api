"""Fonte CANÔNICA do total de uma comanda — serviços (`OrderItem.price`)
+ produtos (`OrderProductItem.quantity * .unit_price`), SEMPRE as duas
parcelas juntas. Antes da Etapa N4.1 essa fórmula estava repetida
manualmente (idêntica, mas duplicada) em `close_order`,
`close_orders_consolidated`, `OrderRead.from_order` e
`clients.py::_order_total` — e foi exatamente essa duplicação que
deixou `services/dashboard.py`/`services/extract.py` divergirem
(somavam só `OrderItem.price`, excluindo produto do Faturamento e do
Extrato). Este módulo centraliza a fórmula pra nunca mais divergir;
todo consumidor que precisar do "valor vendido de uma comanda" deve
chamar `order_total()`, nunca reimplementar a soma.

Não inclui desconto/acréscimo — esse conceito não existe hoje no
domínio (`OrderRead.total == OrderRead.subtotal` sempre, ver docstring
de `schemas/order.py::OrderRead`). Quando desconto for modelado, este
é o único lugar que precisa mudar.

Não é a mesma coisa que "Recebido" (`Payment.amount`) — pagamento e
venda continuam sendo conceitos diferentes (ver docstring "TRÊS
CONCEITOS" em `services/dashboard.py`); esta função nunca lê
`Payment`."""
from dataclasses import dataclass
from decimal import Decimal

from nexasalon_api.models.order import Order, OrderItem


@dataclass(frozen=True)
class OrderTotalBreakdown:
    services_total: Decimal
    products_total: Decimal
    total: Decimal


def order_total_breakdown(order: Order) -> OrderTotalBreakdown:
    services_total = sum((item.price for item in order.items), Decimal("0"))
    products_total = sum((item.quantity * item.unit_price for item in order.product_items), Decimal("0"))
    return OrderTotalBreakdown(
        services_total=services_total, products_total=products_total, total=services_total + products_total
    )


def order_total(order: Order) -> Decimal:
    """Valor ECONÔMICO/vendido — sempre `OrderItem.price` intocado,
    nunca reduzido por benefício (ver `item_charged_amount` abaixo pra
    isso). É a base de Faturamento/Extrato/Dashboard e continua sendo
    o ÚNICO significado desta função — "quanto foi vendido", não
    "quanto falta pagar"."""
    return order_total_breakdown(order).total


# --- Etapa "Benefício por Item" ---------------------------------------


def item_charged_amount(item: OrderItem) -> Decimal:
    """Valor efetivamente A COBRAR deste item — `price` menos o
    benefício aplicado (`benefit_amount`, `None` = sem benefício).
    DERIVADO, nunca persistido: `item.price` continua sendo só o valor
    econômico (nunca alterado pra representar benefício, ver docstring
    de `OrderItem` em `models/order.py`)."""
    return item.price - (item.benefit_amount or Decimal("0"))


def order_charged_total(order: Order) -> Decimal:
    """Quanto esta comanda ainda precisa RECEBER pra fechar — soma de
    `item_charged_amount` de cada serviço + produtos (produtos não têm
    benefício nesta rodada, usa `unit_price` cheio). É o valor usado
    em TODA validação de saldo/overpayment do fechamento
    (`services/orders.py::close_order`/`close_orders_consolidated`) —
    NUNCA `order_total()` (que é o valor econômico/vendido, usado só
    por Faturamento/Extrato/Dashboard, nunca por "quanto falta pagar").
    Uma comanda com benefício cobrindo tudo tem `order_charged_total
    == 0` mesmo com `order_total() > 0` — fecha com `payments=[]`."""
    services_charged = sum((item_charged_amount(item) for item in order.items), Decimal("0"))
    products_total = order_total_breakdown(order).products_total
    return services_charged + products_total
