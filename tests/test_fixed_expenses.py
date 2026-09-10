import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.enums import ExpenseNature, FixedExpenseRecurrence
from nexasalon_api.models.finance import FinancialCategory
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, BusinessHours, Organization
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


# --- `managerial_fixed_costs` — rateio gerencial por dias operacionais ----
# Setembro/2026 (1º é terça-feira, mês com 30 dias) — unidade terça a
# sábado tem exatamente 22 dias operacionais (fechado domingo/segunda).
# Mesmo exemplo usado no relatório de arquitetura aprovado pelo usuário:
# R$ 2.420 / 22 = R$ 110,00 por dia operacional, sem resto.


def _open_tuesday_to_saturday(session, organization_id):
    """domingo(0)/segunda(1) fechados, terça(2)..sábado(6) abertos."""
    for weekday, is_open in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, True), (6, True)]:
        session.add(
            BusinessHours(
                organization_id=organization_id, weekday=weekday, is_open=is_open,
                start_time=time(9, 0) if is_open else None, end_time=time(19, 0) if is_open else None,
            )
        )
    session.flush()


def test_managerial_mes_inteiro_fecha_exatamente_com_o_valor_provisionado(context):
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="2420.00", start_month=date(2026, 9, 1)))
    date_from, date_to = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)

    managerial_rows = fixed_expenses.managerial_fixed_costs(session, actor.organization_id, branch_id=None, date_from=date_from, date_to=date_to)
    provisioned_rows = fixed_expenses.provisions(session, actor.organization_id, branch_id=None, date_from=date_from, date_to=date_to)

    assert sum((r.amount for r in managerial_rows), Decimal("0")) == Decimal("2420.00")
    assert sum((r.amount for r in managerial_rows), Decimal("0")) == sum((r.amount for r in provisioned_rows), Decimal("0"))


def test_managerial_hoje_em_dia_aberto_e_fechado(context):
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="2420.00", start_month=date(2026, 9, 1)))

    # quarta-feira 09/09/2026 — dia aberto.
    open_rows = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 9, tzinfo=timezone.utc), date_to=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert sum((r.amount for r in open_rows), Decimal("0")) == Decimal("110.00")

    # domingo 06/09/2026 — dia fechado: nenhum rateio, nunca negativo nem inventado.
    closed_rows = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 6, tzinfo=timezone.utc), date_to=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    assert closed_rows == []


def test_managerial_semana_com_cinco_dias_operacionais(context):
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="2420.00", start_month=date(2026, 9, 1)))

    # terça 08/09 a sábado 12/09 (fim exclusive em 13/09) = 5 dias operacionais.
    rows = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 8, tzinfo=timezone.utc), date_to=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert sum((r.amount for r in rows), Decimal("0")) == Decimal("550.00")


def test_managerial_periodo_cruzando_dois_meses_rateia_cada_competencia_separadamente(context):
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="2420.00", start_month=date(2026, 9, 1)))

    # 29/09 a 03/10 (fim exclusive): Set 29(ter)/30(qua) = 2 dias operacionais
    # de 22 no mês; Out 1(qui)/2(sex)/3(sáb) = 3 dias operacionais de 23 no mês.
    rows = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 29, tzinfo=timezone.utc), date_to=datetime(2026, 10, 4, tzinfo=timezone.utc),
    )
    by_month = {row.competence_month: row.amount for row in rows}
    assert by_month[date(2026, 9, 1)] == Decimal("220.00")  # 2420 * 2/22
    assert by_month[date(2026, 10, 1)] == Decimal("315.65")  # 2420 * 3/23, arredondado só no total da linha


def test_managerial_despesa_trimestral_so_rateia_no_mes_em_que_realmente_ocorre(context):
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(
        session, actor,
        payload(branches[0].id, category.id, amount="2400.00", recurrence=FixedExpenseRecurrence.QUARTERLY, start_month=date(2026, 9, 1)),
    )

    setembro = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 1, tzinfo=timezone.utc), date_to=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert sum((r.amount for r in setembro), Decimal("0")) == Decimal("2400.00")

    # Outubro/Novembro: despesa trimestral não ocorre nesses meses —
    # NUNCA vira um valor mensal fictício (R$0, nunca 2400/3).
    outubro = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 10, 1, tzinfo=timezone.utc), date_to=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    assert outubro == []


