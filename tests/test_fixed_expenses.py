import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.enums import ExpenseNature, FixedExpenseRecurrence
from nexasalon_api.models.finance import FinancialCategory
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.schemas.fixed_expense import (
    FixedExpenseCreate,
    FixedExpenseStatusUpdate,
    FixedExpenseUpdate,
)
from nexasalon_api.repositories import audit_log_repo
from nexasalon_api.services import fixed_expenses


@pytest.fixture()
def context():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org fixas", slug=f"fixas-{org_id.hex[:8]}"))
        user = User(email=f"fixas-{org_id.hex[:8]}@test.local", name="Owner")
        session.add(user); session.flush()
        branches = [Branch(organization_id=org_id, name=name, slug=f"{name.lower()}-{org_id.hex[:5]}") for name in ("Matriz", "Filial")]
        session.add_all(branches); session.flush()
        category = FinancialCategory(organization_id=org_id, name="Estrutura", nature=ExpenseNature.FIXED)
        session.add(category); session.flush()
        actor = ActorContext(organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="OWNER", permissions=frozenset({"finance.view", "finance.manage"}))
        yield session, actor, branches, category
        session.rollback()


def payload(branch_id, category_id, **changes):
    base = {"name": "Aluguel", "financial_category_id": category_id, "amount": "2420.00", "recurrence": FixedExpenseRecurrence.MONTHLY, "due_day": 31, "start_month": fixed_expenses.current_month(), "end_month": None, "branch_id": branch_id, "is_active": True}
    base.update(changes)
    return FixedExpenseCreate(**base)


def test_create_edit_deactivate_and_history(context):
    session, actor, branches, category = context
    created = fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id))
    next_month = fixed_expenses.add_months(fixed_expenses.current_month(), 1)
    changed = fixed_expenses.update_expense(session, actor, created.id, FixedExpenseUpdate(**payload(branches[0].id, category.id, amount="3000").model_dump(), effective_from=next_month))
    assert changed.amount == Decimal(3000)
    historical = fixed_expenses.list_expenses(session, actor, branch_id=None, competence=fixed_expenses.current_month(), include_inactive=True)
    assert historical[0].amount == Decimal(2420)
    fixed_expenses.set_status(session, actor, created.id, FixedExpenseStatusUpdate(is_active=False, effective_from=next_month))
    assert fixed_expenses.list_expenses(session, actor, branch_id=None, competence=next_month, include_inactive=False) == []
    assert fixed_expenses.list_expenses(session, actor, branch_id=None, competence=fixed_expenses.current_month(), include_inactive=False)[0].is_active
    logs = audit_log_repo.list_for_entity(session, actor.organization_id, "fixed_expense", created.id)
    assert [log.action.value for log in logs] == ["create", "update", "update"]
    assert logs[-1].new_values["is_active"] is False


def test_branch_and_organization_isolation(context):
    session, actor, branches, category = context
    created = fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id))
    assert len(fixed_expenses.list_expenses(session, actor, branch_id=branches[0].id, competence=date(2026, 9, 1), include_inactive=True)) == 1
    assert fixed_expenses.list_expenses(session, actor, branch_id=branches[1].id, competence=date(2026, 9, 1), include_inactive=True) == []
    other = ActorContext(organization_id=uuid.uuid4(), user_id=actor.user_id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="OWNER", permissions=actor.permissions)
    with pytest.raises(NotFoundError):
        fixed_expenses.set_status(session, other, created.id, FixedExpenseStatusUpdate(is_active=False, effective_from=fixed_expenses.current_month()))


@pytest.mark.parametrize("recurrence,expected", [(FixedExpenseRecurrence.MONTHLY, 12), (FixedExpenseRecurrence.QUARTERLY, 4), (FixedExpenseRecurrence.SEMIANNUAL, 2), (FixedExpenseRecurrence.ANNUAL, 1)])
def test_provisions_across_competences(context, recurrence, expected):
    session, actor, branches, category = context
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, recurrence=recurrence, amount="1200", start_month=fixed_expenses.current_month()))
    start = fixed_expenses.current_month()
    end = fixed_expenses.add_months(start, 12)
    rows = fixed_expenses.provisions(session, actor.organization_id, branch_id=branches[0].id, date_from=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc), date_to=datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc))
    assert len(rows) == expected
    assert rows[0].due_date.day in (28, 30, 31)


def test_optional_end_month_preserves_prior_competences(context):
    session, actor, branches, category = context
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, start_month=date(2026, 9, 1), end_month=date(2026, 11, 1), due_day=10))
    rows = fixed_expenses.provisions(session, actor.organization_id, branch_id=None, date_from=datetime(2026, 8, 1, tzinfo=timezone.utc), date_to=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert [row.competence_month for row in rows] == [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]


def test_create_rejects_silent_retroactivity(context):
    session, actor, branches, category = context
    with pytest.raises(ConflictError):
        fixed_expenses.create_expense(
            session, actor,
            payload(branches[0].id, category.id, start_month=fixed_expenses.add_months(fixed_expenses.current_month(), -1)),
        )


def test_rejects_branch_and_category_from_another_organization(context):
    session, actor, _branches, _category = context
    other_org = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org)})
    session.add(Organization(id=other_org, name="Outra org", slug=f"other-{other_org.hex[:8]}"))
    session.flush()
    other_branch = Branch(organization_id=other_org, name="Outra filial", slug=f"other-b-{other_org.hex[:8]}")
    other_category = FinancialCategory(organization_id=other_org, name="Outra fixa", nature=ExpenseNature.FIXED)
    session.add_all([other_branch, other_category])
    session.flush()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(actor.organization_id)})

    with pytest.raises(NotFoundError):
        fixed_expenses.create_expense(session, actor, payload(other_branch.id, _category.id))
    with pytest.raises(NotFoundError):
        fixed_expenses.create_expense(session, actor, payload(_branches[0].id, other_category.id))

    other_actor = ActorContext(
        organization_id=other_org, user_id=actor.user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="OWNER", permissions=actor.permissions,
    )
    assert fixed_expenses.list_expenses(
        session, other_actor, branch_id=None, competence=fixed_expenses.current_month(),
        include_inactive=False,
    ) == []
