"""Testes do Dashboard/BI (`services/dashboard.py`) — foco em REGRAS DE
DADOS, não renderização (item 17 do pedido). Direto no service layer via
`SessionLocal`, construindo Appointment/Order/OrderItem/Payment
diretamente pelo ORM (não via `services/appointments.py`/`orders.py`)
porque estes testes precisam de datas HISTÓRICAS controladas (mês
passado, ano passado, "90 dias atrás") que os serviços reais não
permitem simular (`close_order` sempre usa `datetime.now()`).

Cobertura: isolamento multi-tenant, filtro de unidade, período atual x
comparativo, faturamento (soma de `OrderItem` de comanda FECHADA, nunca
`AppointmentItem`/estimativa), ticket médio (incl. divisão por zero),
clientes únicos, cliente novo por primeira visita real, taxa de faltas
com elegibilidade, pontos percentuais na comparação de taxas, top
serviços, desempenho por profissional, formas de pagamento (bucket),
novos×recorrentes, retenção 90 dias com censura temporal, status por
código interno, e a prova explícita de que nenhuma query duplica
faturamento por causa de JOIN 1:N entre Order/OrderItem/Payment."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.cash_register import CashRegister
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus, CashRegisterStatus, OrderStatus, PaymentMethod
from nexasalon_api.models.identity import User
from nexasalon_api.models.order import Order, OrderItem, Payment
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import Service
from nexasalon_api.services import dashboard as dashboard_service

_TZ = timezone(timedelta(hours=-3))


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org dashboard", slug=f"org-dash-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset({"dashboard.view"}),
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


_order_number_counter = 0


def _next_order_number() -> int:
    global _order_number_counter
    _order_number_counter += 1
    return _order_number_counter


def _appointment(
    session, org_id, branch_id, client_id, professional_id, service_id, *,
    start_at: datetime, duration_minutes=60, status=AppointmentStatus.SCHEDULED, price=Decimal("100.00"),
) -> Appointment:
    appt = Appointment(organization_id=org_id, branch_id=branch_id, client_id=client_id, status=status)
    session.add(appt)
    session.flush()
    item = AppointmentItem(
        organization_id=org_id, appointment_id=appt.id, service_id=service_id, professional_id=professional_id,
        start_at=start_at, end_at=start_at + timedelta(minutes=duration_minutes), duration_minutes=duration_minutes,
        price=price,
    )
    session.add(item)
    session.flush()
    session.refresh(appt)
    return appt


def _closed_order(
    session, org_id, branch_id, client_id, appointment_id, *,
    closed_at: datetime,
    items: list[dict],
    payments: list[dict],
    cash_register_id,
) -> Order:
    order = Order(
        organization_id=org_id, order_number=_next_order_number(), appointment_id=appointment_id,
        branch_id=branch_id, client_id=client_id, status=OrderStatus.CLOSED, closed_at=closed_at,
    )
    session.add(order)
    session.flush()
    for it in items:
        session.add(
            OrderItem(
                organization_id=org_id, order_id=order.id, service_id=it["service_id"],
                professional_id=it["professional_id"], duration_minutes=it.get("duration_minutes", 60),
                price=it["price"], service_name=it.get("service_name", "Serviço"),
                professional_name=it.get("professional_name", "Profissional"),
            )
        )
    for p in payments:
        session.add(
            Payment(
                organization_id=org_id, order_id=order.id, cash_register_id=cash_register_id,
                method=p["method"], amount=p["amount"], created_by_name="Teste",
            )
        )
    session.flush()
    return order


def _sale(
    session, org_id, branch_id, client_id, professional_id, service_id, cash_register_id, *,
    closed_at: datetime, price=Decimal("100.00"), method=PaymentMethod.PIX,
    service_name="Serviço", professional_name="Profissional", appointment_status=AppointmentStatus.PAID,
) -> Order:
    """Atalho: 1 agendamento + 1 comanda fechada com 1 item + 1 pagamento
    — o caso comum usado pela maioria dos testes de faturamento."""
    appt = _appointment(
        session, org_id, branch_id, client_id, professional_id, service_id,
        start_at=closed_at - timedelta(hours=1), status=appointment_status, price=price,
    )
    return _closed_order(
        session, org_id, branch_id, client_id, appt.id, closed_at=closed_at,
        items=[
            {
                "service_id": service_id, "professional_id": professional_id, "price": price,
                "service_name": service_name, "professional_name": professional_name,
            }
        ],
        payments=[{"method": method, "amount": price}],
        cash_register_id=cash_register_id,
    )


def _dt(y, m, d, h=10):
    return datetime(y, m, d, h, 0, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# Isolamento / filtros
# ---------------------------------------------------------------------------


def test_isolamento_entre_organizacoes(org_session):
    session, org_a = org_session
    actor_a = _actor(session, org_a)
    branch_a = _branch(session, org_a)
    client_a = _client(session, org_a)
    prof_a = _professional(session, org_a, branch_a.id)
    cr_a = _cash_register(session, org_a, branch_a.id, actor_a.user_id)
    service_a = _service(session, org_a)
    _sale(session, org_a, branch_a.id, client_a.id, prof_a.id, service_a.id, cr_a.id, closed_at=_dt(2026, 8, 10), price=Decimal("100"))

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_b)
    branch_b = _branch(session, org_b)
    client_b = _client(session, org_b)
    prof_b = _professional(session, org_b, branch_b.id)
    cr_b = _cash_register(session, org_b, branch_b.id, actor_b.user_id)
    service_b = _service(session, org_b)
    _sale(session, org_b, branch_b.id, client_b.id, prof_b.id, service_b.id, cr_b.id, closed_at=_dt(2026, 8, 10), price=Decimal("500"))

    # volta pro contexto de RLS da org A antes de consultar (troca de
    # `app.current_org_id` — mesma mecânica de `get_db` numa request real).
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})

    overview = dashboard_service.get_overview(
        session, actor_a, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.value == Decimal("100")  # nunca R$600 (100+500) — org B não vaza pra org A.


def test_filtro_por_unidade(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch1 = _branch(session, org_id, "Unidade 1")
    branch2 = _branch(session, org_id, "Unidade 2")
    client = _client(session, org_id)
    prof1 = _professional(session, org_id, branch1.id)
    prof2 = _professional(session, org_id, branch2.id)
    cr1 = _cash_register(session, org_id, branch1.id, actor.user_id)
    cr2 = _cash_register(session, org_id, branch2.id, actor.user_id)
    service_id = _service(session, org_id).id
    _sale(session, org_id, branch1.id, client.id, prof1.id, service_id, cr1.id, closed_at=_dt(2026, 8, 10), price=Decimal("100"))
    _sale(session, org_id, branch2.id, client.id, prof2.id, service_id, cr2.id, closed_at=_dt(2026, 8, 10), price=Decimal("250"))

    overview_branch1 = dashboard_service.get_overview(
        session, actor, branch_id=branch1.id, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    overview_all = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview_branch1.kpis.revenue.value == Decimal("100")
    assert overview_all.kpis.revenue.value == Decimal("350")


def test_periodo_atual_conta_so_vendas_dentro_do_intervalo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 7, 31, 23), price=Decimal("10"))  # antes
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 15), price=Decimal("100"))  # dentro
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 9, 1), price=Decimal("10"))  # depois (exclusive)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.value == Decimal("100")


# ---------------------------------------------------------------------------
# Faturamento / ticket médio / clientes
# ---------------------------------------------------------------------------


def test_faturamento_usa_orderitem_de_comanda_fechada_nao_orderitem_de_comanda_aberta(org_session):
    """Uma comanda ABERTA (nunca fechada, nenhum pagamento registrado)
    não pode contar como faturamento — senão "faturamento" viraria
    estimativa de agendamento em vez de dinheiro recebido."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    service_id = _service(session, org_id).id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9))
    order = Order(
        organization_id=org_id, order_number=_next_order_number(), appointment_id=appt.id, branch_id=branch.id,
        client_id=client.id, status=OrderStatus.OPEN,
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            organization_id=org_id, order_id=order.id, service_id=service_id, professional_id=prof.id,
            duration_minutes=60, price=Decimal("9999.00"), service_name="Serviço", professional_name="Profissional",
        )
    )
    session.flush()

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.value == Decimal("0")


