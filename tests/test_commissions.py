"""Testes da Etapa C2 — Snapshot e cálculo de comissão
(`services/commissions.py::resolve_commission`, chamado a partir de
`services/orders.py::close_order`/`close_orders_consolidated`).

Cobre: percentual; valor fixo; regra ausente (`NOT_CONFIGURED`, nunca
comissão 0 inventada); profissionais diferentes com comissão diferente
no MESMO serviço; serviços diferentes do MESMO profissional;
arredondamento com fração de centavo; fechamento simples e
consolidado; alteração da regra depois do fechamento NUNCA recalcula
`OrderItem` já fechado (venda antiga preserva o snapshot, venda nova
usa a regra atual); `OrderItem` histórico (fechado sem passar pelo
snapshot, simulando pré-migration) permanece com os 4 campos `NULL`
pra sempre; isolamento entre organizações.

Mesmo padrão de `test_orders.py`/`test_orders_related_consolidated.py`
— direto no service layer via `SessionLocal`, helpers redefinidos
localmente (nenhum import cruzado entre arquivos de teste)."""
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CommissionStatus,
    CommissionType,
    OrderStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.order import Order, OrderItem
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import professional_service_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    ConsolidatedOrderClose,
    OrderClose,
    PaymentCreate,
)
from nexasalon_api.services import appointments, cash_register, orders

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
        session.add(Organization(id=org_id, name="Org comissões", slug=f"org-comissoes-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_ALL_AGENDA_PERMS) -> ActorContext:
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


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _open_register(session, actor, initial_amount=Decimal("0")):
    branch_id = _branch(session, actor.organization_id).id
    return cash_register.open_register(session, actor, branch_id, initial_amount, None)


def _appointment_with_items(session, org_id, actor, branch, client, items):
    """`items`: lista de dicts `{professional_id, service_id, start_at}`
    — já FINISHED, pronto pra abrir comanda (mesmo padrão de
    `test_orders.py::_finished_appointment_with_two_services`, mas
    genérico sobre profissional/serviço/horário por item)."""
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


# ---------------------------------------------------------------------
# 1/2/3 — percentual, fixo, regra ausente
# ---------------------------------------------------------------------


def test_comissao_percentual(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(9, 0)}],
    )
    order = orders.create_order(session, actor, appt.id)

    closed = _close_with_pix(session, actor, order)
    item = closed.items[0]
    assert item.commission_status == CommissionStatus.CALCULATED
    assert item.commission_type_snapshot == CommissionType.PERCENTAGE
    assert item.commission_value_snapshot == Decimal("20.00")
    assert item.commission_amount_snapshot == Decimal("60.00")  # 300 * 20%


def test_comissao_valor_fixo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Mega Hair", price=Decimal("4500.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.FIXED, commission_value=Decimal("100.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(9, 0)}],
    )
    order = orders.create_order(session, actor, appt.id)

    closed = _close_with_pix(session, actor, order)
    item = closed.items[0]
    assert item.commission_status == CommissionStatus.CALCULATED
    assert item.commission_type_snapshot == CommissionType.FIXED
    assert item.commission_amount_snapshot == Decimal("100.00")  # fixo, independe do preço do serviço.


