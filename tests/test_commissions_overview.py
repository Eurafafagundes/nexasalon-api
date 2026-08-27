"""Testes da Etapa C3 — leitura (`services/commissions.py::get_overview`/
`get_detail`, `GET /api/v1/commissions/overview`/`/{id}/detail`).

Cobre: agregação por profissional (percentual/fixo/vários serviços);
`not_configured` nunca entra em "Comissão calculada" (conta e valor
separados); `OrderItem` histórico (`commission_status IS NULL`) nunca
entra em Produção/Comissão/"sem configuração" — só no contador próprio
de histórico; mudança atual de `ProfessionalService` nunca altera um
relatório de período já fechado (usa sempre o snapshot); filtro de
período (`Order.closed_at`, competência); isolamento entre
organizações; permissão HTTP (`commissions.view_all`/`view_own`).

Mesmo padrão de `test_commissions.py` (C2) — direto no service layer
via `SessionLocal`, helpers redefinidos localmente; os dois testes de
permissão HTTP (11/12) usam `client_as`/`org_a_actor` de `conftest.py`,
mesmo padrão de `test_dashboard.py`."""
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus, CommissionType, PaymentMethod
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import OrderClose, PaymentCreate
from nexasalon_api.services import appointments, cash_register, commissions, orders

_ALL_AGENDA_PERMS = frozenset(
    {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel"}
)
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 2026-08-13 é quinta.


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org comissões overview", slug=f"org-comissoes-ov-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, professional_id=None, permissions=_ALL_AGENDA_PERMS | {"commissions.view_all", "commissions.view_own"}) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions), professional_id=professional_id,
    )


def _branch(session, org_id, name="Unidade") -> Branch:
    b = Branch(organization_id=org_id, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _professional(session, org_id, branch_id, name="Profissional") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Serviço", duration=60, price=Decimal("100.00")) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=duration, default_price=price)
    session.add(s)
    session.flush()
    return s


def _link(session, professional_id, service_id, **overrides) -> ProfessionalService:
    ps = ProfessionalService(professional_id=professional_id, service_id=service_id, **overrides)
    session.add(ps)
    session.flush()
    return ps


def _working_hours(session, org_id, professional_id, weekday, start, end):
    session.add(
        WorkingHours(organization_id=org_id, professional_id=professional_id, weekday=weekday, start_time=start, end_time=end)
    )
    session.flush()


def _client(session, org_id, name="Cliente") -> Client:
    c = Client(organization_id=org_id, name=name)
    session.add(c)
    session.flush()
    return c


def _dt(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=_TZ)


def _open_register(session, actor, initial_amount=Decimal("0")):
    branch_id = _branch(session, actor.organization_id).id
    return cash_register.open_register(session, actor, branch_id, initial_amount, None)


def _appointment_with_items(session, org_id, actor, branch, client, items):
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(**it) for it in items],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt


def _close_with_pix(session, actor, order):
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    return orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )


def _sale(session, org_id, actor, branch, prof, svc, client, *, day, hour=9):
    """Um agendamento -> comanda -> fechamento com PIX, pronto — o
    caminho comum da maioria dos testes deste arquivo (só o preço/
    profissional/serviço variam)."""
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(day, hour)}],
    )
    order = orders.create_order(session, actor, appt.id)
    return _close_with_pix(session, actor, order)


# ---------------------------------------------------------------------
# 1/2/3/4 — agregação por profissional (percentual, fixo, múltiplos)
# ---------------------------------------------------------------------


def test_overview_percentual_calculado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)

    overview = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview.production == Decimal("300.00")
    assert overview.known_commission_total == Decimal("60.00")
    assert overview.unconfigured_count == 0
    assert overview.historical_count == 0
    assert len(overview.professionals) == 1
    assert overview.professionals[0].professional_name == "Ianka"
    assert overview.professionals[0].production == Decimal("300.00")
    assert overview.professionals[0].commission_total == Decimal("60.00")


