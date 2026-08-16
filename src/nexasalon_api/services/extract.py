"""Extrato — Financeiro > Extrato (item 17/18/19 da rodada "evolução
funcional"). Unidade principal é a COMANDA (`Order` fechada), nunca o
item/pagamento: uma comanda com 2 serviços ou 2 pagamentos continua
sendo UMA linha (item "pagamento misto não duplica faturamento").
Movimentações (`CashMovement`) são uma granularidade SEPARADA — nunca
somadas junto com o faturamento de vendas na mesma conta (item
"Movimentações").
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.cash_register import CashMovement
from nexasalon_api.models.enums import CashMovementType, OrderStatus
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import cash_movement_repo, client_repo, order_repo


@dataclass
class ExtractSummary:
    revenue_total: Decimal = Decimal("0")  # Receitas — soma de comandas FECHADAS no período.
    expense_total: Decimal = Decimal("0")  # Despesas — soma de CashMovement WITHDRAWAL no período.
    result: Decimal = Decimal("0")
    sales: list[Order] = field(default_factory=list)
    movements: list[CashMovement] = field(default_factory=list)
    client_names: dict[uuid.UUID, str] = field(default_factory=dict)


def get_extract(
    session: Session,
    actor: ActorContext,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    status: OrderStatus | None = None,
) -> ExtractSummary:
    sales = order_repo.list_for_org(
        session, actor.organization_id, status=status, date_from=date_from, date_to=date_to
    )
    movements = cash_movement_repo.list_for_org(
        session, actor.organization_id, date_from=date_from, date_to=date_to
    )

    revenue_total = sum(
        (sum((item.price for item in o.items), Decimal("0")) for o in sales if o.status == OrderStatus.CLOSED),
        Decimal("0"),
    )
    expense_total = sum(
        (m.amount for m in movements if m.type == CashMovementType.WITHDRAWAL), Decimal("0")
    )

    client_ids = {o.client_id for o in sales}
    client_names = {
        c.id: c.name for c in (client_repo.get(session, actor.organization_id, cid) for cid in client_ids) if c
    }

    return ExtractSummary(
        revenue_total=revenue_total,
        expense_total=expense_total,
        result=revenue_total - expense_total,
        sales=sales,
        movements=movements,
        client_names=client_names,
    )
