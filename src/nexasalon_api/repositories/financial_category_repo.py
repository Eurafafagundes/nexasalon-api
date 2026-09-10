import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexasalon_api.models.cash_register import CashMovement
from nexasalon_api.models.finance import FinancialCategory, FixedExpenseVersion


def get(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> FinancialCategory | None:
    stmt = select(FinancialCategory).where(
        FinancialCategory.id == category_id, FinancialCategory.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[FinancialCategory]:
    stmt = (
        select(FinancialCategory)
        .where(FinancialCategory.organization_id == organization_id)
        .order_by(FinancialCategory.display_order, FinancialCategory.name)
    )
    if not include_inactive:
        stmt = stmt.where(FinancialCategory.is_active.is_(True))
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> FinancialCategory:
    category = FinancialCategory(organization_id=organization_id, **fields)
    session.add(category)
    session.flush()
    return category


def save(session: Session, category: FinancialCategory) -> FinancialCategory:
    session.flush()
    return category


def delete(session: Session, category: FinancialCategory) -> None:
    session.delete(category)
    session.flush()


def count_movements_using(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> int:
    """Conta TODOS os lançamentos (qualquer período) vinculados a esta
    categoria — usado pra decidir se `DELETE /financial-categories/{id}`
    pode prosseguir. Mesmo raciocínio de
    `service_repo.py::count_by_category`: nunca hard-delete de uma
    categoria em uso."""
    stmt = select(func.count()).select_from(CashMovement).where(
        CashMovement.organization_id == organization_id, CashMovement.financial_category_id == category_id
    )
    return session.scalar(stmt) or 0


def count_fixed_expense_versions_using(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(FixedExpenseVersion).where(
        FixedExpenseVersion.organization_id == organization_id,
        FixedExpenseVersion.financial_category_id == category_id,
    )
    return session.scalar(stmt) or 0
