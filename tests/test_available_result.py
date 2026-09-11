"""Testes do painel "Resultado disponível" (`services/dashboard.py::
_available_result`, exposto em `DashboardOverviewResponse.available_result`).

Cobertura: fórmula completa (Bruto - Impostos - Comissões - Taxas -
Custos variáveis - Despesas fixas), impostos provisionados respeitando
a alíquota vigente POR COMPETÊNCIA (nunca a atual sobre todo o
histórico), comissões/taxas vindas dos snapshots já existentes (nunca
recalculadas), despesas classificadas por natureza da categoria,
despesas não classificadas nunca somadas a fixo/variável, resultado
positivo/negativo, faturamento zero, e o gate de permissão de
Comissões (indicador auditável — nunca um total que muda de valor
dependendo de quem está olhando).

Mesmo padrão de `test_dashboard_bi_update.py`: direto no service layer
via `SessionLocal`, fixtures/helpers duplicados localmente."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CashMovementType,
    CashRegisterStatus,
    CommissionStatus,
    CommissionType,
    ExpenseNature,
    FixedExpenseRecurrence,
    OrderStatus,
    PaymentFeeStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.order import Order, OrderItem, Payment
from nexasalon_api.models.organization import Branch, BusinessHours, Organization
from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import Service
from nexasalon_api.schemas.financial_category import (
    FinancialCategoryCreate,
    FinancialCategoryUpdate,
)
from nexasalon_api.schemas.fixed_expense import FixedExpenseCreate
from nexasalon_api.schemas.tax_rate import TaxRateSet
from nexasalon_api.services import dashboard as dashboard_service
from nexasalon_api.services import financial_categories as financial_categories_service
from nexasalon_api.services import fixed_expenses as fixed_expenses_service
from nexasalon_api.services import tax_rates as tax_rates_service

_TZ = timezone(timedelta(hours=-3))
_order_number_counter = 500_000


def _next_order_number() -> int:
    global _order_number_counter
    _order_number_counter += 1
    return _order_number_counter


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        # O wheel Windows do pgserver não empacota a base IANA; UTC
        # mantém estes testes financeiros independentes dessa limitação.
        session.add(Organization(id=org_id, name="Org resultado", slug=f"org-result-{org_id.hex[:8]}", timezone="UTC"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=frozenset({"dashboard.view", "commissions.view_all", "organization.manage"})) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _branch(session, org_id, name="Unidade") -> Branch:
    b = Branch(organization_id=org_id, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _client(session, org_id, name="Cliente") -> Client:
    c = Client(organization_id=org_id, name=name)
    session.add(c)
    session.flush()
    return c


def _professional(session, org_id, branch_id, name="Profissional") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Serviço") -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=60, default_price=Decimal("100.00"))
    session.add(s)
    session.flush()
    return s


def _cash_register(session, org_id, branch_id, user_id) -> CashRegister:
    cr = CashRegister(
        organization_id=org_id, branch_id=branch_id, opened_by=user_id, opened_by_name="Caixa Teste",
        initial_amount=Decimal("0"), status=CashRegisterStatus.OPEN,
    )
    session.add(cr)
    session.flush()
    return cr


_MONTH_0 = fixed_expenses_service.current_month()
_MONTH_1 = fixed_expenses_service.add_months(_MONTH_0, 1)
_MONTH_2 = fixed_expenses_service.add_months(_MONTH_0, 2)


def _at(month: date, day: int, hour: int = 10):
    return datetime(month.year, month.month, day, hour, 0, tzinfo=_TZ)


def _sale(
    session, org_id, branch_id, client_id, professional_id, service_id, cash_register_id, *,
    closed_at, price=Decimal("100.00"),
    commission_type=None, commission_value=None, commission_amount=None, commission_status=None,
    fee_status=None, fee_percent_snapshot=None, fee_amount_snapshot=None, net_amount_snapshot=None,
    payment_method=PaymentMethod.PIX,
) -> Order:
    appt = Appointment(organization_id=org_id, branch_id=branch_id, client_id=client_id, status=AppointmentStatus.PAID)
    session.add(appt)
    session.flush()
    session.add(
        AppointmentItem(
            organization_id=org_id, appointment_id=appt.id, service_id=service_id, professional_id=professional_id,
            start_at=closed_at - timedelta(hours=1), end_at=closed_at, duration_minutes=60, price=price,
        )
    )
    order = Order(
        organization_id=org_id, order_number=_next_order_number(), appointment_id=appt.id,
        branch_id=branch_id, client_id=client_id, status=OrderStatus.CLOSED, closed_at=closed_at,
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            organization_id=org_id, order_id=order.id, service_id=service_id, professional_id=professional_id,
            duration_minutes=60, price=price, service_name="Serviço", professional_name="Profissional",
            commission_type_snapshot=commission_type, commission_value_snapshot=commission_value,
            commission_amount_snapshot=commission_amount, commission_status=commission_status,
        )
    )
    session.add(
        Payment(
            organization_id=org_id, order_id=order.id, cash_register_id=cash_register_id,
            method=payment_method, amount=price, created_by_name="Teste",
            fee_status=fee_status, fee_percent_snapshot=fee_percent_snapshot,
            fee_amount_snapshot=fee_amount_snapshot, net_amount_snapshot=net_amount_snapshot,
        )
    )
    session.flush()
    return order


def _withdrawal(session, org_id, register_id, user_id, *, amount, financial_category_id=None, fixed_expense_id=None, description="Despesa"):
    session.add(
        CashMovement(
            organization_id=org_id, cash_register_id=register_id, type=CashMovementType.WITHDRAWAL,
            amount=amount, description=description, financial_category_id=financial_category_id,
            fixed_expense_id=fixed_expense_id,
            created_by=user_id, created_by_name="Teste",
        )
    )
    session.flush()


def _fixed_expense(session, actor, branch_id, amount, *, category=None, name="Aluguel"):
    category = category or financial_categories_service.create_category(
        session, actor.organization_id,
        FinancialCategoryCreate(name="Estrutura", nature=ExpenseNature.FIXED, display_order=0),
    )
    return fixed_expenses_service.create_expense(session, actor, FixedExpenseCreate(
        name=name, financial_category_id=category.id, amount=amount,
        recurrence=FixedExpenseRecurrence.MONTHLY, due_day=10,
        start_month=_MONTH_0, branch_id=branch_id, is_active=True,
    ))


def test_formula_completa_com_todos_os_componentes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=_MONTH_0, tax_rate="6.00"))

    _sale(
        session, org_id, branch.id, client.id, professional.id, service.id, register.id,
        closed_at=_at(_MONTH_0, 10), price=Decimal("1000.00"),
        commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"),
        commission_amount=Decimal("200.00"), commission_status=CommissionStatus.CALCULATED,
        fee_status=PaymentFeeStatus.CALCULATED, fee_percent_snapshot=Decimal("3.00"),
        fee_amount_snapshot=Decimal("30.00"), net_amount_snapshot=Decimal("970.00"),
        payment_method=PaymentMethod.CREDIT,
    )

    variable_cat = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Produtos", nature=ExpenseNature.VARIABLE, display_order=0)
    )
    _fixed_expense(session, actor, branch.id, "50.00")
    _withdrawal(session, org_id, register.id, actor.user_id, amount=Decimal("100.00"), financial_category_id=variable_cat.id)
    _withdrawal(session, org_id, register.id, actor.user_id, amount=Decimal("20.00"), financial_category_id=None)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.available is True
    assert result.gross_revenue == Decimal("1000.00")
    assert result.taxes_provisioned == Decimal("60.00")
    assert result.commissions == Decimal("200.00")
    assert result.payment_fees == Decimal("30.00")
    assert result.variable_costs == Decimal("100.00")
    assert result.fixed_costs == Decimal("50.00")
    assert result.unclassified_expenses == Decimal("20.00")
    assert result.available_result == Decimal("560.00")  # 1000 - 60 - 200 - 30 - 100 - 50
    assert result.available_percent == Decimal("56.00")
    assert result.has_multiple_tax_rates is False
    assert result.single_tax_rate == Decimal("6.00")


def test_pix_com_taxa_configurada_e_deduzido_uma_unica_vez_no_resultado_disponivel(org_session):
    """Etapa N3.1 — taxa de Pix entra em `payment_fees` do "Resultado
    disponível" exatamente como taxa de cartão: mesma fonte
    (`known_fee_total`, lida do snapshot já congelado no `Payment`) que
    já alimenta `revenue_fee_summary` — nunca um segundo cálculo, nunca
    deduzida duas vezes."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    _sale(
        session, org_id, branch.id, client.id, professional.id, service.id, register.id,
        closed_at=_at(_MONTH_0, 10), price=Decimal("1000.00"),
        payment_method=PaymentMethod.PIX,
        fee_status=PaymentFeeStatus.CALCULATED, fee_percent_snapshot=Decimal("0.99"),
        fee_amount_snapshot=Decimal("9.90"), net_amount_snapshot=Decimal("990.10"),
    )

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.payment_fees == Decimal("9.90")
    assert result.available_result == Decimal("990.10")  # 1000 - 9.90, taxa deduzida uma única vez.


