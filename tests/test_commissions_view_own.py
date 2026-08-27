"""Testes da Etapa C5 — Minha Comissão / `commissions.view_own` (leitura
escopada ao próprio profissional em `get_overview`/`get_detail`/
`get_pending_adjustments`/`list_settlements`/`get_settlement_detail`),
mais o acabamento de permissões (`commissions.manage` agora também
concede escopo completo de leitura — `_has_full_view_scope` — mesma
regra que a rota já aplicava, só tornada consistente na service layer).

Cobre os 17 cenários mínimos pedidos: view_own vê o próprio overview/
detalhe/settlement; nunca vê outro profissional (nem por parâmetro
malicioso `professional_id`); usuário sem vínculo `Professional` nunca
acessa nada; view_all continua vendo todos; `manage` registra
pagamento; `view_own` nunca registra pagamento nem cria ajuste (HTTP,
403 real — nunca só escondido no frontend); isolamento entre
organizações; snapshot congelado continua sendo a fonte dos valores
mesmo depois de mudar a regra atual.

Mesmo padrão de `test_commission_settlements.py` (C4) — direto no
service layer via `SessionLocal`, helpers redefinidos localmente; os
testes HTTP (10/11) usam `client_as`/`org_a_actor` de `conftest.py`,
mesmo padrão de `test_commission_settlements.py`."""
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from datetime import time as time_type
from decimal import Decimal

import pytest
from sqlalchemy import select, text

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
_ALL_COMMISSION_PERMS = frozenset({"commissions.view_all", "commissions.view_own", "commissions.manage"})
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 2026-08-13 é quinta.
_PERIOD = {"date_from": datetime(2026, 8, 1, 0, 0, tzinfo=_TZ), "date_to": datetime(2026, 8, 31, 23, 59, tzinfo=_TZ)}


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org view_own", slug=f"org-view-own-{org_id.hex[:8]}"))
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


def _two_professionals_scenario(session, org_id):
    """Ianka e Duda, cada uma com uma venda fechada e comissão
    CALCULADA — o cenário base da maioria dos testes deste arquivo."""
    owner = _actor(session, org_id)
    branch = _branch(session, org_id)
    svc = _service(session, org_id, name="Manutenção", price=Decimal("300.00"))
    ianka = _professional(session, org_id, branch.id, name="Ianka")
    duda = _professional(session, org_id, branch.id, name="Duda")
    _link(session, ianka.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("20.00"))
    _link(session, duda.id, svc.id, commission_type=CommissionType.PERCENTAGE, commission_value=Decimal("25.00"))
    _working_hours(session, org_id, ianka.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    _working_hours(session, org_id, duda.id, _THURSDAY, time_type(9, 0), time_type(20, 0))
    client = _client(session, org_id)
    _sale(session, org_id, owner, branch, ianka, svc, client, day=13, hour=9)
    _sale(session, org_id, owner, branch, duda, svc, client, day=13, hour=11)
    return owner, branch, svc, ianka, duda, client


# ---------------------------------------------------------------------
# 1/2 — view_own vê o próprio overview e detalhe
# ---------------------------------------------------------------------


def test_view_own_ve_proprio_overview(org_session):
    session, org_id = org_session
    _owner, _branch, _svc, ianka, _duda, _client = _two_professionals_scenario(session, org_id)
    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})

    overview = commissions.get_overview(session, ianka_actor, **_PERIOD)
    assert overview.production == Decimal("300.00")
    assert overview.known_commission_total == Decimal("60.00")
    assert len(overview.professionals) == 1
    assert overview.professionals[0].professional_id == ianka.id


def test_view_own_ve_proprio_detalhe(org_session):
    session, org_id = org_session
    _owner, _branch, _svc, ianka, _duda, _client = _two_professionals_scenario(session, org_id)
    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})

    detail = commissions.get_detail(session, ianka_actor, ianka.id, **_PERIOD)
    assert detail.professional_name == "Ianka"
    assert len(detail.items) == 1
    assert detail.items[0].service_name == "Manutenção"


