import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.commission import CommissionAdjustment


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    order_item_id: uuid.UUID | None,
    amount: Decimal,
    reason: str,
    created_by: uuid.UUID | None,
    created_by_name: str | None,
) -> CommissionAdjustment:
    adjustment = CommissionAdjustment(
        organization_id=organization_id,
        professional_id=professional_id,
        order_item_id=order_item_id,
        amount=amount,
        reason=reason,
        created_by=created_by,
        created_by_name=created_by_name,
    )
    session.add(adjustment)
    session.flush()
    return adjustment


def list_pending_for_professional(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID
) -> list[CommissionAdjustment]:
    """Ajustes ainda NÃO incluídos em nenhum settlement — candidatos a
    entrar na próxima liquidação (ver
    `services/commissions.py::create_settlement`, `adjustment_ids`)."""
    stmt = (
        select(CommissionAdjustment)
        .where(
            CommissionAdjustment.organization_id == organization_id,
            CommissionAdjustment.professional_id == professional_id,
            CommissionAdjustment.commission_settlement_id.is_(None),
        )
        .order_by(CommissionAdjustment.created_at)
    )
    return list(session.scalars(stmt).all())


def lock_by_ids(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID, adjustment_ids: list[uuid.UUID]
) -> list[CommissionAdjustment]:
    """Trava (`FOR UPDATE`) os ajustes explicitamente escolhidos pra
    entrar numa liquidação — mesmo raciocínio de concorrência de
    `order_item_repo.lock_pending_commission_items`: duas requisições
    concorrentes tentando incluir o MESMO ajuste em dois settlements
    diferentes serializam aqui; a segunda, ao ser liberada, vê
    `commission_settlement_id` já preenchido pela primeira e
    `services/commissions.py::create_settlement` recusa (nunca paga o
    mesmo ajuste duas vezes)."""
    stmt = (
        select(CommissionAdjustment)
        .where(
            CommissionAdjustment.organization_id == organization_id,
            CommissionAdjustment.professional_id == professional_id,
            CommissionAdjustment.id.in_(adjustment_ids),
        )
        .with_for_update()
    )
    return list(session.scalars(stmt).all())


def list_for_settlement(session: Session, settlement_id: uuid.UUID) -> list[CommissionAdjustment]:
    stmt = (
        select(CommissionAdjustment)
        .where(CommissionAdjustment.commission_settlement_id == settlement_id)
        .order_by(CommissionAdjustment.created_at)
    )
    return list(session.scalars(stmt).all())