def test_overview_fixo_calculado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Mega Hair", price=Decimal("4500.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.FIXED, commission_value=Decimal("100.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)

    overview = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview.known_commission_total == Decimal("100.00")


def test_overview_dois_profissionais(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    ianka = _professional(session, org_id, branch.id, name="Ianka")
    duda = _professional(session, org_id, branch.id, name="Duda")
    _link(session, ianka.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, duda.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("25.00"))
    _working_hours(session, org_id, ianka.id, _THURSDAY, time(9, 0), time(20, 0))
    _working_hours(session, org_id, duda.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, ianka, svc, client, day=13, hour=9)
    _sale(session, org_id, actor, branch, duda, svc, client, day=13, hour=11)

    overview = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview.production == Decimal("600.00")
    assert overview.known_commission_total == Decimal("135.00")  # 60 + 75
    by_name = {p.professional_name: p for p in overview.professionals}
    assert by_name["Ianka"].commission_total == Decimal("60.00")
    assert by_name["Duda"].commission_total == Decimal("75.00")


def test_overview_varios_servicos_do_mesmo_profissional_somam(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    manutencao = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    corte = _service(session, org_id, name="Corte", price=Decimal("100.00"))
    _link(session, prof.id, manutencao.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, prof.id, corte.id, commission_type=CommissionType.FIXED, commission_value=Decimal("30.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [
            {"professional_id": prof.id, "service_id": manutencao.id, "start_at": _dt(13, 9)},
            {"professional_id": prof.id, "service_id": corte.id, "start_at": _dt(13, 10)},
        ],
    )
    order = orders.create_order(session, actor, appt.id)
    _close_with_pix(session, actor, order)

    overview = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert len(overview.professionals) == 1
    assert overview.professionals[0].production == Decimal("400.00")
    assert overview.professionals[0].commission_total == Decimal("90.00")  # 60 + 30


# ---------------------------------------------------------------------
# 5 — not_configured nunca vira R$0 nem entra em "Comissão calculada"
# ---------------------------------------------------------------------


def test_overview_not_configured_separado_de_calculada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Corte", price=Decimal("50.00"))
    _link(session, prof.id, svc.id)  # sem comissão configurada.
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)

    overview = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview.production == Decimal("50.00")  # venda real, conta em produção.
    assert overview.known_commission_total == Decimal("0")  # nunca inventa comissão.
    assert overview.unconfigured_count == 1
    assert overview.unconfigured_amount == Decimal("50.00")
    assert overview.professionals[0].unconfigured_count == 1


# ---------------------------------------------------------------------
# 6 — snapshot NULL histórico nunca entra em produção/comissão/sem-config
# ---------------------------------------------------------------------


def test_overview_historico_nao_entra_em_producao_nem_comissao(org_session):
    """Simula `OrderItem` fechado ANTES da migration 0036 (construído
    direto via ORM, sem passar por `close_order`) — nunca conta como
    produção, comissão OU "sem configuração"; só no contador próprio
    de histórico."""
    from nexasalon_api.models.enums import OrderStatus
    from nexasalon_api.models.order import Order, OrderItem

    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(13, 9)}],
    )
    order = Order(
        organization_id=org_id, order_number=1, appointment_id=appt.id, branch_id=branch.id,
        client_id=client.id, status=OrderStatus.CLOSED, closed_at=_dt(13, 11),
    )
    session.add(order)
    session.flush()
    session.add(
        OrderItem(
            organization_id=org_id, order_id=order.id, service_id=svc.id, professional_id=prof.id,
            duration_minutes=60, price=Decimal("300.00"), service_name="Manutenção", professional_name="Ianka",
        )
    )
    session.flush()

    overview = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview.production == Decimal("0")
    assert overview.known_commission_total == Decimal("0")
    assert overview.unconfigured_count == 0
    assert overview.historical_count == 1
    assert overview.professionals[0].historical_count == 1
    assert overview.professionals[0].production == Decimal("0")


# ---------------------------------------------------------------------
# 7 — mudança atual da regra nunca altera relatório histórico
# ---------------------------------------------------------------------


def test_overview_mudanca_de_regra_nao_altera_relatorio_ja_fechado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    link = _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)

    before = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert before.known_commission_total == Decimal("60.00")

    link.commission_value = Decimal("25.00")
    session.flush()

    after = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert after.known_commission_total == Decimal("60.00")  # continua usando o snapshot congelado.


# ---------------------------------------------------------------------
# 8/9 — competência = Order.closed_at
# ---------------------------------------------------------------------