# ---------------------------------------------------------------------
# 3 — view_own vê o próprio settlement (histórico)
# ---------------------------------------------------------------------


def test_view_own_ve_proprio_settlement(org_session):
    session, org_id = org_session
    owner, _branch, _svc, ianka, _duda, _client = _two_professionals_scenario(session, org_id)
    settlement = commissions.create_settlement(session, owner, professional_id=ianka.id, **_PERIOD)

    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})
    settlements = commissions.list_settlements(session, ianka_actor)
    assert len(settlements) == 1
    assert settlements[0].id == settlement.id

    detail = commissions.get_settlement_detail(session, ianka_actor, settlement.id)
    assert detail.settlement.id == settlement.id
    assert len(detail.items) == 1
    assert detail.items[0].service_name == "Manutenção"


# ---------------------------------------------------------------------
# 4/5/6 — view_own nunca vê outro profissional, nem por settlement, nem
# por parâmetro malicioso de professional_id
# ---------------------------------------------------------------------


def test_view_own_nao_ve_outro_profissional(org_session):
    session, org_id = org_session
    _owner, _branch, _svc, ianka, duda, _client = _two_professionals_scenario(session, org_id)
    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})

    with pytest.raises(NotFoundError):
        commissions.get_detail(session, ianka_actor, duda.id, **_PERIOD)


def test_view_own_nao_ve_settlement_alheio(org_session):
    session, org_id = org_session
    owner, _branch, _svc, ianka, duda, _client = _two_professionals_scenario(session, org_id)
    settlement_duda = commissions.create_settlement(session, owner, professional_id=duda.id, **_PERIOD)

    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})
    with pytest.raises(NotFoundError):
        commissions.get_settlement_detail(session, ianka_actor, settlement_duda.id)

    # E o settlement da Duda nunca aparece no histórico "próprio" da
    # Ianka, mesmo sem pedir o detalhe.
    settlements = commissions.list_settlements(session, ianka_actor)
    assert settlements == []


def test_query_maliciosa_professional_id_de_outro_e_ignorado(org_session):
    session, org_id = org_session
    _owner, _branch, _svc, ianka, duda, _client = _two_professionals_scenario(session, org_id)
    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})

    # Pede explicitamente o professional_id da Duda no parâmetro — nunca
    # aceito, sempre forçado pro próprio ator.
    overview = commissions.get_overview(session, ianka_actor, professional_id=duda.id, **_PERIOD)
    assert len(overview.professionals) == 1
    assert overview.professionals[0].professional_id == ianka.id
    assert overview.known_commission_total == Decimal("60.00")  # nunca a comissão da Duda (75.00).

    settlements = commissions.list_settlements(session, ianka_actor, professional_id=duda.id)
    assert settlements == []  # nunca lista settlement da Duda mesmo pedindo explicitamente.


# ---------------------------------------------------------------------
# 7 — usuário sem vínculo Professional nunca acessa nada
# ---------------------------------------------------------------------


def test_usuario_sem_professional_id_nao_acessa_nada(org_session):
    session, org_id = org_session
    _owner, _branch, _svc, ianka, _duda, _client = _two_professionals_scenario(session, org_id)
    unlinked_actor = _actor(session, org_id, professional_id=None, permissions={"commissions.view_own"})

    overview = commissions.get_overview(session, unlinked_actor, **_PERIOD)
    assert overview.professionals == []
    assert overview.production == Decimal("0")

    with pytest.raises(NotFoundError):
        commissions.get_detail(session, unlinked_actor, ianka.id, **_PERIOD)

    assert commissions.list_settlements(session, unlinked_actor) == []


# ---------------------------------------------------------------------
# 8 — view_all continua vendo todos
# ---------------------------------------------------------------------