def test_faturamento_reflete_preco_editado_da_comanda_nao_o_preco_do_agendamento(org_session):
    """`OrderItem.price` pode ser editado na comanda (lápis na UI) e fica
    independente do `AppointmentItem.price` original — o Dashboard tem
    que ler o preço da COMANDA (o que de fato foi cobrado), não o do
    agendamento (o que foi reservado)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    appt = _appointment(
        session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9), price=Decimal("100.00")
    )
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 10),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("70.00")}],  # desconto dado na comanda
        payments=[{"method": PaymentMethod.PIX, "amount": Decimal("70.00")}],
        cash_register_id=cr.id,
    )

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.value == Decimal("70.00")  # nunca 100 (preço do agendamento).


def test_ticket_medio_e_faturamento_dividido_por_comandas_fechadas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    for i, price in enumerate([Decimal("100"), Decimal("300")]):
        client = _client(session, org_id, name=f"Cliente {i}")
        _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 10 + i), price=price)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.value == Decimal("400")
    assert overview.kpis.ticket_average.value == Decimal("200.00")  # 400 / 2 comandas


def test_ticket_medio_zero_sem_erro_quando_nao_ha_comandas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.ticket_average.value == Decimal("0")
    assert overview.kpis.revenue.value == Decimal("0")


def test_clientes_atendidos_conta_unicos_nao_numero_de_comandas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    # mesmo cliente, 2 comandas fechadas no período.
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 5), price=Decimal("50"))
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 20), price=Decimal("50"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.clients_served.value == Decimal("1")
    assert overview.kpis.revenue.value == Decimal("100")


def test_cliente_novo_usa_primeira_visita_real_nao_created_at(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    # Cliente cadastrado há muito tempo (created_at antigo) mas cuja
    # PRIMEIRA comanda fechada só acontece dentro do período — conta
    # como novo (a definição é "primeira visita real", não cadastro).
    client_new = _client(session, org_id, name="Cliente Novo")
    client_new.created_at = _dt(2020, 1, 1)
    session.flush()
    _sale(session, org_id, branch.id, client_new.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("100"))

    # Cliente recorrente: primeira visita foi ANTES do período (em
    # julho); uma segunda visita cai dentro do período — não deve
    # contar como novo cliente em agosto.
    client_returning = _client(session, org_id, name="Cliente Recorrente")
    _sale(session, org_id, branch.id, client_returning.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 7, 5), price=Decimal("80"))
    _sale(session, org_id, branch.id, client_returning.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 12), price=Decimal("80"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.new_clients.value == Decimal("1")
    assert overview.kpis.clients_served.value == Decimal("2")


# ---------------------------------------------------------------------------
# Agendamentos / faltas / pontos percentuais
# ---------------------------------------------------------------------------


def test_taxa_de_faltas_usa_elegiveis_excluindo_cancelados(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    service_id = _service(session, org_id).id

    _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 5, 9), status=AppointmentStatus.NO_SHOW)
    _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 6, 9), status=AppointmentStatus.CONFIRMED)
    _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 7, 9), status=AppointmentStatus.CANCELLED)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    # elegíveis = NO_SHOW + CONFIRMED (cancelado fica de fora do denominador) = 2; faltas = 1 -> 50%.
    assert overview.kpis.no_show_rate.value == Decimal("50.00")
    assert overview.kpis.appointments_count.value == Decimal("3")  # agendamentos conta TODOS, inclusive cancelado.


def test_pontos_percentuais_na_comparacao_de_taxa_nao_percentual_de_percentual(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    service_id = _service(session, org_id).id

    # Julho: 10 elegíveis, 8 faltas -> 80%.
    for i in range(8):
        _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 7, 1 + i, 9), status=AppointmentStatus.NO_SHOW)
    for i in range(2):
        _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 7, 9 + i, 9), status=AppointmentStatus.CONFIRMED)
    # Agosto: 10 elegíveis, 2 faltas -> 20%.
    for i in range(2):
        _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 1 + i, 9), status=AppointmentStatus.NO_SHOW)
    for i in range(8):
        _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 3 + i, 9), status=AppointmentStatus.CONFIRMED)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=_dt(2026, 7, 1, 0), compare_to=_dt(2026, 8, 1, 0),
    )
    kpi = overview.kpis.no_show_rate
    assert kpi.value == Decimal("20.00")
    assert kpi.comparison_value == Decimal("80.00")
    assert kpi.delta_points == pytest.approx(-60.0)  # 20 - 80, NUNCA "-75%" (percentual de percentual).
    assert kpi.delta_percent is None  # taxa nunca usa delta_percent como comparação principal.


def test_divisao_por_zero_periodo_anterior_zero_nao_gera_infinity_nem_nan(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    # sem nenhuma venda em julho (comparativo) — só em agosto.
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("150"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=_dt(2026, 7, 1, 0), compare_to=_dt(2026, 8, 1, 0),
    )
    kpi = overview.kpis.revenue
    assert kpi.has_comparison is True
    assert kpi.comparison_value == Decimal("0")
    assert kpi.delta_absolute == Decimal("150")
    assert kpi.delta_percent is None  # "sem base de comparação" — nunca Infinity/NaN.


def test_sem_periodo_comparativo_kpi_marca_has_comparison_false(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.has_comparison is False
    assert overview.kpis.revenue.comparison_value is None
    assert overview.kpis.revenue.delta_percent is None


def test_data_final_antes_da_inicial_e_rejeitada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    with pytest.raises(ValidationDomainError):
        dashboard_service.get_overview(
            session, actor, branch_id=None, date_from=_dt(2026, 8, 31, 0), date_to=_dt(2026, 8, 1, 0),
            compare_from=None, compare_to=None,
        )


# ---------------------------------------------------------------------------
# Top serviços / profissionais / formas de pagamento
# ---------------------------------------------------------------------------


def test_top_servicos_agrega_por_servico_sem_duplicar_faturamento(org_session):
    """UMA comanda com 2 serviços e pagamento MISTO (2 lançamentos de
    Payment) — o join Order->OrderItem->Payment é o risco clássico de
    duplicar faturamento (produto cartesiano 2 itens x 2 pagamentos =
    4 linhas). `_fetch_period_data` nunca faz esse join junto; este
    teste prova que o total bate exatamente com a soma dos itens."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    corte_id = _service(session, org_id, name="Corte").id
    coloracao_id = _service(session, org_id, name="Coloração").id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, corte_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 11),
        items=[
            {"service_id": corte_id, "professional_id": prof.id, "price": Decimal("100"), "service_name": "Corte"},
            {"service_id": coloracao_id, "professional_id": prof.id, "price": Decimal("280"), "service_name": "Coloração"},
        ],
        payments=[
            {"method": PaymentMethod.PIX, "amount": Decimal("200")},
            {"method": PaymentMethod.CREDIT, "amount": Decimal("180")},
        ],
        cash_register_id=cr.id,
    )

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.revenue.value == Decimal("380")  # 100+280, nunca 760 (x2) nem 1520 (x4).
    services_by_name = {row.service_name: row for row in overview.top_services}
    assert services_by_name["Corte"].revenue == Decimal("100")
    assert services_by_name["Coloração"].revenue == Decimal("280")
    assert services_by_name["Corte"].quantity == 1
    total_top_services = sum((r.revenue for r in overview.top_services), Decimal("0"))
    assert total_top_services == Decimal("380")

    payment_total = sum((r.amount for r in overview.payment_methods), Decimal("0"))
    assert payment_total == Decimal("380")  # formas de pagamento também não duplica.