def test_comissao_sem_regra_configurada_nunca_vira_zero(org_session):
    """Vínculo existe (profissional executa o serviço) mas SEM comissão
    configurada — `commission_status=NOT_CONFIGURED`, os 3 snapshots
    ficam `None`, nunca `0`."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Corte", price=Decimal("50.00"))
    _link(session, prof.id, svc.id)  # sem commission_type/commission_value.
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(9, 0)}],
    )
    order = orders.create_order(session, actor, appt.id)

    closed = _close_with_pix(session, actor, order)
    item = closed.items[0]
    assert item.commission_status == CommissionStatus.NOT_CONFIGURED
    assert item.commission_type_snapshot is None
    assert item.commission_value_snapshot is None
    assert item.commission_amount_snapshot is None


# ---------------------------------------------------------------------
# 4/5 — granularidade por profissional/serviço (N2 aplicado à comissão)
# ---------------------------------------------------------------------


def test_profissionais_diferentes_tem_comissao_diferente_no_mesmo_servico(org_session):
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
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [
            {"professional_id": ianka.id, "service_id": svc.id, "start_at": _dt(9, 0)},
            {"professional_id": duda.id, "service_id": svc.id, "start_at": _dt(11, 0)},
        ],
    )
    order = orders.create_order(session, actor, appt.id)

    closed = _close_with_pix(session, actor, order)
    by_prof = {i.professional_id: i for i in closed.items}
    assert by_prof[ianka.id].commission_amount_snapshot == Decimal("60.00")  # 300 * 20%
    assert by_prof[duda.id].commission_amount_snapshot == Decimal("75.00")  # 300 * 25%


def test_servicos_diferentes_do_mesmo_profissional_tem_comissao_propria(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    manutencao = _service(session, org_id, name="Manutenção", duration=60, price=Decimal("300.00"))
    corte = _service(session, org_id, name="Corte", duration=30, price=Decimal("100.00"))
    _link(session, prof.id, manutencao.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, prof.id, corte.id, commission_type=CommissionType.FIXED, commission_value=Decimal("30.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [
            {"professional_id": prof.id, "service_id": manutencao.id, "start_at": _dt(9, 0)},
            {"professional_id": prof.id, "service_id": corte.id, "start_at": _dt(10, 0)},
        ],
    )
    order = orders.create_order(session, actor, appt.id)

    closed = _close_with_pix(session, actor, order)
    by_service = {i.service_id: i for i in closed.items}
    assert by_service[manutencao.id].commission_amount_snapshot == Decimal("60.00")
    assert by_service[corte.id].commission_type_snapshot == CommissionType.FIXED
    assert by_service[corte.id].commission_amount_snapshot == Decimal("30.00")


# ---------------------------------------------------------------------
# 6 — arredondamento
# ---------------------------------------------------------------------


def test_arredondamento_com_fracao_de_centavo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Serviço fracionado", price=Decimal("333.33"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("2.99"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(9, 0)}],
    )
    order = orders.create_order(session, actor, appt.id)

    closed = _close_with_pix(session, actor, order)
    # 333.33 * 2.99% = 9.966567 -> arredonda pra 9.97 (mesma convenção
    # de `services/payment_fees.py`: `.quantize(Decimal("0.01"))`).
    assert closed.items[0].commission_amount_snapshot == Decimal("9.97")


# ---------------------------------------------------------------------
# 8 — fechamento consolidado
# ---------------------------------------------------------------------


def test_fechamento_consolidado_resolve_comissao_por_item_em_cada_comanda(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    client = _client(session, org_id, name="Amanda")

    duda = _professional(session, org_id, branch.id, name="Duda")
    manutencao = _service(session, org_id, name="Manutenção", price=Decimal("150.00"))
    _link(session, duda.id, manutencao.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, duda.id, _THURSDAY, time(8, 0), time(21, 0))

    ianka = _professional(session, org_id, branch.id, name="Ianka")
    progressiva = _service(session, org_id, name="Progressiva", price=Decimal("300.00"))
    _link(session, ianka.id, progressiva.id, commission_type=CommissionType.FIXED, commission_value=Decimal("50.00"))
    _working_hours(session, org_id, ianka.id, _THURSDAY, time(8, 0), time(21, 0))

    appt1 = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": duda.id, "service_id": manutencao.id, "start_at": _dt(9, 0)}],
    )
    appt2 = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": ianka.id, "service_id": progressiva.id, "start_at": _dt(13, 0)}],
    )
    order1 = orders.create_order(session, actor, appt1.id)
    order2 = orders.create_order(session, actor, appt2.id)

    register = _open_register(session, actor)
    payments = [PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("450.00"), cash_register_id=register.id)]
    closed = orders.close_orders_consolidated(
        session, actor, order1.id,
        ConsolidatedOrderClose(order_ids=[order1.id, order2.id], payments=payments),
    )

    by_id = {o.id: o for o in closed}
    assert by_id[order1.id].items[0].commission_amount_snapshot == Decimal("30.00")  # 150 * 20%
    assert by_id[order2.id].items[0].commission_amount_snapshot == Decimal("50.00")  # fixo


# ---------------------------------------------------------------------
# 9/10 — histórico imutável: regra muda depois, venda antiga preserva
# ---------------------------------------------------------------------


def test_alteracao_da_regra_nao_recalcula_venda_antiga_venda_nova_usa_regra_atual(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    link = _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)

    # Venda A — fecha com 20%.
    appt_a = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(9, 0)}],
    )
    order_a = orders.create_order(session, actor, appt_a.id)
    closed_a = _close_with_pix(session, actor, order_a)
    assert closed_a.items[0].commission_value_snapshot == Decimal("20.00")
    assert closed_a.items[0].commission_amount_snapshot == Decimal("60.00")

    # Regra muda pra 25%.
    link.commission_value = Decimal("25.00")
    session.flush()

    # Venda B — nova, mesmo par profissional/serviço, usa a regra ATUAL (25%).
    appt_b = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(14, 0)}],
    )
    order_b = orders.create_order(session, actor, appt_b.id)
    closed_b = _close_with_pix(session, actor, order_b)
    assert closed_b.items[0].commission_value_snapshot == Decimal("25.00")
    assert closed_b.items[0].commission_amount_snapshot == Decimal("75.00")

    # Venda A, relida do banco, continua exatamente como fechou (20%/R$60).
    session.refresh(closed_a.items[0])
    assert closed_a.items[0].commission_value_snapshot == Decimal("20.00")
    assert closed_a.items[0].commission_amount_snapshot == Decimal("60.00")


# ---------------------------------------------------------------------
# 11 — OrderItem histórico (fechado sem passar pelo snapshot) permanece nulo
# ---------------------------------------------------------------------


def test_orderitem_historico_permanece_sem_snapshot(org_session):
    """Simula uma comanda fechada ANTES da migration 0036 existir:
    construída direto via ORM, sem passar por `close_order` — nunca
    inventa comissão retroativa pra dado histórico."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    client = _client(session, org_id)
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(9, 0)}],
    )

    order = Order(
        organization_id=org_id, order_number=1, appointment_id=appt.id, branch_id=branch.id,
        client_id=client.id, status=OrderStatus.CLOSED, closed_at=datetime.now(timezone.utc),
    )
    session.add(order)
    session.flush()
    item = OrderItem(
        organization_id=org_id, order_id=order.id, service_id=svc.id, professional_id=prof.id,
        duration_minutes=60, price=Decimal("300.00"), service_name="Manutenção", professional_name="Ianka",
    )
    session.add(item)
    session.flush()

    assert item.commission_status is None
    assert item.commission_type_snapshot is None
    assert item.commission_value_snapshot is None
    assert item.commission_amount_snapshot is None


