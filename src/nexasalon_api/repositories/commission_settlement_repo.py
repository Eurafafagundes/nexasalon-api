import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.commission import CommissionSettlement
from nexasalon_api.models.professional import Professional


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    period_start: datetime,
    period_end: datetime,
    production_total: Decimal,
    commission_total: Decimal,
    created_by: uuid.UUID | None,
    created_by_name: str | None,
    paid_at: datetime,
) -> CommissionSettlement:
    settlement = CommissionSettlement(
        organization_id=organization_id,
        professional_id=professional_id,
        period_start=period_start,
        period_end=period_end,
        production_total=production_total,
        commission_total=commission_total,
        created_by=created_by,
        created_by_name=created_by_name,
        paid_at=paid_at,
    )
    session.add(settlement)
    session.flush()
    return settlement


def get(session: Session, organization_id: uuid.UUID, settlement_id: uuid.UUID) -> CommissionSettlement | None:
    stmt = select(CommissionSettlement).where(
        CommissionSettlement.id == settlement_id, CommissionSettlement.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[tuple[CommissionSettlement, str]]:
    """Histórico de pagamentos — já junta o nome do profissional numa
    ÚNICA query (evita N+1 na listagem, mesmo raciocínio de
    `services/commissions.py::get_overview`). Filtro de período incide
    sobre `paid_at` (quando o pagamento foi REGISTRADO), não sobre
    `period_start`/`period_end` (o período de COMPETÊNCIA que o
    pagamento cobre) — são conceitos diferentes."""
    stmt = (
        select(CommissionSettlement, Professional.name)
        .join(Professional, Professional.id == CommissionSettlement.professional_id)
        .where(CommissionSettlement.organization_id == organization_id)
    )
    if professional_id is not None:
        stmt = stmt.where(CommissionSettlement.professional_id == professional_id)
    if date_from is not None:
        stmt = stmt.where(CommissionSettlement.paid_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(CommissionSettlement.paid_at <= date_to)
    stmt = stmt.order_by(CommissionSettlement.paid_at.desc())
    return [(row[0], row[1]) for row in session.execute(stmt).all()]
