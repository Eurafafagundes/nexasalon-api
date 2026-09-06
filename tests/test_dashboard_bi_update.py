"""Testes do redesign BI do Dashboard (`services/dashboard.py`) — só a
parte NOVA desta etapa: `net_revenue`/`orders_count`/`repeat_rate` (3
cards novos), `financial_summary` (Resumo Financeiro, incl. permissão
de Comissões), `get_services`/`get_service_detail` e
`get_professionals_detail`/`get_professional_detail` ("Ver todos" +
drill-down), e sparkline por card. As regras JÁ cobertas por
`test_dashboard.py` (isolamento, filtro de unidade, Faturamento/Ticket
médio/Clientes/Top serviços/Ranking de profissionais/Formas de
pagamento/Bruto-Líquido com taxa conhecida ou desconhecida) NUNCA são
reescritas aqui — só reaproveitadas indiretamente pelas mesmas funções.

Mesmo padrão de `test_dashboard.py`: direto no service layer via
`SessionLocal`, Order/OrderItem/Payment construídos via ORM (não
`services/orders.py::close_order`) pra controlar datas HISTÓRICAS —
inclusive os snapshots de comissão (`commission_*_snapshot`), setados
diretamente no `OrderItem` pra provar que o Dashboard nunca recalcula
comissão pela regra atual de `ProfessionalService`."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, text

from nexasalon_api.core.db import engine as db_engine

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.cash_register import CashMovement, CashRegister
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CashMovementType,
    CashRegisterStatus,
    CommissionStatus,
    CommissionType,
    OrderStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.order import Order, OrderItem, Payment
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.services import dashboard as dashboard_service

_TZ = timezone(timedelta(hours=-3))
_order_number_counter = 100_000


def _next_order_number() -> int:
    global _order_number_counter
    _order_number_counter += 1
    return _order_number_counter


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org dashboard BI", slug=f"org-dash-bi-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=frozenset({"dashboard.view"})) -> ActorContext:
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


def _service(session, org_id, name="Serviço", price=Decimal("100.00")) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=60, default_price=price)
    session.add(s)
    session.flush()
    return s


def _link(session, professional_id, service_id, **overrides) -> ProfessionalService:
    ps = ProfessionalService(professional_id=professional_id, service_id=service_id, **overrides)
    session.add(ps)
    session.flush()
    return ps


def _cash_register(session, org_id, branch_id, user_id) -> CashRegister:
    cr = CashRegister(
        organization_id=org_id, branch_id=branch_id, opened_by=user_id, opened_by_name="Caixa Teste",
        initial_amount=Decimal("0"), status=CashRegisterStatus.OPEN,
    )
    session.add(cr)
    session.flush()
    return cr


def _dt(y, m, d, h=10):
    return datetime(y, m, d, h, 0, tzinfo=_TZ)


def _appointment(session, org_id, branch_id, client_id, professional_id, service_id, *, start_at, price=Decimal("100.00")) -> Appointment:
    appt = Appointment(organization_id=org_id, branch_id=branch_id, client_id=client_id, status=AppointmentStatus.PAID)
    session.add(appt)
    session.flush()
    item = AppointmentItem(
        organization_id=org_id, appointment_id=appt.id, service_id=service_id, professional_id=professional_id,
        start_at=start_at, end_at=start_at + timedelta(minutes=60), duration_minutes=60, price=price,
    )
    session.add(item)
    session.flush()
    return appt


def _sale(
    session, org_id, branch_id, client_id, professional_id, service_id, cash_register_id, *,
    closed_at, price=Decimal("100.00"), method=PaymentMethod.PIX,
    service_name="Serviço", professional_name="Profissional",
    commission_type: CommissionType | None = None, commission_value: Decimal | None = None,
    commission_amount: Decimal | None = None, commission_status: CommissionStatus | None = None,
) -> Order:
    """Mesmo atalho de `test_dashboard.py::_sale`, com o acréscimo dos
    campos de snapshot de comissão (Etapa C2) — setados diretamente
    (nunca via `resolve_commission`) pra ter controle total do cenário
    histórico em cada teste."""
    appt = _appointment(session, org_id, branch_id, client_id, professional_id, service_id, start_at=closed_at - timedelta(hours=1), price=price)
    order = Order(
        organization_id=org_id, order_number=_next_order_number(), appointment_id=appt.id,
        branch_id=branch_id, client_id=client_id, status=OrderStatus.CLOSED, closed_at=closed_at,
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            organization_id=org_id, order_id=order.id, service_id=service_id, professional_id=professional_id,
            duration_minutes=60, price=price, service_name=service_name, professional_name=professional_name,
            commission_type_snapshot=commission_type, commission_value_snapshot=commission_value,
            commission_amount_snapshot=commission_amount, commission_status=commission_status,
        )
    )
    session.add(
        Payment(
            organization_id=org_id, order_id=order.id, cash_register_id=cash_register_id,
            method=method, amount=price, created_by_name="Teste",
        )
    )
    session.flush()
    return order


# ---------------------------------------------------------------------
# Atendimentos (orders_count) — comandas fechadas, NUNCA Appointment
# ---------------------------------------------------------------------


def test_atendimentos_conta_comandas_fechadas_nao_agendamentos(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)

    # Um agendamento SEM comanda (nunca vendeu) não conta pra "Atendimentos".
    _appointment(session, org_id, branch.id, client.id, prof.id, svc.id, start_at=_dt(2026, 8, 10))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 11))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 12))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.orders_count.value == Decimal("2")
    # "Agendamentos" (métrica antiga, mantida no contrato) continua
    # contando os 3 (2 vendidos + 1 que nunca virou venda) — prova de
    # que as duas métricas realmente medem coisas diferentes.
    assert overview.kpis.appointments_count.value == Decimal("3")


def test_zero_atendimentos_nao_quebra_ticket_medio_nem_taxa_de_retorno(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert overview.kpis.orders_count.value == Decimal("0")
    assert overview.kpis.ticket_average.value == Decimal("0")
    assert overview.kpis.repeat_rate.value == Decimal("0")


# ---------------------------------------------------------------------
# Taxa de Retorno (repeat_rate) — NOVA definição, "olhando pra trás"
# ---------------------------------------------------------------------


def test_taxa_de_retorno_cliente_que_ja_visitou_antes_do_periodo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    returning_client = _client(session, org_id, "Cliente Recorrente")
    new_client = _client(session, org_id, "Cliente Novo")

    # Visita ANTERIOR ao período (fora do filtro) — torna o cliente "recorrente".
    _sale(session, org_id, branch.id, returning_client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 7, 1))
    # As duas visitas DENTRO do período analisado.
    _sale(session, org_id, branch.id, returning_client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10))
    _sale(session, org_id, branch.id, new_client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 11))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    # 2 clientes atendidos no período, 1 já era cliente antes -> 50%.
    assert overview.kpis.repeat_rate.value == Decimal("50.00")
    assert overview.kpis.repeat_rate.kind.value == "rate"


def test_taxa_de_retorno_compara_em_pontos_percentuais(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    client = _client(session, org_id)

    # Período comparativo (julho): cliente novo, 0% de retorno.
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 7, 15))
    # Período atual (agosto): mesmo cliente retorna -> 100% de retorno.
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 15))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=_dt(2026, 7, 1), compare_to=_dt(2026, 8, 1),
    )
    assert overview.kpis.repeat_rate.value == Decimal("100.00")
    assert overview.kpis.repeat_rate.comparison_value == Decimal("0.00")
    assert overview.kpis.repeat_rate.delta_points == pytest.approx(100.0)
    assert overview.kpis.repeat_rate.delta_percent is None  # rate nunca usa variação percentual.


# ---------------------------------------------------------------------
# Item de performance ("quick win 2") — `_first_visit_by_client` não
# pode mais ser recalculado várias vezes dentro da MESMA chamada de
# `get_overview`/`get_kpi_detail` (antes desta rodada, rodava de 5 a 7
# vezes — uma por card/sparkline que depende de "primeira visita").
# ---------------------------------------------------------------------


class _FirstVisitQueryCounter:
    """Conta quantas vezes a query de `_first_visit_by_client`
    (`SELECT client_id, min(closed_at) ... GROUP BY client_id`) chega a
    executar de verdade no banco — via `before_cursor_execute`, não uma
    contagem de CHAMADAS à função (que continuaria em 5-7: o cache é
    verificado DENTRO da própria função, então só contar invocações não
    provaria nada; o que importa é quantas vezes o SQL de fato roda)."""

    def __init__(self):
        self.count = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        if "min(orders.closed_at)" in statement.lower():
            self.count += 1


def test_first_visit_by_client_nao_e_recalculado_dentro_do_mesmo_overview(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    returning_client = _client(session, org_id, "Cliente Recorrente")
    new_client = _client(session, org_id, "Cliente Novo")

    _sale(session, org_id, branch.id, returning_client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 7, 1))
    _sale(session, org_id, branch.id, returning_client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10))
    _sale(session, org_id, branch.id, new_client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 11))

    counter = _FirstVisitQueryCounter()
    event.listen(db_engine, "before_cursor_execute", counter)
    try:
        overview = dashboard_service.get_overview(
            session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
            compare_from=None, compare_to=None,
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", counter)

    # Sem período comparativo: o conjunto de clientes do período ATUAL é
    # usado por até 5 caminhos (new_clients, repeat_rate + os dois
    # sparklines, new_vs_recurring) — antes desta rodada, 5 execuções da
    # MESMA query; com o cache por request, deve ser exatamente 1.
    assert counter.count == 1
    # Resultado continua correto (equivalência de comportamento) —
    # mesma asserção do teste de repeat_rate acima, valor inalterado.
    assert overview.kpis.repeat_rate.value == Decimal("50.00")
    assert overview.kpis.new_clients.value == Decimal("1")


def test_first_visit_by_client_com_periodo_comparativo_executa_no_maximo_uma_vez_por_periodo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    client = _client(session, org_id)

    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 7, 15))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 15))

    counter = _FirstVisitQueryCounter()
    event.listen(db_engine, "before_cursor_execute", counter)
    try:
        dashboard_service.get_overview(
            session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
            compare_from=_dt(2026, 7, 1), compare_to=_dt(2026, 8, 1),
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", counter)

    # Período atual e comparativo têm conjuntos de client_ids DIFERENTES
    # (chaves de cache distintas) — 1 execução por período, nunca mais
    # que isso mesmo com os múltiplos cards que precisam de cada um.
    assert counter.count == 2


def test_first_visit_by_client_cache_e_por_chamada_nunca_global_entre_requests(org_session):
    """Explícito por pedido: cache só dentro da MESMA execução/request —
    duas chamadas SEPARADAS a `get_overview` (dois "requests" simulados,
    cada uma cria seu próprio `first_visit_cache` novo) precisam voltar
    a consultar o banco, provando que não existe cache global/persistido
    entre chamadas."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    client = _client(session, org_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10))

    counter = _FirstVisitQueryCounter()
    event.listen(db_engine, "before_cursor_execute", counter)
    try:
        dashboard_service.get_overview(
            session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
            compare_from=None, compare_to=None,
        )
        first_request_count = counter.count
        dashboard_service.get_overview(
            session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
            compare_from=None, compare_to=None,
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", counter)

    assert first_request_count == 1
    # A SEGUNDA chamada (novo "request") volta a consultar — não reaproveitou nada da primeira.
    assert counter.count == 2


# ---------------------------------------------------------------------
# Faturamento Líquido (net_revenue) — mesmo `RevenueFeeSummary`, agora
# também como KpiValue com comparação.
# ---------------------------------------------------------------------


def test_net_revenue_kpi_bate_com_revenue_fee_summary(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id, price=Decimal("300.00"))
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("300.00"), method=PaymentMethod.PIX)

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    # Pix nunca tem taxa (NOT_APPLICABLE) -> líquido = bruto.
    assert overview.kpis.net_revenue.value == overview.revenue_fee_summary.known_net_revenue
    assert overview.kpis.net_revenue.value == Decimal("300.00")
    assert overview.kpis.net_revenue.kind.value == "currency"