# ---------------------------------------------------------------------
# 12 — isolamento entre organizações
# ---------------------------------------------------------------------


def test_resolve_commission_nao_vaza_regra_entre_organizacoes(org_session):
    session, org_a = org_session
    actor_a = _actor(session, org_a)
    branch_a = _branch(session, org_a)
    cash_register.open_register(session, actor_a, branch_a.id, Decimal("0"), None)
    prof_a = _professional(session, org_a, branch_a.id, name="Ianka")
    svc_a = _service(session, org_a, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof_a.id, svc_a.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_a, prof_a.id, _THURSDAY, time(9, 0), time(20, 0))
    client_a = _client(session, org_a)
    appt_a = _appointment_with_items(
        session, org_a, actor_a, branch_a, client_a,
        [{"professional_id": prof_a.id, "service_id": svc_a.id, "start_at": _dt(9, 0)}],
    )
    order_a = orders.create_order(session, actor_a, appt_a.id)

    # Organização B — mesmo NOME de serviço/profissional, comissão
    # BEM diferente (40%) — nunca deveria interferir na resolução de A.
    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B comissões", slug=f"org-b-comissoes-{org_b.hex[:8]}"))
    session.flush()
    branch_b = _branch(session, org_b)
    prof_b = _professional(session, org_b, branch_b.id, name="Ianka")
    svc_b = _service(session, org_b, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof_b.id, svc_b.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("40.00"))

    # Volta pro contexto de RLS da org A antes de fechar a comanda de A
    # (mesma mecânica de `get_db` numa request real).
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
    closed_a = _close_with_pix(session, actor_a, order_a)
    assert closed_a.items[0].commission_amount_snapshot == Decimal("60.00")  # 20% de A, nunca 40% de B.

    # `resolve_commission`/`get_for_pair` também não encontram o vínculo
    # de B a partir do contexto de A, mesmo com organization_id de B
    # passado explicitamente por engano (defesa em profundidade, além
    # da RLS) — a query já filtra por `Professional.organization_id`.
    link_from_a_context = professional_service_repo.get_for_pair(session, org_b, prof_b.id, svc_b.id)
    assert link_from_a_context is None
