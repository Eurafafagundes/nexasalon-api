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

from nexasalon_api.models.order import Order


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
    return order_total_breakdown(order).total