# ---------------------------------------------------------------------------
# Faturamento × Recebido — reconciliação (`RevenueReconciliation`, drill-down
# de `revenue`). Três conceitos que NUNCA se misturam: Faturamento
# (`OrderItem`, o que foi vendido) sempre vem do drill-down `revenue`;
# Recebido (`Payment.amount`, o que entrou de fato) só aparece aqui — a
# soma de pagamentos nunca altera o valor de Faturamento, só sinaliza
# pendência/excedente.
# ---------------------------------------------------------------------------


def _revenue_detail(session, actor):
    return dashboard_service.get_kpi_detail(
        session, actor, key="revenue", branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )


def test_reconciliacao_comanda_paga_exatamente_o_total(org_session):
    """Comanda de R$800 paga R$800 (um único pagamento) — sem
    pendência, sem excedente."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 11),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("800")}],
        payments=[{"method": PaymentMethod.CASH, "amount": Decimal("800")}],
        cash_register_id=cr.id,
    )

    detail = _revenue_detail(session, actor)
    assert detail.kpi.value == Decimal("800")
    assert detail.reconciliation is not None
    assert detail.reconciliation.revenue == Decimal("800")
    assert detail.reconciliation.received == Decimal("800")
    assert detail.reconciliation.pending_amount == Decimal("0")
    assert detail.reconciliation.overpaid_amount == Decimal("0")


def test_reconciliacao_pagamento_misto_nao_duplica_faturamento_nem_gera_diferenca(org_session):
    """R$300 Pix + R$500 Crédito numa comanda de R$800 — Faturamento
    continua R$800 (nunca 1600, nunca a soma de linhas de pagamento
    tratada como se fosse item vendido), Recebido bate exatamente,
    sem pendência nem excedente."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 11),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("800")}],
        payments=[
            {"method": PaymentMethod.PIX, "amount": Decimal("300")},
            {"method": PaymentMethod.CREDIT, "amount": Decimal("500")},
        ],
        cash_register_id=cr.id,
    )

    detail = _revenue_detail(session, actor)
    assert detail.kpi.value == Decimal("800")
    assert detail.reconciliation.revenue == Decimal("800")
    assert detail.reconciliation.received == Decimal("800")
    assert detail.reconciliation.pending_amount == Decimal("0")
    assert detail.reconciliation.overpaid_amount == Decimal("0")