def test_net_revenue_com_comparacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id, price=Decimal("200.00"))
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 7, 10), price=Decimal("100.00"))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("200.00"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=_dt(2026, 7, 1), compare_to=_dt(2026, 8, 1),
    )
    assert overview.kpis.net_revenue.value == Decimal("200.00")
    assert overview.kpis.net_revenue.comparison_value == Decimal("100.00")
    assert overview.kpis.net_revenue.has_comparison is True


# ---------------------------------------------------------------------
# Sparkline — presente nos 6 cards principais, período ATUAL apenas.
# ---------------------------------------------------------------------


def test_sparkline_presente_nos_6_cards_principais(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 5))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 20))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    for kpi in (
        overview.kpis.revenue, overview.kpis.net_revenue, overview.kpis.orders_count,
        overview.kpis.new_clients, overview.kpis.ticket_average, overview.kpis.repeat_rate,
    ):
        assert kpi.sparkline is not None
        assert len(kpi.sparkline) > 0
        assert sum(kpi.sparkline) >= 0  # nunca None/erro dentro da lista.


# ---------------------------------------------------------------------
# Resumo Financeiro (financial_summary)
# ---------------------------------------------------------------------


def test_resumo_financeiro_despesas_usa_mesma_definicao_do_extrato(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    session.add(
        CashMovement(
            organization_id=org_id, cash_register_id=cr.id, type=CashMovementType.WITHDRAWAL,
            amount=Decimal("150.00"), description="Compra de material", created_by=actor.user_id, created_by_name="Teste",
        )
    )
    session.add(
        CashMovement(
            organization_id=org_id, cash_register_id=cr.id, type=CashMovementType.SUPPLY,
            amount=Decimal("50.00"), description="Reforço de troco", created_by=actor.user_id, created_by_name="Teste",
        )
    )
    session.flush()

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    # Só WITHDRAWAL (sangria) conta como despesa — SUPPLY (suprimento) nunca.
    assert overview.financial_summary.expenses == Decimal("150.00")


def test_resumo_financeiro_sem_permissao_de_comissoes_nunca_mostra_valor(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions={"dashboard.view"})  # sem commissions.*
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(
        session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10),
        commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"),
        commission_amount=Decimal("20.00"), commission_status=CommissionStatus.CALCULATED,
    )

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    # NUNCA R$0 fingido — None explícito, ator não tem escopo de Comissões.
    assert overview.financial_summary.commissions_calculated is None


