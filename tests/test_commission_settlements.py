"""Testes da Etapa C4 — Fechamento/Pagamento de Comissão + Ajustes
Auditáveis (`services/commissions.py::create_settlement`/
`create_adjustment`/`get_pending_adjustments`/`list_settlements`/
`get_settlement_detail`).

Cobre os 20 cenários pedidos: settlement com 1/vários itens; itens
ficam vinculados; item nunca é pago duas vezes (sequencial E sob
concorrência real); `not_configured`/histórico nunca entram; duas
profissionais e duas organizações nunca se misturam; permissão
(`view_all` sozinho não paga, `view_own` não paga, `manage` paga);
total do settlement = soma exata dos snapshots incluídos; venda
atrasada no mesmo período não é absorvida retroativamente por um
settlement já criado; ajuste positivo/negativo, motivo obrigatório,
ajuste de outra organização recusado, ajuste pode entrar no
settlement; histórico mostra os itens exatos.

Mesmo padrão de `test_commissions_overview.py` (C3) — direto no
service layer via `SessionLocal`, helpers redefinidos localmente. O
cenário de concorrência real (20) segue o padrão de
`test_appointment_concurrency.py` — threads com sessões/conexões
independentes, sincronizadas por `threading.Event`, provando bloqueio
de verdade (não só "chegou depois")."""
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from datetime import time as time_type
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
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
_ALL_COMMISSION_PERMS = frozenset({"commissions.view_all", "commissions.view_own", "commissions.manage"})
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 2026-08-13 é quinta.


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org settlements", slug=f"org-settlements-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, professional_id=None, permissions=_ALL_AGENDA_PERMS | _ALL_COMMISSION_PERMS) -> ActorContext:
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
    appt = _appointment_with_items(
        session, org_id, actor, branch, client,
        [{"professional_id": prof.id, "service_id": svc.id, "start_at": _dt(day, hour)}],
    )
    order = orders.create_order(session, actor, appt.id)
    return _close_with_pix(session, actor, order)


def _setup_one_sale(session, org_id, *, price=Decimal("300.00"), commission_percent=Decimal("20.00")):
    """Um profissional com uma venda fechada e comissão CALCULADA — o
    caminho comum da maioria dos testes deste arquivo."""
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=price)
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=commission_percent)
    _working_hours(session, org_id, prof.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)
    return actor, prof, svc, client, branch


_PERIOD = {"date_from": _dt(1, 0), "date_to": _dt(31, 23, 59)}


# ---------------------------------------------------------------------
# 1/2/3 — settlement com 1/vários itens; itens ficam vinculados
# ---------------------------------------------------------------------


def test_settlement_com_um_item(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)

    settlement = commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)
    assert settlement.professional_id == prof.id
    assert settlement.production_total == Decimal("300.00")
    assert settlement.commission_total == Decimal("60.00")


def test_settlement_com_varios_orderitems(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    _link(session, prof.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13, hour=9)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13, hour=11)

    settlement = commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)
    assert settlement.production_total == Decimal("600.00")
    assert settlement.commission_total == Decimal("120.00")


def test_itens_ficam_vinculados_ao_settlement(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)

    settlement = commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)

    detail = commissions.get_detail(session, actor, prof.id, **_PERIOD)
    assert len(detail.items) == 1
    assert detail.items[0].commission_settlement_id == settlement.id


# ---------------------------------------------------------------------
# 4 — item não pode ser pago duas vezes (sequencial)
# ---------------------------------------------------------------------


def test_item_nao_pode_ser_pago_duas_vezes_sequencial(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)

    commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)

    with pytest.raises(ValidationDomainError):
        commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)


# ---------------------------------------------------------------------
# 5/6 — not_configured e histórico nunca entram
# ---------------------------------------------------------------------


def test_settlement_nao_inclui_not_configured(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    svc = _service(session, org_id, name="Corte", price=Decimal("50.00"))
    _link(session, prof.id, svc.id)  # sem comissão configurada.
    _working_hours(session, org_id, prof.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, prof, svc, client, day=13)

    with pytest.raises(ValidationDomainError):
        commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)


def test_settlement_nao_inclui_historico_null(org_session):
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

    with pytest.raises(ValidationDomainError):
        commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)