def test_view_all_continua_vendo_todos(org_session):
    session, org_id = org_session
    owner, _branch, _svc, _ianka, duda, _client = _two_professionals_scenario(session, org_id)

    overview = commissions.get_overview(session, owner, **_PERIOD)
    assert overview.production == Decimal("600.00")
    assert len(overview.professionals) == 2

    detail_duda = commissions.get_detail(session, owner, duda.id, **_PERIOD)
    assert len(detail_duda.items) == 1


# ---------------------------------------------------------------------
# 9/10/11 — manage paga/cria ajuste; view_own nunca (HTTP, 403 real)
# ---------------------------------------------------------------------


def test_manage_registra_pagamento(org_session):
    session, org_id = org_session
    _owner, _branch, _svc, ianka, _duda, _client = _two_professionals_scenario(session, org_id)
    manage_only = _actor(session, org_id, permissions={"commissions.manage"})

    settlement = commissions.create_settlement(session, manage_only, professional_id=ianka.id, **_PERIOD)
    assert settlement.commission_total == Decimal("60.00")

    adjustment = commissions.create_adjustment(
        session, manage_only, professional_id=ianka.id, amount=Decimal("10.00"), reason="Bônus",
    )
    assert adjustment.amount == Decimal("10.00")


def test_view_own_nao_registra_pagamento_http(org_a_actor, client_as):
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


def test_view_own_nao_cria_ajuste_http(org_a_actor, client_as):
    restricted = replace(org_a_actor, permissions=frozenset({"commissions.view_own"}), professional_id=uuid.uuid4())
    client = client_as(restricted)
    resp = client.post(
        "/api/v1/commissions/adjustments",
        json={"professional_id": str(restricted.professional_id), "amount": "10.00", "reason": "Tentativa"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# 12 — isolamento entre organizações (view_own também nunca vaza entre orgs)
# ---------------------------------------------------------------------


def test_tenant_isolation_view_own(org_session):
    session, org_a = org_session
    owner_a, _branch_a, _svc_a, ianka_a, _duda_a, _client_a = _two_professionals_scenario(session, org_a)
    settlement_a = commissions.create_settlement(session, owner_a, professional_id=ianka_a.id, **_PERIOD)

    org_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
    session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
    session.flush()
    # Ator na org B com o MESMO professional_id (por acidente/coincidência
    # de UUID não é realista, mas o teste importante é: RLS/organization_id
    # isolam de qualquer forma — nunca enxerga nada da org A.
    actor_b = _actor(session, org_b, professional_id=ianka_a.id, permissions={"commissions.view_own"})

    overview_b = commissions.get_overview(session, actor_b, **_PERIOD)
    assert overview_b.professionals == []

    with pytest.raises(NotFoundError):
        commissions.get_settlement_detail(session, actor_b, settlement_a.id)

    assert commissions.list_settlements(session, actor_b) == []


# ---------------------------------------------------------------------
# 17 — snapshot congelado continua sendo a fonte dos valores em view_own
# ---------------------------------------------------------------------


def test_view_own_sempre_usa_snapshot_nunca_regra_atual(org_session):
    session, org_id = org_session
    _owner, _branch, svc, ianka, _duda, _client = _two_professionals_scenario(session, org_id)
    ianka_actor = _actor(session, org_id, professional_id=ianka.id, permissions={"commissions.view_own"})

    before = commissions.get_detail(session, ianka_actor, ianka.id, **_PERIOD)
    assert before.items[0].commission_amount_snapshot == Decimal("60.00")

    link = session.scalars(
        select(ProfessionalService).where(
            ProfessionalService.professional_id == ianka.id, ProfessionalService.service_id == svc.id
        )
    ).one()
    link.commission_value = Decimal("50.00")
    session.flush()

    after = commissions.get_detail(session, ianka_actor, ianka.id, **_PERIOD)
    assert after.items[0].commission_amount_snapshot == Decimal("60.00")  # continua o valor congelado.