def test_reconciliacao_pagamento_abaixo_do_total_vira_pendencia_sem_reduzir_faturamento(org_session):
    """Comanda de R$800 com só R$600 registrados em `Payment` (dado
    inconsistente, fora do fluxo normal de `close_order`) — Faturamento
    continua R$800 (o que foi VENDIDO não muda), Recebido reflete os
    R$600 reais, e a diferença aparece como pendência, nunca reduz o
    Faturamento."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 11),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("800")}],
        payments=[{"method": PaymentMethod.CASH, "amount": Decimal("600")}],
        cash_register_id=cr.id,
    )

    detail = _revenue_detail(session, actor)
    assert detail.kpi.value == Decimal("800")  # Faturamento não cai por causa de pagamento parcial.
    assert detail.reconciliation.revenue == Decimal("800")
    assert detail.reconciliation.received == Decimal("600")
    assert detail.reconciliation.pending_amount == Decimal("200")
    assert detail.reconciliation.overpaid_amount == Decimal("0")


def test_reconciliacao_pagamento_acima_do_total_nao_infla_faturamento(org_session):
    """Comanda de R$800 com R$850 registrados em `Payment` (troco não
    lançado / erro de lançamento) — Faturamento continua exatamente
    R$800 (NUNCA lê `Payment`, item 6 do pedido), o excedente de R$50
    aparece só como `overpaid_amount`, nunca como faturamento
    adicional."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 11),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("800")}],
        payments=[{"method": PaymentMethod.CASH, "amount": Decimal("850")}],
        cash_register_id=cr.id,
    )

    detail = _revenue_detail(session, actor)
    assert detail.kpi.value == Decimal("800")  # NUNCA 850 — Faturamento não lê Payment.
    assert detail.reconciliation.revenue == Decimal("800")
    assert detail.reconciliation.received == Decimal("850")
    assert detail.reconciliation.pending_amount == Decimal("0")
    assert detail.reconciliation.overpaid_amount == Decimal("50")


