import calendar
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.models.enums import (
    AuditAction,
    ExpenseNature,
    FixedExpenseRecurrence,
)
from nexasalon_api.models.finance import FixedExpense, FixedExpenseVersion
from nexasalon_api.repositories import (
    audit_log_repo,
    branch_repo,
    financial_category_repo,
    fixed_expense_repo,
    user_repo,
)
from nexasalon_api.schemas.fixed_expense import (
    FixedExpenseCreate,
    FixedExpenseProvisionRow,
    FixedExpenseRead,
    FixedExpenseStatusUpdate,
    FixedExpenseSummary,
    FixedExpenseUpdate,
)

INTERVALS = {
    FixedExpenseRecurrence.MONTHLY: 1,
    FixedExpenseRecurrence.QUARTERLY: 3,
    FixedExpenseRecurrence.SEMIANNUAL: 6,
    FixedExpenseRecurrence.ANNUAL: 12,
}


def current_month() -> date:
    return datetime.now(timezone.utc).date().replace(day=1)


def add_months(month: date, count: int) -> date:
    absolute = month.year * 12 + month.month - 1 + count
    return date(absolute // 12, absolute % 12 + 1, 1)


def months_between(start: date, end: date):
    month = start.replace(day=1)
    last = end.replace(day=1)
    while month <= last:
        yield month
        month = add_months(month, 1)


def due_date_for(month: date, due_day: int) -> date:
    return date(
        month.year,
        month.month,
        min(due_day, calendar.monthrange(month.year, month.month)[1]),
    )


def occurs_in(version: FixedExpenseVersion, month: date) -> bool:
    month = month.replace(day=1)
    if (
        not version.is_active
        or month < version.start_month
        or (version.end_month and month > version.end_month)
    ):
        return False
    offset = (
        (month.year - version.start_month.year) * 12
        + month.month
        - version.start_month.month
    )
    return offset % INTERVALS[version.recurrence] == 0


def effective_version(
    expense: FixedExpense, competence: date
) -> FixedExpenseVersion | None:
    month = competence.replace(day=1)
    return next(
        (
            v
            for v in reversed(expense.versions)
            if v.effective_from <= month
            and (v.effective_to is None or month < v.effective_to)
        ),
        None,
    )


def _read(expense: FixedExpense, version: FixedExpenseVersion) -> FixedExpenseRead:
    return FixedExpenseRead(
        id=expense.id,
        organization_id=expense.organization_id,
        branch_id=version.branch_id,
        name=version.name,
        category=version.category_name_snapshot,
        financial_category_id=version.financial_category_id,
        amount=version.amount,
        recurrence=version.recurrence,
        due_day=version.due_day,
        start_month=version.start_month,
        end_month=version.end_month,
        is_active=version.is_active,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        created_by=expense.created_by,
        created_by_name=expense.created_by_name,
        created_at=expense.created_at,
        updated_at=expense.updated_at,
    )


def _validate_branch(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID
) -> None:
    if not branch_repo.exists(session, organization_id, branch_id):
        raise NotFoundError("Unidade não encontrada.")


def _validate_dates(start: date, end: date | None, effective: date) -> None:
    if end and end < start:
        raise ValidationDomainError(
            "A competência final não pode ser anterior à inicial."
        )
    if effective < current_month():
        raise ConflictError(
            "Competências passadas são imutáveis; aplique a alteração no mês atual ou futuro."
        )


def _fixed_category(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID):
    category = financial_category_repo.get(session, organization_id, category_id)
    if category is None:
        raise NotFoundError("Categoria financeira não encontrada.")
    if category.nature != ExpenseNature.FIXED:
        raise ValidationDomainError("Despesas fixas exigem uma categoria de natureza fixa.")
    if not category.is_active:
        raise ValidationDomainError("A categoria financeira selecionada está inativa.")
    return category


def _audit_values(version: FixedExpenseVersion) -> dict[str, str | bool | None]:
    return {
        "name": version.name, "financial_category_id": str(version.financial_category_id),
        "category": version.category_name_snapshot, "amount": str(version.amount),
        "recurrence": version.recurrence.value, "due_day": str(version.due_day),
        "start_month": version.start_month.isoformat(),
        "end_month": version.end_month.isoformat() if version.end_month else None,
        "branch_id": str(version.branch_id), "is_active": version.is_active,
        "effective_from": version.effective_from.isoformat(),
    }


def list_expenses(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None,
    competence: date,
    include_inactive: bool,
) -> list[FixedExpenseRead]:
    if branch_id:
        _validate_branch(session, actor.organization_id, branch_id)
    rows = []
    for expense in fixed_expense_repo.list_with_versions(
        session, actor.organization_id
    ):
        version = effective_version(expense, competence)
        if (
            version
            and (branch_id is None or version.branch_id == branch_id)
            and (include_inactive or version.is_active)
        ):
            rows.append(_read(expense, version))
    return sorted(rows, key=lambda row: row.name.casefold())


def create_expense(
    session: Session, actor: ActorContext, data: FixedExpenseCreate
) -> FixedExpenseRead:
    _validate_branch(session, actor.organization_id, data.branch_id)
    _validate_dates(data.start_month, data.end_month, data.start_month)
    category = _fixed_category(session, actor.organization_id, data.financial_category_id)
    user = user_repo.get(session, actor.user_id)
    expense = fixed_expense_repo.create(
        session,
        organization_id=actor.organization_id,
        created_by=actor.user_id,
        created_by_name=user.name if user else "Usuário removido",
    )
    version = fixed_expense_repo.add_version(
        session,
        organization_id=actor.organization_id,
        fixed_expense_id=expense.id,
        effective_from=data.start_month,
        effective_to=None,
        **data.model_dump(exclude={"branch_id", "financial_category_id"}),
        branch_id=data.branch_id,
        financial_category_id=category.id,
        category_name_snapshot=category.name,
    )
    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id,
        entity_type="fixed_expense", entity_id=expense.id, action=AuditAction.CREATE,
        new_values=_audit_values(version),
    )
    return _read(expense, version)