def test_pix_sem_taxa_configurada_nao_deduz_nada_no_resultado_disponivel(org_session):
    """Pix sem regra (comportamento padrão, ainda o mais comum) continua
    `NOT_APPLICABLE` — `payment_fees` fica 0, igual a antes desta
    feature existir."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    _sale(
        session, org_id, branch.id, client.id, professional.id, service.id, register.id,
        closed_at=_at(_MONTH_0, 10), price=Decimal("500.00"), payment_method=PaymentMethod.PIX,
    )

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.payment_fees == Decimal("0")
    assert result.available_result == Decimal("500.00")


def test_periodo_com_duas_competencias_aplica_a_aliquota_de_cada_uma(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    tax_rates_service.set_rate(session, actor, TaxRateSet(competence_month=_MONTH_0, tax_rate="6.00"))
    tax_rates_service.set_rate(
        session, actor, TaxRateSet(competence_month=_MONTH_1, tax_rate="8.00")
    )

    _sale(session, org_id, branch.id, client.id, professional.id, service.id, register.id, closed_at=_at(_MONTH_0, 15), price=Decimal("800.00"))
    _sale(session, org_id, branch.id, client.id, professional.id, service.id, register.id, closed_at=_at(_MONTH_1, 15), price=Decimal("1000.00"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_2, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    # 800*6% + 1000*8% = 48 + 80 = 128 — NUNCA (800+1000)*8%=144.
    assert result.taxes_provisioned == Decimal("128.00")
    assert result.has_multiple_tax_rates is True
    assert result.single_tax_rate is None
    assert len(result.tax_breakdown) == 2


def test_competencia_com_faturamento_e_sem_aliquota_configurada_nunca_e_tratada_como_zero(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    _sale(session, org_id, branch.id, client.id, professional.id, service.id, register.id, closed_at=_at(_MONTH_0, 15), price=Decimal("500.00"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.has_unconfigured_tax_rate is True
    assert result.unconfigured_tax_revenue == Decimal("500.00")
    assert result.taxes_provisioned == Decimal("0")  # nada provisionado sem alíquota conhecida — nunca inventado.


def test_despesa_nao_classificada_nunca_conta_como_fixa_ou_variavel(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    _withdrawal(session, org_id, register.id, actor.user_id, amount=Decimal("300.00"), financial_category_id=None)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.unclassified_expenses == Decimal("300.00")
    assert result.variable_costs == Decimal("0")
    assert result.fixed_costs == Decimal("0")


def test_pagamento_vinculado_nao_duplica_provisao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)
    expense = _fixed_expense(session, actor, branch.id, "2420.00")
    _withdrawal(
        session, org_id, register.id, actor.user_id, amount=Decimal("2420.00"),
        financial_category_id=expense.financial_category_id, fixed_expense_id=expense.id,
    )

    result = dashboard_service.get_overview(
        session, actor, branch_id=branch.id, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert result.fixed_costs == Decimal("2420.00")
    assert result.linked_fixed_payments == Decimal("2420.00")
    assert result.available_result == Decimal("-2420.00")


def test_fixed_legado_sem_vinculo_nao_desaparece(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Fixo legado", nature=ExpenseNature.FIXED, display_order=0)
    )
    _withdrawal(session, org_id, register.id, actor.user_id, amount=Decimal("850.00"), financial_category_id=category.id)

    result = dashboard_service.get_overview(
        session, actor, branch_id=branch.id, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert result.fixed_costs == Decimal("0")
    assert result.legacy_fixed_costs == Decimal("850.00")
    assert result.available_result == Decimal("-850.00")


def test_fallback_legado_consolidado_respeita_categoria_e_filial(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_a = _branch(session, org_id, "Filial A")
    branch_b = _branch(session, org_id, "Filial B")
    register_b = _cash_register(session, org_id, branch_b.id, actor.user_id)
    category = financial_categories_service.create_category(
        session, org_id,
        FinancialCategoryCreate(name="Estrutura compartilhada", nature=ExpenseNature.FIXED, display_order=0),
    )
    _fixed_expense(session, actor, branch_a.id, "2420.00", category=category)
    _withdrawal(
        session, org_id, register_b.id, actor.user_id,
        amount=Decimal("850.00"), financial_category_id=category.id,
    )

    result = dashboard_service.get_overview(
        session, actor, branch_id=None,
        date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert result.fixed_costs == Decimal("2420.00")
    assert result.legacy_fixed_costs == Decimal("850.00")
    assert result.available_result == Decimal("-3270.00")


def test_cenario_financeiro_aprovado_e_pagamento_nao_duplica(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)
    tax_rates_service.set_rate(
        session, actor, TaxRateSet(competence_month=_MONTH_0, tax_rate="6.00")
    )
    _sale(
        session, org_id, branch.id, client.id, professional.id, service.id, register.id,
        closed_at=_at(_MONTH_0, 10), price=Decimal("80000.00"),
        commission_type=CommissionType.FIXED, commission_value=Decimal("15000.00"),
        commission_amount=Decimal("15000.00"), commission_status=CommissionStatus.CALCULATED,
        fee_status=PaymentFeeStatus.CALCULATED,
        fee_amount_snapshot=Decimal("2000.00"), net_amount_snapshot=Decimal("78000.00"),
        payment_method=PaymentMethod.CREDIT,
    )
    variable = financial_categories_service.create_category(
        session, org_id,
        FinancialCategoryCreate(name="Custos variáveis", nature=ExpenseNature.VARIABLE, display_order=0),
    )
    fixed_category = financial_categories_service.create_category(
        session, org_id,
        FinancialCategoryCreate(name="Estrutura", nature=ExpenseNature.FIXED, display_order=0),
    )
    _withdrawal(
        session, org_id, register.id, actor.user_id,
        amount=Decimal("18000.00"), financial_category_id=variable.id,
    )
    expenses = [
        _fixed_expense(session, actor, branch.id, amount, category=fixed_category, name=name)
        for name, amount in (
            ("Aluguel", "2420.00"), ("Condomínio", "850.00"),
            ("Internet", "150.00"), ("Contabilidade", "500.00"),
        )
    ]

    before = dashboard_service.get_overview(
        session, actor, branch_id=branch.id,
        date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert before.gross_revenue == Decimal("80000.00")
    assert before.taxes_provisioned == Decimal("4800.00")
    assert before.commissions == Decimal("15000.00")
    assert before.payment_fees == Decimal("2000.00")
    assert before.variable_costs == Decimal("18000.00")
    assert before.fixed_costs == Decimal("3920.00")
    assert before.available_result == Decimal("36280.00")

    _withdrawal(
        session, org_id, register.id, actor.user_id, amount=Decimal("2420.00"),
        financial_category_id=fixed_category.id, fixed_expense_id=expenses[0].id,
        description="Pagamento do aluguel",
    )
    after = dashboard_service.get_overview(
        session, actor, branch_id=branch.id,
        date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert after.linked_fixed_payments == Decimal("2420.00")
    assert after.available_result == Decimal("36280.00")


def test_resultado_negativo_quando_deducoes_superam_o_faturamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)

    _sale(session, org_id, branch.id, client.id, professional.id, service.id, register.id, closed_at=_at(_MONTH_0, 15), price=Decimal("100.00"))
    _fixed_expense(session, actor, branch.id, "5000.00")

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.available_result < 0


def test_faturamento_zero_nunca_divide_por_zero(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.gross_revenue == Decimal("0")
    assert result.available_percent is None
    assert result.available_result == Decimal("0")


def test_sem_permissao_de_comissoes_o_painel_inteiro_fica_indisponivel(org_session):
    """Nunca mostra um total que mudaria de valor dependendo de quem
    está olhando — sem `commissions.view_all`/`commissions.manage`,
    `available=False` e todo o resto vem `None`."""
    session, org_id = org_session
    actor = _actor(session, org_id, permissions=frozenset({"dashboard.view"}))
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    professional = _professional(session, org_id, branch.id)
    service = _service(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, professional.id, service.id, register.id, closed_at=_at(_MONTH_0, 15), price=Decimal("500.00"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    )
    result = overview.available_result
    assert result.available is False
    assert result.gross_revenue is None
    assert result.available_result is None


def test_reclassificar_categoria_nao_duplica_despesa_fixa_provisionada(org_session):
    """Categoria em uso não pode ser reclassificada nem apagar impacto."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = _cash_register(session, org_id, branch.id, actor.user_id)
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Produtos", nature=ExpenseNature.VARIABLE, display_order=0)
    )
    _withdrawal(session, org_id, register.id, actor.user_id, amount=Decimal("70.00"), financial_category_id=category.id)

    before = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert before.variable_costs == Decimal("70.00")
    assert before.fixed_costs == Decimal("0")

    with pytest.raises(ConflictError):
        financial_categories_service.update_category(
            session, org_id, category.id, FinancialCategoryUpdate(name="Produtos", nature=ExpenseNature.FIXED, display_order=0)
        )

    after = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_at(_MONTH_0, 1), date_to=_at(_MONTH_1, 1),
        compare_from=None, compare_to=None,
    ).available_result
    assert after.variable_costs == Decimal("70.00")
    assert after.fixed_costs == Decimal("0")