# ---------------------------------------------------------------------
# 7/8 — duas profissionais e duas organizações nunca se misturam
# ---------------------------------------------------------------------


def test_duas_profissionais_nao_se_misturam(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    ianka = _professional(session, org_id, branch.id, name="Ianka")
    duda = _professional(session, org_id, branch.id, name="Duda")
    _link(session, ianka.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, duda.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("25.00"))
    _working_hours(session, org_id, ianka.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    _working_hours(session, org_id, duda.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, actor, branch, ianka, svc, client, day=13, hour=9)
    _sale(session, org_id, actor, branch, duda, svc, client, day=13, hour=11)

    settlement_ianka = commissions.create_settlement(session, actor, professional_id=ianka.id, **_PERIOD)
    assert settlement_ianka.commission_total == Decimal("60.00")

    # A comissão da Duda continua pendente — não foi tocada pelo
    # settlement da Ianka.
    detail_duda = commissions.get_detail(session, actor, duda.id, **_PERIOD)
    assert detail_duda.items[0].commission_settlement_id is None

    settlement_duda = commissions.create_settlement(session, actor, professional_id=duda.id, **_PERIOD)
    assert settlement_duda.commission_total == Decimal("75.00")


def test_tenant_isolation_settlement(org_session):
    session, org_a = org_session
    actor_a, prof_a, _svc_a, _client_a, _branch_a = _setup_one_sale(session, org_a)

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_b)

    with pytest.raises(NotFoundError):
        commissions.create_settlement(session, actor_b, professional_id=prof_a.id, **_PERIOD)

    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
    settlement = commissions.create_settlement(session, actor_a, professional_id=prof_a.id, **_PERIOD)
    assert settlement.commission_total == Decimal("60.00")


# ---------------------------------------------------------------------
# 9/10/11 — permissão: view_all sozinho não paga, view_own não paga, manage paga
# ---------------------------------------------------------------------


def test_view_all_sem_manage_nao_paga_http(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"commissions.view_all"}))
    client = client_as(restricted)
    resp = client.post(
        "/api/v1/commissions/settlements",
        json={
            "professional_id": str(uuid.uuid4()),
            "date_from": "2026-08-01T00:00:00-03:00",
            "date_to": "2026-08-31T23:59:59-03:00",
        },
    )
    assert resp.status_code == 403


def test_view_own_nao_paga_http(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"commissions.view_own"}), professional_id=uuid.uuid4())
    client = client_as(restricted)
    resp = client.post(
        "/api/v1/commissions/settlements",
        json={
            "professional_id": str(restricted.professional_id),
            "date_from": "2026-08-01T00:00:00-03:00",
            "date_to": "2026-08-31T23:59:59-03:00",
        },
    )
    assert resp.status_code == 403


def test_manage_paga(org_session):
    session, org_id = org_session
    _owner_actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)
    manage_only = _actor(session, org_id, permissions={"commissions.manage", "commissions.view_all"})

    settlement = commissions.create_settlement(session, manage_only, professional_id=prof.id, **_PERIOD)
    assert settlement.commission_total == Decimal("60.00")


# ---------------------------------------------------------------------
# 12 — total do settlement = soma dos snapshots incluídos
# ---------------------------------------------------------------------


def test_total_do_settlement_e_soma_exata_dos_snapshots(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id, name="Ianka")
    manutencao = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    corte = _service(session, org_id, name="Corte", price=Decimal("100.00"))
    _link(session, prof.id, manutencao.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, prof.id, corte.id, commission_type=CommissionType.FIXED, commission_value=Decimal("30.00"))
    _working_hours(session, org_id, prof.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
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

    settlement = commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)
    assert settlement.production_total == Decimal("400.00")
    assert settlement.commission_total == Decimal("90.00")  # 60 (20% de 300) + 30 (fixo)


# ---------------------------------------------------------------------
# 13 — venda atrasada no mesmo período não entra em settlement já criado
# ---------------------------------------------------------------------