def test_reconciliacao_multiplos_servicos_na_mesma_comanda_soma_corretamente(org_session):
    """Comanda com 2 serviços (R$120 + R$680 = R$800) e pagamento exato
    — Faturamento soma os 2 itens sem duplicar por causa do JOIN com
    Payment, Recebido bate, sem pendência/excedente."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    corte_id = _service(session, org_id, name="Corte").id
    coloracao_id = _service(session, org_id, name="Coloração").id

    appt = _appointment(session, org_id, branch.id, client.id, prof.id, corte_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client.id, appt.id, closed_at=_dt(2026, 8, 10, 11),
        items=[
            {"service_id": corte_id, "professional_id": prof.id, "price": Decimal("120"), "service_name": "Corte"},
            {"service_id": coloracao_id, "professional_id": prof.id, "price": Decimal("680"), "service_name": "Coloração"},
        ],
        payments=[{"method": PaymentMethod.PIX, "amount": Decimal("800")}],
        cash_register_id=cr.id,
    )

    detail = _revenue_detail(session, actor)
    assert detail.kpi.value == Decimal("800")
    assert detail.reconciliation.revenue == Decimal("800")
    assert detail.reconciliation.received == Decimal("800")
    assert detail.reconciliation.pending_amount == Decimal("0")
    assert detail.reconciliation.overpaid_amount == Decimal("0")


def test_reconciliacao_nao_faz_netting_entre_comandas_diferentes(org_session):
    """Uma comanda paga R$50 A MAIS e outra paga R$30 A MENOS não podem
    se cancelar num único "diferença líquida" — cada uma é uma
    inconsistência distinta, calculada POR COMANDA (item explícito:
    "não misture essas granularidades")."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    client_over = _client(session, org_id, name="Cliente Pagou a Mais")
    appt_over = _appointment(session, org_id, branch.id, client_over.id, prof.id, service_id, start_at=_dt(2026, 8, 10, 9))
    _closed_order(
        session, org_id, branch.id, client_over.id, appt_over.id, closed_at=_dt(2026, 8, 10, 11),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("200")}],
        payments=[{"method": PaymentMethod.CASH, "amount": Decimal("250")}],
        cash_register_id=cr.id,
    )

    client_under = _client(session, org_id, name="Cliente Pagou a Menos")
    appt_under = _appointment(session, org_id, branch.id, client_under.id, prof.id, service_id, start_at=_dt(2026, 8, 12, 9))
    _closed_order(
        session, org_id, branch.id, client_under.id, appt_under.id, closed_at=_dt(2026, 8, 12, 11),
        items=[{"service_id": service_id, "professional_id": prof.id, "price": Decimal("200")}],
        payments=[{"method": PaymentMethod.CASH, "amount": Decimal("170")}],
        cash_register_id=cr.id,
    )

    detail = _revenue_detail(session, actor)
    assert detail.kpi.value == Decimal("400")  # 200 + 200, Faturamento nunca lê Payment.
    assert detail.reconciliation.revenue == Decimal("400")
    assert detail.reconciliation.received == Decimal("420")  # 250 + 170
    # Nunca (420 - 400) = 20 líquido: são R$30 pendentes de uma comanda
    # e R$50 excedentes de outra, cada um pertence à sua granularidade.
    assert detail.reconciliation.pending_amount == Decimal("30")
    assert detail.reconciliation.overpaid_amount == Decimal("50")