def _get(session: Session, actor: ActorContext, expense_id: uuid.UUID) -> FixedExpense:
    expense = fixed_expense_repo.get_with_versions(
        session, actor.organization_id, expense_id
    )
    if expense is None:
        raise NotFoundError("Despesa fixa não encontrada.")
    return expense


def update_expense(
    session: Session,
    actor: ActorContext,
    expense_id: uuid.UUID,
    data: FixedExpenseUpdate,
) -> FixedExpenseRead:
    _validate_branch(session, actor.organization_id, data.branch_id)
    _validate_dates(data.start_month, data.end_month, data.effective_from)
    category = _fixed_category(session, actor.organization_id, data.financial_category_id)
    expense = _get(session, actor, expense_id)
    current = effective_version(expense, data.effective_from)
    if current is None:
        raise ConflictError("Não existe versão vigente nessa competência.")
    existing_same = next(
        (v for v in expense.versions if v.effective_from == data.effective_from), None
    )
    fields = data.model_dump(exclude={"effective_from", "financial_category_id"})
    fields.update(financial_category_id=category.id, category_name_snapshot=category.name)
    old_values = _audit_values(current)
    if existing_same:
        for key, value in fields.items():
            setattr(existing_same, key, value)
        fixed_expense_repo.save(session)
        audit_log_repo.create(
            session, organization_id=actor.organization_id, user_id=actor.user_id,
            entity_type="fixed_expense", entity_id=expense.id, action=AuditAction.UPDATE,
            old_values=old_values, new_values=_audit_values(existing_same),
        )
        return _read(expense, existing_same)
    next_effective = current.effective_to
    current.effective_to = data.effective_from
    version = fixed_expense_repo.add_version(
        session,
        organization_id=actor.organization_id,
        fixed_expense_id=expense.id,
        effective_from=data.effective_from,
        effective_to=next_effective,
        **fields,
    )
    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id,
        entity_type="fixed_expense", entity_id=expense.id, action=AuditAction.UPDATE,
        old_values=old_values, new_values=_audit_values(version),
    )
    return _read(expense, version)


def set_status(
    session: Session,
    actor: ActorContext,
    expense_id: uuid.UUID,
    data: FixedExpenseStatusUpdate,
) -> FixedExpenseRead:
    expense = _get(session, actor, expense_id)
    current = effective_version(expense, data.effective_from)
    if current is None:
        raise ConflictError("Não existe versão vigente nessa competência.")
    payload = FixedExpenseUpdate(
        name=current.name,
        financial_category_id=current.financial_category_id,
        amount=current.amount,
        recurrence=current.recurrence,
        due_day=current.due_day,
        start_month=current.start_month,
        end_month=current.end_month,
        branch_id=current.branch_id,
        is_active=data.is_active,
        effective_from=data.effective_from,
    )
    return update_expense(session, actor, expense_id, payload)


def provisions(
    session: Session,
    organization_id: uuid.UUID,
    *,
    branch_id: uuid.UUID | None,
    date_from: datetime,
    date_to: datetime,
) -> list[FixedExpenseProvisionRow]:
    if date_to <= date_from:
        return []
    start_date, end_date = date_from.date(), date_to.date()
    versions = fixed_expense_repo.versions_for_period(
        session,
        organization_id,
        start_date.replace(day=1),
        end_date.replace(day=1),
        branch_id,
    )
    result = []
    for version in versions:
        for month in months_between(start_date, end_date):
            if not (
                version.effective_from <= month
                and (version.effective_to is None or month < version.effective_to)
            ) or not occurs_in(version, month):
                continue
            due = due_date_for(month, version.due_day)
            due_dt = datetime.combine(due, time.min, tzinfo=date_from.tzinfo)
            if date_from <= due_dt < date_to:
                result.append(
                    FixedExpenseProvisionRow(
                        fixed_expense_id=version.fixed_expense_id,
                        name=version.name,
                        category=version.category_name_snapshot,
                        financial_category_id=version.financial_category_id,
                        branch_id=version.branch_id,
                        competence_month=month,
                        due_date=due,
                        amount=version.amount,
                    )
                )
    return sorted(result, key=lambda row: (row.due_date, row.name.casefold()))


def summary(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None,
    competence: date,
) -> FixedExpenseSummary:
    rows = list_expenses(
        session,
        actor,
        branch_id=branch_id,
        competence=competence,
        include_inactive=False,
    )
    average = sum(
        (
            row.amount / Decimal(INTERVALS[row.recurrence])
            for row in rows
            if row.start_month <= competence
            and (row.end_month is None or competence <= row.end_month)
        ),
        Decimal(0),
    )
    return FixedExpenseSummary(
        competence_month=competence.replace(day=1),
        monthly_average=average.quantize(Decimal("0.01")),
        active_count=len(rows),
    )