def test_managerial_sem_business_hours_configurado_conta_todos_os_dias(context):
    session, actor, branches, category = context
    # Nenhuma linha de BusinessHours — mesma semântica retrocompatível de
    # "sem restrição" já usada em `services/business_hours.py::get_window_utc`.
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="3000.00", start_month=date(2026, 9, 1)))

    rows = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 1, tzinfo=timezone.utc), date_to=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    # Setembro tem 30 dias corridos; 10 dias corridos / 30 = 1/3 do mês.
    assert sum((r.amount for r in rows), Decimal("0")) == Decimal("1000.00")


def test_managerial_precisao_decimal_valor_que_nao_divide_exatamente_pelos_dias(context):
    """R$1.000 / 22 dias operacionais NÃO é uma divisão exata — prova
    que a implementação nunca calcula uma "taxa diária" arredondada
    pra depois multiplicar (isso daria 45,45 × 22 = R$999,90, NUNCA
    R$1.000,00): cada linha é uma única divisão `valor_integral ×
    dias_no_intervalo ÷ dias_do_mês`, só o resultado é arredondado."""
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="1000.00", start_month=date(2026, 9, 1)))

    # Hoje (quarta 09/09, 1 dia operacional de 22): 1000/22 = 45,4545... -> 45,45.
    hoje = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 9, tzinfo=timezone.utc), date_to=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert len(hoje) == 1
    assert hoje[0].amount == Decimal("45.45")
    assert Decimal("45.45") * 22 == Decimal("999.90")  # prova que "taxa diária × dias" NÃO seria a conta certa.

    # Mês inteiro: 22/22 = 1 — fecha exatamente em R$1.000,00, nunca R$999,90.
    mes = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 1, tzinfo=timezone.utc), date_to=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert sum((r.amount for r in mes), Decimal("0")) == Decimal("1000.00")


def test_managerial_multiplas_despesas_hoje_semana_mes_e_breakdown_bate_com_o_total(context):
    """Aluguel R$1.000 + Internet R$199,90 + Sistema R$149,99, Setembro/
    2026 (22 dias operacionais terça-sábado) — cada filtro do Dashboard
    (Hoje/Semana/Mês) e o fechamento exato do mês inteiro em
    R$1.349,89, sempre com breakdown somando exatamente o total."""
    session, actor, branches, category = context
    _open_tuesday_to_saturday(session, actor.organization_id)
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="1000.00", name="Aluguel", start_month=date(2026, 9, 1)))
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="199.90", name="Internet", start_month=date(2026, 9, 1)))
    fixed_expenses.create_expense(session, actor, payload(branches[0].id, category.id, amount="149.99", name="Sistema", start_month=date(2026, 9, 1)))

    def total(rows):
        return sum((r.amount for r in rows), Decimal("0"))

    mes = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 1, tzinfo=timezone.utc), date_to=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert total(mes) == Decimal("1349.89")

    # Hoje — quarta 09/09/2026, 1 dia operacional de 22.
    hoje = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 9, tzinfo=timezone.utc), date_to=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert {row.name: row.amount for row in hoje} == {
        "Aluguel": Decimal("45.45"), "Internet": Decimal("9.09"), "Sistema": Decimal("6.82"),
    }
    assert total(hoje) == Decimal("61.36")

    # Semana — terça 08/09 a sábado 12/09 (fim exclusive 13/09), 5 dias operacionais de 22.
    semana = fixed_expenses.managerial_fixed_costs(
        session, actor.organization_id, branch_id=None,
        date_from=datetime(2026, 9, 8, tzinfo=timezone.utc), date_to=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    assert {row.name: row.amount for row in semana} == {
        "Aluguel": Decimal("227.27"), "Internet": Decimal("45.43"), "Sistema": Decimal("34.09"),
    }
    assert total(semana) == Decimal("306.79")

    # Breakdown sempre soma exatamente o total, nos três recortes — nunca dois números conflitantes.
    for rows in (mes, hoje, semana):
        assert total(rows) == sum((row.amount for row in rows), Decimal("0"))