def test_resumo_financeiro_com_permissao_de_comissoes_mostra_total_do_snapshot(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions={"dashboard.view", "commissions.view_all"})
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(
        session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("300.00"),
        commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"),
        commission_amount=Decimal("60.00"), commission_status=CommissionStatus.CALCULATED,
    )
    # Regra ATUAL do vínculo é diferente do snapshot acima — nunca deve
    # ser usada (prova de que o Dashboard também lê o snapshot congelado,
    # nunca recalcula pela regra viva de ProfessionalService).
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("50.00"))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert overview.financial_summary.commissions_calculated == Decimal("60.00")


def test_resumo_financeiro_not_configured_e_historico_nunca_viram_comissao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions={"dashboard.view", "commissions.view_all"})
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    # not_configured: sem regra na época do fechamento.
    _sale(
        session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10),
        commission_status=CommissionStatus.NOT_CONFIGURED,
    )
    # histórico: fechado antes da migration 0036 existir (snapshot NULL).
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 11))

    overview = dashboard_service.get_overview(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert overview.financial_summary.commissions_calculated == Decimal("0")


# ---------------------------------------------------------------------
# "Ver todos" — Análise de Serviços
# ---------------------------------------------------------------------


def test_get_services_lista_completa_com_comparacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id, name="Manutenção", price=Decimal("230.00"))
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 7, 10), price=Decimal("200.00"))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("230.00"))
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 20), price=Decimal("230.00"))

    result = dashboard_service.get_services(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=_dt(2026, 7, 1), compare_to=_dt(2026, 8, 1),
    )
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.quantity == 2
    assert row.revenue.value == Decimal("460.00")
    assert row.revenue.comparison_value == Decimal("200.00")
    assert row.ticket_average == Decimal("230.00")


