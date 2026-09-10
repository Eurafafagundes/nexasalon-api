import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.finance import FixedExpense, FixedExpenseVersion


def get(
    session: Session, organization_id: uuid.UUID, expense_id: uuid.UUID
) -> FixedExpense | None:
    stmt = select(FixedExpense).where(
        FixedExpense.id == expense_id, FixedExpense.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def get_with_versions(
    session: Session, organization_id: uuid.UUID, expense_id: uuid.UUID
) -> FixedExpense | None:
    stmt = (
        select(FixedExpense)
        .options(selectinload(FixedExpense.versions))
        .where(
            FixedExpense.id == expense_id,
            FixedExpense.organization_id == organization_id,
        )
    )
    return session.scalars(stmt).first()


def list_with_versions(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None = None
) -> list[FixedExpense]:
    stmt = (
        select(FixedExpense)
        .options(selectinload(FixedExpense.versions))
        .where(FixedExpense.organization_id == organization_id)
        .order_by(FixedExpense.created_at.desc())
    )
    return list(session.scalars(stmt).all())


def versions_for_period(
    session: Session,
    organization_id: uuid.UUID,
    start: date,
    end: date,
    branch_id: uuid.UUID | None = None,
) -> list[FixedExpenseVersion]:
    stmt = (
        select(FixedExpenseVersion)
        .join(FixedExpense)
        .where(
            FixedExpenseVersion.organization_id == organization_id,
            FixedExpenseVersion.effective_from <= end,
            (
                FixedExpenseVersion.effective_to.is_(None)
                | (FixedExpenseVersion.effective_to > start)
            ),
        )
    )
    if branch_id is not None:
        stmt = stmt.where(FixedExpenseVersion.branch_id == branch_id)
    return list(session.scalars(stmt).all())


def create(session: Session, **fields) -> FixedExpense:
    expense = FixedExpense(**fields)
    session.add(expense)
    session.flush()
    return expense


def add_version(session: Session, **fields) -> FixedExpenseVersion:
    version = FixedExpenseVersion(**fields)
    session.add(version)
    session.flush()
    return version


def save(session: Session) -> None:
    session.flush()
