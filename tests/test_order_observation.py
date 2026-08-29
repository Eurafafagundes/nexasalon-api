"""Testes de `services/orders.py::update_observation` (migration 0039,
item "Comanda — Observação + Auditoria"). Reaproveita os fixtures de
`tests/test_order_products.py` (comanda de serviço + registro/caixa)."""
import uuid
from datetime import time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus, PaymentMethod
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import cash_register_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import OrderClose, OrderObservationUpdate, PaymentCreate
from nexasalon_api.services import appointments, cash_register, orders

_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4
_ALL_PERMS = frozenset(
    {
        "agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel",
        "orders.view", "orders.manage", "orders.edit_price", "payments.register",
    }
)


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org observação", slug=f"org-obs-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, name="Rafael", permissions=_ALL_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name=name)
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _dt(hour, minute=0):
    from datetime import datetime

    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _open_order(session, org_id, actor):
    branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(branch)
    session.flush()
    if cash_register_repo.get_open_for_branch(session, org_id, branch.id) is None:
        cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional")
    session.add(prof)
    session.flush()
    session.add(
        WorkingHours(organization_id=org_id, professional_id=prof.id, weekday=_THURSDAY, start_time=time(9, 0), end_time=time(20, 0))
    )
    client = Client(organization_id=org_id, name="Cliente")
    session.add(client)
    svc = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=Decimal("100.00"))
    session.add(svc)
    session.flush()
    session.add(ProfessionalService(professional_id=prof.id, service_id=svc.id))
    session.flush()
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=svc.id, start_at=_dt(9))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    order = orders.create_order(session, actor, appt.id)
    return order, branch


def test_editar_observacao_registra_quem_e_quando(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, name="Rafael")
    order, _branch = _open_order(session, org_id, actor)
    assert order.observation is None

    updated = orders.update_observation(
        session, actor, order.id, OrderObservationUpdate(observation="Cliente pediu para não usar produto X.")
    )

    assert updated.observation == "Cliente pediu para não usar produto X."
    assert updated.observation_updated_by == actor.user_id
    assert updated.observation_updated_by_name == "Rafael"
    assert updated.observation_updated_at is not None


def test_string_vazia_limpa_a_observacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, _branch = _open_order(session, org_id, actor)
    orders.update_observation(session, actor, order.id, OrderObservationUpdate(observation="Algo"))

    cleared = orders.update_observation(session, actor, order.id, OrderObservationUpdate(observation="   "))
    assert cleared.observation is None


def test_editar_observacao_de_comanda_fechada_nao_altera_status_nem_pagamentos(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    register = cash_register_repo.get_open_for_branch(session, org_id, branch.id)
    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("100.00"), cash_register_id=register.id)]),
    )
    assert closed.status.value == "closed"

    updated = orders.update_observation(
        session, actor, order.id, OrderObservationUpdate(observation="Observação pós-fechamento")
    )
    assert updated.observation == "Observação pós-fechamento"
    assert updated.status.value == "closed"  # nunca reabre
    assert len(updated.payments) == 1  # financeiro intocado


def test_edicao_concorrente_com_timestamp_desatualizado_e_recusada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, _branch = _open_order(session, org_id, actor)
    first = orders.update_observation(session, actor, order.id, OrderObservationUpdate(observation="Primeira versão"))
    stale_timestamp = first.observation_updated_at

    # Segunda edição "de verdade" acontece — timestamp em banco avança.
    orders.update_observation(session, actor, order.id, OrderObservationUpdate(observation="Segunda versão"))

    with pytest.raises(ConflictError):
        orders.update_observation(
            session, actor, order.id,
            OrderObservationUpdate(observation="Terceira versão (baseada na primeira, desatualizada)", expected_observation_updated_at=stale_timestamp),
        )

    reloaded = orders.get_order(session, actor, order.id)
    assert reloaded.observation == "Segunda versão"  # não foi sobrescrita silenciosamente


def test_edicao_com_timestamp_atualizado_e_aceita(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, _branch = _open_order(session, org_id, actor)
    first = orders.update_observation(session, actor, order.id, OrderObservationUpdate(observation="Primeira versão"))

    updated = orders.update_observation(
        session, actor, order.id,
        OrderObservationUpdate(observation="Segunda versão", expected_observation_updated_at=first.observation_updated_at),
    )
    assert updated.observation == "Segunda versão"


def test_comanda_inexistente_404(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    with pytest.raises(NotFoundError):
        orders.update_observation(session, actor, uuid.uuid4(), OrderObservationUpdate(observation="x"))


def test_isolamento_multiempresa_comanda_de_outra_org(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, _branch = _open_order(session, org_id, actor)

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-obs-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_actor = _actor(other_session, other_org_id)
        other_session.commit()

    with pytest.raises(NotFoundError):
        orders.update_observation(session, other_actor, order.id, OrderObservationUpdate(observation="invasão"))