def test_resultado_disponivel_deduz_e_exibe_o_rateio_gerencial_em_periodo_parcial(org_session):
    """`fixed_costs` no painel passa a ser a visão GERENCIAL (rateio por
    dias operacionais de `BusinessHours`), não o valor provisionado
    bruto — mesmo exemplo do relatório aprovado: Aluguel R$2.420,
    unidade terça-sábado, Setembro/2026 com 22 dias operacionais. O mês
    inteiro fecha exatamente com o valor provisionado; um dia isolado
    mostra só a fração daquele dia — nunca os dois números ao mesmo
    tempo no card."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    category = financial_categories_service.create_category(
        session, org_id, FinancialCategoryCreate(name="Estrutura", nature=ExpenseNature.FIXED, display_order=0)
    )
    for weekday, is_open in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, True), (6, True)]:
        session.add(
            BusinessHours(
                organization_id=org_id, weekday=weekday, is_open=is_open,
                start_time=time(9, 0) if is_open else None, end_time=time(19, 0) if is_open else None,
            )
        )
    session.flush()
    fixed_expenses_service.create_expense(session, actor, FixedExpenseCreate(
        name="Aluguel", financial_category_id=category.id, amount=Decimal("2420.00"),
        recurrence=FixedExpenseRecurrence.MONTHLY, due_day=10,
        start_month=date(2026, 9, 1), branch_id=branch.id, is_active=True,
    ))

    mes_inteiro = dashboard_service.get_overview(
        session, actor, branch_id=None,
        date_from=datetime(2026, 9, 1, tzinfo=timezone.utc), date_to=datetime(2026, 10, 1, tzinfo=timezone.utc),
        compare_from=None, compare_to=None,
    ).available_result
    assert mes_inteiro.fixed_costs == Decimal("2420.00")

    # Quarta-feira 09/09/2026 isolada — dia aberto: R$110 (2420/22), nunca o valor cheio do mês.
    hoje_aberto = dashboard_service.get_overview(
        session, actor, branch_id=None,
        date_from=datetime(2026, 9, 9, tzinfo=timezone.utc), date_to=datetime(2026, 9, 10, tzinfo=timezone.utc),
        compare_from=None, compare_to=None,
    ).available_result
    assert hoje_aberto.fixed_costs == Decimal("110.00")
    assert len(hoje_aberto.fixed_expense_breakdown) == 1
    assert hoje_aberto.fixed_expense_breakdown[0].amount == Decimal("110.00")

    # Domingo 06/09/2026 — dia fechado: R$0, nunca negativo nem inventado.
    hoje_fechado = dashboard_service.get_overview(
        session, actor, branch_id=None,
        date_from=datetime(2026, 9, 6, tzinfo=timezone.utc), date_to=datetime(2026, 9, 7, tzinfo=timezone.utc),
        compare_from=None, compare_to=None,
    ).available_result
    assert hoje_fechado.fixed_costs == Decimal("0")
    assert hoje_fechado.fixed_expense_breakdown == []