def test_get_service_detail_mostra_itens_exatos(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    client = _client(session, org_id, name="Maria")
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção")
    outro_svc = _service(session, org_id, name="Corte")
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10), service_name="Manutenção", professional_name="Ianka")
    _sale(session, org_id, branch.id, client.id, prof.id, outro_svc.id, cr.id, closed_at=_dt(2026, 8, 11), service_name="Corte", professional_name="Ianka")

    detail = dashboard_service.get_service_detail(
        session, actor, svc.id, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
    )
    assert detail.service_name == "Manutenção"
    assert len(detail.items) == 1
    assert detail.items[0].client_name == "Maria"
    assert detail.items[0].professional_name == "Ianka"


def test_get_service_detail_servico_inexistente_404(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    with pytest.raises(NotFoundError):
        dashboard_service.get_service_detail(
            session, actor, uuid.uuid4(), branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        )


# ---------------------------------------------------------------------
# "Ver todos" — Desempenho dos Profissionais
# ---------------------------------------------------------------------


def test_get_professionals_detail_inclui_comissao_calculada_do_snapshot(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions={"dashboard.view", "commissions.view_all"})
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, price=Decimal("300.00"))
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(
        session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("300.00"),
        professional_name="Ianka",
        commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"),
        commission_amount=Decimal("60.00"), commission_status=CommissionStatus.CALCULATED,
    )

    result = dashboard_service.get_professionals_detail(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert result.commissions_available is True
    assert len(result.rows) == 1
    assert result.rows[0].professional_name == "Ianka"
    assert result.rows[0].commission_calculated == Decimal("60.00")


def test_get_professionals_detail_sem_permissao_nunca_mostra_comissao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions={"dashboard.view"})
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id)
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(session, org_id, branch.id, client.id, prof.id, svc.id, cr.id, closed_at=_dt(2026, 8, 10))

    result = dashboard_service.get_professionals_detail(
        session, actor, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert result.commissions_available is False
    assert result.rows[0].commission_calculated is None


def test_get_professional_detail_traz_producao_atendimentos_e_top_servicos(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, permissions={"dashboard.view", "commissions.view_all"})
    branch = _branch(session, org_id)
    client = _client(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    manutencao = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    corte = _service(session, org_id, name="Corte", price=Decimal("100.00"))
    cr = _cash_register(session, org_id, branch.id, actor.user_id)
    _sale(
        session, org_id, branch.id, client.id, prof.id, manutencao.id, cr.id, closed_at=_dt(2026, 8, 10), price=Decimal("300.00"),
        service_name="Manutenção", professional_name="Ianka",
        commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"),
        commission_amount=Decimal("60.00"), commission_status=CommissionStatus.CALCULATED,
    )
    _sale(
        session, org_id, branch.id, client.id, prof.id, corte.id, cr.id, closed_at=_dt(2026, 8, 11), price=Decimal("100.00"),
        service_name="Corte", professional_name="Ianka",
        commission_type=CommissionType.FIXED, commission_value=Decimal("15.00"),
        commission_amount=Decimal("15.00"), commission_status=CommissionStatus.CALCULATED,
    )

    detail = dashboard_service.get_professional_detail(
        session, actor, prof.id, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
    )
    assert detail.professional_name == "Ianka"
    assert detail.revenue == Decimal("400.00")
    assert detail.services_count == 2
    assert detail.ticket_average == Decimal("200.00")
    assert detail.commission_calculated == Decimal("75.00")
    assert len(detail.items) == 2
    assert {s.service_name for s in detail.top_services} == {"Manutenção", "Corte"}


def test_get_professional_detail_profissional_inexistente_404(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    with pytest.raises(NotFoundError):
        dashboard_service.get_professional_detail(
            session, actor, uuid.uuid4(), branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        )


# ---------------------------------------------------------------------
# Isolamento entre organizações (novos endpoints)
# ---------------------------------------------------------------------


def test_tenant_isolation_ver_todos_servicos_e_profissionais(org_session):
    session, org_a = org_session
    actor_a = _actor(session, org_a)
    branch_a = _branch(session, org_a)
    client_a = _client(session, org_a)
    prof_a = _professional(session, org_a, branch_a.id)
    svc_a = _service(session, org_a)
    cr_a = _cash_register(session, org_a, branch_a.id, actor_a.user_id)
    _sale(session, org_a, branch_a.id, client_a.id, prof_a.id, svc_a.id, cr_a.id, closed_at=_dt(2026, 8, 10))

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_b)

    services_b = dashboard_service.get_services(
        session, actor_b, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert services_b.rows == []

    professionals_b = dashboard_service.get_professionals_detail(
        session, actor_b, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        compare_from=None, compare_to=None,
    )
    assert professionals_b.rows == []

    with pytest.raises(NotFoundError):
        dashboard_service.get_service_detail(
            session, actor_b, svc_a.id, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        )
    with pytest.raises(NotFoundError):
        dashboard_service.get_professional_detail(
            session, actor_b, prof_a.id, branch_id=None, date_from=_dt(2026, 8, 1), date_to=_dt(2026, 9, 1),
        )