def test_venda_atrasada_nao_entra_em_settlement_ja_criado(org_session):
    session, org_id = org_session
    actor, prof, svc, client, branch = _setup_one_sale(session, org_id)

    settlement = commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)
    assert settlement.production_total == Decimal("300.00")

    # Uma segunda venda, competência DENTRO do mesmo período, só
    # "aparece" (fecha) depois que o settlement já existe.
    _sale(session, org_id, actor, branch, prof, svc, client, day=20, hour=9)

    detail = commissions.get_detail(session, actor, prof.id, **_PERIOD)
    pending = [i for i in detail.items if i.commission_settlement_id is None]
    assert len(pending) == 1
    assert pending[0].price == Decimal("300.00")

    # Uma nova liquidação pega só a venda atrasada — nunca reabre a
    # primeira.
    settlement_2 = commissions.create_settlement(session, actor, professional_id=prof.id, **_PERIOD)
    assert settlement_2.id != settlement.id
    assert settlement_2.production_total == Decimal("300.00")


# ---------------------------------------------------------------------
# 14/15/16/17/18 — ajustes auditáveis
# ---------------------------------------------------------------------


def test_ajuste_positivo(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)

    adjustment = commissions.create_adjustment(
        session, actor, professional_id=prof.id, amount=Decimal("50.00"), reason="Bônus por meta batida",
    )
    assert adjustment.amount == Decimal("50.00")
    assert adjustment.commission_settlement_id is None


def test_ajuste_negativo(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)

    adjustment = commissions.create_adjustment(
        session, actor, professional_id=prof.id, amount=Decimal("-30.00"), reason="Correção de valor lançado a mais",
    )
    assert adjustment.amount == Decimal("-30.00")


def test_ajuste_exige_motivo(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)

    with pytest.raises(ValidationDomainError):
        commissions.create_adjustment(session, actor, professional_id=prof.id, amount=Decimal("50.00"), reason="   ")

    with pytest.raises(ValidationDomainError):
        commissions.create_adjustment(session, actor, professional_id=prof.id, amount=Decimal("0"), reason="Motivo")


def test_ajuste_de_outra_organizacao_e_recusado(org_session):
    session, org_a = org_session
    actor_a, prof_a, _svc_a, _client_a, _branch_a = _setup_one_sale(session, org_a)

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    actor_b, prof_b, _svc_b, _client_b, _branch_b = _setup_one_sale(session, org_b)

    detail_b = commissions.get_detail(session, actor_b, prof_b.id, **_PERIOD)
    order_item_id_b = detail_b.items[0].order_item_id

    # Volta pro contexto da org A e tenta criar um ajuste apontando pra
    # um OrderItem da org B — nunca permitido.
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
    with pytest.raises(NotFoundError):
        commissions.create_adjustment(
            session, actor_a, professional_id=prof_a.id, amount=Decimal("10.00"), reason="Tentativa cross-tenant",
            order_item_id=order_item_id_b,
        )


def test_ajuste_pode_entrar_no_settlement(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)
    adjustment = commissions.create_adjustment(
        session, actor, professional_id=prof.id, amount=Decimal("50.00"), reason="Bônus",
    )

    settlement = commissions.create_settlement(
        session, actor, professional_id=prof.id, adjustment_ids=[adjustment.id], **_PERIOD
    )
    assert settlement.commission_total == Decimal("110.00")  # 60 (item) + 50 (ajuste)

    pending = commissions.get_pending_adjustments(session, actor, prof.id)
    assert pending == []


def test_ajuste_ja_liquidado_nao_pode_ser_incluido_de_novo(org_session):
    session, org_id = org_session
    actor, prof, svc, client, branch = _setup_one_sale(session, org_id)
    adjustment = commissions.create_adjustment(
        session, actor, professional_id=prof.id, amount=Decimal("50.00"), reason="Bônus",
    )
    commissions.create_settlement(session, actor, professional_id=prof.id, adjustment_ids=[adjustment.id], **_PERIOD)

    # Uma segunda venda pra ter algo a liquidar de novo.
    _sale(session, org_id, actor, branch, prof, svc, client, day=20, hour=9)
    with pytest.raises(ConflictError):
        commissions.create_settlement(
            session, actor, professional_id=prof.id, adjustment_ids=[adjustment.id], **_PERIOD
        )


# ---------------------------------------------------------------------
# 19 — histórico mostra os itens exatos
# ---------------------------------------------------------------------