def test_overview_filtro_por_periodo_competencia_e_data_de_fechamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)  # fecha em 2026-08-13.

    agosto = commissions.get_overview(session, actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    setembro = commissions.get_overview(
        session, actor,
        date_from=datetime(2026, 9, 1, 0, 0, tzinfo=_TZ), date_to=datetime(2026, 9, 30, 23, 59, tzinfo=_TZ),
    )
    assert agosto.production == Decimal("300.00")
    assert setembro.production == Decimal("0")  # item fora do período não entra.


# ---------------------------------------------------------------------
# 10 — isolamento entre organizações
# ---------------------------------------------------------------------


def test_overview_isolamento_entre_organizacoes(org_session):
    session, org_a = org_session
    actor_a = _actor(session, org_a)
    branch_a = _branch(session, org_a)
    cash_register.open_register(session, actor_a, branch_a.id, Decimal("0"), None)
    prof_a = _professional(session, org_a, branch_a.id, name="Ianka")
    svc_a = _service(session, org_a, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof_a.id, svc_a.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_a, prof_a.id, _THURSDAY, time(9, 0), time(20, 0))
    client_a = _client(session, org_a)
    _sale(session, org_a, actor_a, branch_a, prof_a, svc_a, client_a, day=13)

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_b)

    overview_b = commissions.get_overview(session, actor_b, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview_b.production == Decimal("0")
    assert overview_b.professionals == []

    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
    overview_a = commissions.get_overview(session, actor_a, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert overview_a.production == Decimal("300.00")


# ---------------------------------------------------------------------
# view_own — estruturalmente preparado (escopo forçado, nunca vaza)
# ---------------------------------------------------------------------


def test_view_own_nunca_ve_comissao_de_outro_profissional(org_session):
    session, org_id = org_session
    branch = _branch(session, org_id)
    owner = _actor(session, org_id)
    cash_register.open_register(session, owner, branch.id, Decimal("0"), None)
    ianka = _professional(session, org_id, branch.id, name="Ianka")
    duda = _professional(session, org_id, branch.id, name="Duda")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    _link(session, ianka.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, duda.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("30.00"))
    _working_hours(session, org_id, ianka.id, _THURSDAY, time(9, 0), time(20, 0))
    _working_hours(session, org_id, duda.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, owner, branch, ianka, svc, client, day=13, hour=9)
    _sale(session, org_id, owner, branch, duda, svc, client, day=13, hour=11)

    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})

    overview = commissions.get_overview(session, ianka_actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))
    assert len(overview.professionals) == 1
    assert overview.professionals[0].professional_id == ianka.id
    assert overview.known_commission_total == Decimal("60.00")  # nunca inclui a comissão da Duda.

    # Pedir o professional_id de outra pessoa explicitamente também não
    # vaza — o filtro é IGNORADO e forçado pro próprio profissional.
    overview_tentando_outro = commissions.get_overview(
        session, ianka_actor, date_from=_dt(1, 0), date_to=_dt(31, 23, 59), professional_id=duda.id,
    )
    assert overview_tentando_outro.professionals[0].professional_id == ianka.id

    with pytest.raises(NotFoundError):
        commissions.get_detail(session, ianka_actor, duda.id, date_from=_dt(1, 0), date_to=_dt(31, 23, 59))


# ---------------------------------------------------------------------
# 11/12 — permissão HTTP
# ---------------------------------------------------------------------


def test_http_sem_permissao_de_comissao_recebe_403(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"agenda.view_own"}))
    client = client_as(restricted)
    resp = client.get(
        "/api/v1/commissions/overview",
        params={"date_from": "2026-08-01T00:00:00-03:00", "date_to": "2026-08-31T23:59:59-03:00"},
    )
    assert resp.status_code == 403


def test_http_com_view_all_acessa(org_a_actor, client_as):
    client = client_as(org_a_actor)  # fixture concede TODAS as permissions (Owner de teste).
    resp = client.get(
        "/api/v1/commissions/overview",
        params={"date_from": "2026-08-01T00:00:00-03:00", "date_to": "2026-08-31T23:59:59-03:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["production"] == "0"
    assert body["professionals"] == []
