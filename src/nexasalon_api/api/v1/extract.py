from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import OrderStatus
from nexasalon_api.schemas.extract import ExtractMovementRow, ExtractResponse, ExtractSaleRow
from nexasalon_api.services import extract as extract_service

router = APIRouter(prefix="/extract", tags=["extract"])

_view = require_permission("finance.view")


@router.get("", response_model=ExtractResponse, summary="Extrato — comandas + movimentações num período")
def get_extract(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    status_filter: OrderStatus | None = Query(None, alias="status"),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ExtractResponse:
    summary = extract_service.get_extract(session, actor, date_from=date_from, date_to=date_to, status=status_filter)
    return ExtractResponse(
        date_from=date_from,
        date_to=date_to,
        revenue_total=summary.revenue_total,
        expense_total=summary.expense_total,
        result=summary.result,
        sales=[
            ExtractSaleRow.from_order(o, summary.client_names.get(o.client_id, "Cliente removido"))
            for o in summary.sales
        ],
        movements=[
            ExtractMovementRow(
                id=m.id, type=m.type, amount=m.amount, category=m.category, description=m.description,
                method=m.method, created_by_name=m.created_by_name, created_at=m.created_at,
            )
            for m in summary.movements
        ],
    )