def test_historico_mostra_itens_e_ajustes_exatos(org_session):
    session, org_id = org_session
    actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)
    adjustment = commissions.create_adjustment(
        session, actor, professional_id=prof.id, amount=Decimal("50.00"), reason="Bônus",
    )
    settlement = commissions.create_settlement(
        session, actor, professional_id=prof.id, adjustment_ids=[adjustment.id], **_PERIOD
    )

    settlements = commissions.list_settlements(session, actor, professional_id=prof.id)
    assert len(settlements) == 1
    assert settlements[0].id == settlement.id
    assert settlements[0].professional_name == "Ianka"

    detail = commissions.get_settlement_detail(session, actor, settlement.id)
    assert len(detail.items) == 1
    assert detail.items[0].service_name == "Manutenção"
    assert detail.items[0].commission_amount_snapshot == Decimal("60.00")
    assert len(detail.adjustments) == 1
    assert detail.adjustments[0].amount == Decimal("50.00")
    assert detail.adjustments[0].reason == "Bônus"


# ---------------------------------------------------------------------
# 20 — concorrência real: nunca paga o mesmo item duas vezes
# ---------------------------------------------------------------------


@pytest.fixture()
def concurrency_scenario():
    """Mesmo raciocínio de `test_appointment_concurrency.py::scenario`
    — cria e COMMITA (não só flush) org/profissional/venda fechada com
    comissão CALCULADA; as duas threads abrem sessões/conexões novas e
    só enxergam dado já commitado."""
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org concorrência comissão", slug=f"org-conc-com-{org_id.hex[:8]}"))
        session.flush()
        owner_actor, prof, _svc, _client, _branch = _setup_one_sale(session, org_id)
        ids = {"org_id": org_id, "professional_id": prof.id, "user_id": owner_actor.user_id}
        session.commit()
        return ids


def _open_scoped_session(org_id):
    session = SessionLocal()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
    return session


def test_concorrencia_real_impede_pagamento_duplicado(concurrency_scenario):
    results = {}
    thread1_locked = threading.Event()

    def _actor_for(session):
        return ActorContext(
            organization_id=concurrency_scenario["org_id"], user_id=concurrency_scenario["user_id"],
            membership_id=uuid.uuid4(), role_id=uuid.uuid4(), role_name="Owner",
            permissions=_ALL_COMMISSION_PERMS, professional_id=None,
        )

    def _thread1():
        session = _open_scoped_session(concurrency_scenario["org_id"])
        try:
            actor = _actor_for(session)
            commissions.create_settlement(
                session, actor, professional_id=concurrency_scenario["professional_id"], **_PERIOD
            )
            # item já travado (FOR UPDATE) e reivindicado nesta
            # transação (não commitada ainda) — segura tudo aberto.
            thread1_locked.set()
            time.sleep(1.0)
            session.commit()
            results["thread1"] = "ok"
        except Exception as exc:  # pragma: no cover
            session.rollback()
            results["thread1"] = exc
        finally:
            session.close()

    def _thread2():
        thread1_locked.wait(timeout=5)
        session = _open_scoped_session(concurrency_scenario["org_id"])
        start = time.perf_counter()
        try:
            actor = _actor_for(session)
            commissions.create_settlement(
                session, actor, professional_id=concurrency_scenario["professional_id"], **_PERIOD
            )
            session.commit()
            results["thread2"] = "ok"
        except Exception as exc:
            session.rollback()
            results["thread2"] = exc
        finally:
            results["thread2_elapsed"] = time.perf_counter() - start
            session.close()

    t1 = threading.Thread(target=_thread1)
    t2 = threading.Thread(target=_thread2)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results["thread1"] == "ok"
    # A Thread 2 ficou de fato BLOQUEADA no `SELECT ... FOR UPDATE`
    # esperando a Thread 1 commitar (prova de bloqueio real, não só
    # "chegou depois") — ao ser liberada, reavalia o WHERE e não sobra
    # nenhum item pendente: nunca paga o mesmo item duas vezes.
    assert results["thread2_elapsed"] >= 0.8
    assert isinstance(results["thread2"], ValidationDomainError), f"esperava recusa, veio: {results['thread2']!r}"

    with SessionLocal() as check_session:
        check_session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"),
            {"oid": str(concurrency_scenario["org_id"])},
        )
        actor = _actor_for(check_session)
        settlements = commissions.list_settlements(
            check_session, actor, professional_id=concurrency_scenario["professional_id"]
        )
        assert len(settlements) == 1  # só a Thread 1 criou settlement.