def test_reconciliacao_so_aparece_no_drill_down_de_revenue(org_session):
    """Nenhum outro KPI carrega `reconciliation` — é específico do
    drill-down de Faturamento."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("100"))

    ticket_detail = dashboard_service.get_kpi_detail(
        session, actor, key="ticket_average", branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert ticket_detail.reconciliation is None

    received_detail = dashboard_service.get_kpi_detail(
        session, actor, key="received", branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert received_detail.kpi.value == Decimal("100")
    assert received_detail.reconciliation is None


def test_desempenho_por_profissional_agrega_clientes_servicos_faturamento_e_ticket(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof1 = _professional(session, org_id, branch.id, name="Ianka")
    prof2 = _professional(session, org_id, branch.id, name="Duda")
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    for i in range(2):
        client = _client(session, org_id, name=f"Cliente Ianka {i}")
        _sale(
            session, org_id, branch.id, client.id, prof1.id, service_id, cr.id, closed_at=_dt(2026, 8, 5 + i),
            price=Decimal("150"), professional_name="Ianka",
        )
    client_duda = _client(session, org_id, name="Cliente Duda")
    _sale(
        session, org_id, branch.id, client_duda.id, prof2.id, service_id, cr.id, closed_at=_dt(2026, 8, 8),
        price=Decimal("90"), professional_name="Duda",
    )

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    by_name = {row.professional_name: row for row in overview.professionals}
    assert by_name["Ianka"].clients_served == 2
    assert by_name["Ianka"].services_count == 2
    assert by_name["Ianka"].revenue == Decimal("300")
    assert by_name["Ianka"].ticket_average == Decimal("150.00")
    assert by_name["Duda"].revenue == Decimal("90")
    assert by_name["Duda"].ticket_average == Decimal("90.00")


def test_formas_de_pagamento_agrupa_metodos_pouco_usados_em_outros(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 5), price=Decimal("100"), method=PaymentMethod.PIX)
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 6), price=Decimal("50"), method=PaymentMethod.VOUCHER)
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 7), price=Decimal("50"), method=PaymentMethod.LOYALTY_CARD)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    by_bucket = {row.bucket.value: row for row in overview.payment_methods}
    assert by_bucket["pix"].amount == Decimal("100")
    assert by_bucket["other"].amount == Decimal("100")  # voucher (50) + loyalty_card (50)
    assert "credit" not in by_bucket  # método sem nenhum uso não aparece.
    total_percent = round(sum(r.percent for r in overview.payment_methods), 2)
    assert total_percent == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------------------
# Novos x recorrentes / retenção / status
# ---------------------------------------------------------------------------


def test_novos_x_recorrentes_classifica_por_bucket_da_primeira_visita(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id

    client = _client(session, org_id, name="Cliente")
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 2), price=Decimal("50"))
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 20), price=Decimal("50"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    total_new = sum(p.new_clients for p in overview.new_vs_recurring)
    total_recurring = sum(p.recurring_clients for p in overview.new_vs_recurring)
    assert total_new == 1  # só no bucket da primeira visita (02/08).
    assert total_recurring == 1  # a visita de 20/08 é recorrente.


def test_retencao_90_dias_censura_temporal_exclui_quem_ainda_nao_completou_a_janela(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    now = datetime.now(timezone.utc)

    # Profissionais DISTINTOS por cliente — evita colisão no trigger de
    # overlap (`check_appointment_item_overlap`), que trava por
    # profissional; aqui vários clientes têm o MESMO instante de
    # "primeira visita" de propósito (todos "há 200 dias"), o que
    # colidiria se dividissem o mesmo profissional.
    prof_a = _professional(session, org_id, branch.id, name="Prof A")
    prof_b = _professional(session, org_id, branch.id, name="Prof B")
    prof_c = _professional(session, org_id, branch.id, name="Prof C")

    # Cliente A: primeira visita há 200 dias (bem mais que 90) — elegível
    # — e retornou dentro da janela de 90 dias -> RETORNOU.
    client_a = _client(session, org_id, name="A - retornou")
    first_a = now - timedelta(days=200)
    _sale(session, org_id, branch.id, client_a.id, prof_a.id, service_id, cr.id, closed_at=first_a, price=Decimal("50"))
    _sale(session, org_id, branch.id, client_a.id, prof_a.id, service_id, cr.id, closed_at=first_a + timedelta(days=30), price=Decimal("50"))

    # Cliente B: primeira visita há 200 dias, NUNCA mais voltou -> NÃO RETORNOU (elegível).
    client_b = _client(session, org_id, name="B - nao retornou")
    first_b = now - timedelta(days=200)
    _sale(session, org_id, branch.id, client_b.id, prof_b.id, service_id, cr.id, closed_at=first_b, price=Decimal("50"))

    # Cliente C: primeira visita há só 10 dias — NÃO teve tempo de
    # completar 90 dias ainda -> NÃO deve entrar no denominador (censura).
    client_c = _client(session, org_id, name="C - visita recente demais")
    first_c = now - timedelta(days=10)
    _sale(session, org_id, branch.id, client_c.id, prof_c.id, service_id, cr.id, closed_at=first_c, price=Decimal("50"))

    date_from = now - timedelta(days=365)
    date_to = now + timedelta(days=1)
    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=date_from, date_to=date_to, compare_from=None, compare_to=None,
    )
    retention = overview.retention
    assert retention.eligible_clients == 2  # A e B só — C fica de fora (censura temporal).
    assert retention.returned_clients == 1  # só A retornou.
    assert retention.rate_percent == pytest.approx(50.0)
    assert retention.note  # limitação/definição sempre documentada, nunca um número sem explicação.


def test_retencao_sem_clientes_elegiveis_nao_inventa_taxa(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert overview.retention.eligible_clients == 0
    assert overview.retention.rate_percent is None  # nunca 0% fabricado sem base.


def test_status_distribution_usa_codigos_internos_oficiais(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    service_id = _service(session, org_id).id
    _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 5, 9), status=AppointmentStatus.NO_SHOW)
    _appointment(session, org_id, branch.id, client.id, prof.id, service_id, start_at=_dt(2026, 8, 6, 9), status=AppointmentStatus.CONFIRMED)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    by_status = {row.status: row.count for row in overview.status_distribution}
    assert by_status[AppointmentStatus.NO_SHOW] == 1
    assert by_status[AppointmentStatus.CONFIRMED] == 1
    assert by_status[AppointmentStatus.PAID] == 0
    # todos os 8 status oficiais aparecem (mesmo com contagem 0) — nunca um rótulo customizado aqui.
    assert {row.status for row in overview.status_distribution} == set(AppointmentStatus)


# ---------------------------------------------------------------------------
# Drill-down por KPI
# ---------------------------------------------------------------------------


def test_drill_down_de_kpi_usa_a_mesma_definicao_do_overview(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    service_id = _service(session, org_id).id
    _sale(session, org_id, branch.id, client.id, prof.id, service_id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("120"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    detail = dashboard_service.get_kpi_detail(
        session, actor, key="revenue", branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
        compare_from=None, compare_to=None,
    )
    assert detail.kpi.value == overview.kpis.revenue.value == Decimal("120")
    assert sum((p.current_value for p in detail.series), Decimal("0")) == Decimal("120")


def test_drill_down_de_kpi_desconhecido_da_not_found(org_session):
    from nexasalon_api.core.exceptions import NotFoundError

    session, org_id = org_session
    actor = _actor(session, org_id)
    with pytest.raises(NotFoundError):
        dashboard_service.get_kpi_detail(
            session, actor, key="inexistente", branch_id=None, date_from=_dt(2026, 8, 1, 0), date_to=_dt(2026, 9, 1, 0),
            compare_from=None, compare_to=None,
        )


# ---------------------------------------------------------------------------
# Permissão (HTTP) — dashboard.view.
# ---------------------------------------------------------------------------


def test_http_sem_dashboard_view_recebe_403(org_a_actor, client_as):
    from dataclasses import replace

    restricted = replace(org_a_actor, permissions=frozenset({"agenda.view_own"}))
    client = client_as(restricted)
    resp = client.get(
        "/api/v1/dashboard/overview",
        params={"date_from": "2026-08-01T00:00:00-03:00", "date_to": "2026-09-01T00:00:00-03:00"},
    )
    assert resp.status_code == 403


def test_http_com_dashboard_view_funciona(org_a_actor, client_as):
    client = client_as(org_a_actor)  # fixture já concede TODAS as permissions (Owner de teste).
    resp = client.get(
        "/api/v1/dashboard/overview",
        params={"date_from": "2026-08-01T00:00:00-03:00", "date_to": "2026-09-01T00:00:00-03:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kpis"]["revenue"]["value"] == "0"
    assert body["granularity"] == "day"
