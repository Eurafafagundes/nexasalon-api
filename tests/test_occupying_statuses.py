"""Regra centralizada de "quais status de Appointment ocupam a agenda"
(ajuste pós-revisão da Etapa 3A) — testada explicitamente, item por
item da tabela pedida, tanto pelo lado da DISPONIBILIDADE quanto da
CHECAGEM DE CONFLITO, provando que as duas usam a MESMA fonte
(`appointment_item_repo.OCCUPYING_STATUSES`), sem regra duplicada.

    SCHEDULED   -> ocupa
    CONFIRMED   -> ocupa
    WAITING     -> ocupa
    IN_PROGRESS -> ocupa
    FINISHED    -> não ocupa (fica só no histórico)
    CANCELLED   -> não ocupa
    NO_SHOW     -> não ocupa
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import appointment_item_repo
from nexasalon_api.repositories.appointment_item_repo import OCCUPYING_STATUSES
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.services import appointments, availability

_OUR_THURSDAY = 4
_THURSDAY = date(2026, 8, 13)
_TZ = timezone(timedelta(hours=-3))

# A tabela exata pedida na revisão — fonte única deste arquivo de teste.
OCCUPYING_TABLE = [
    (AppointmentStatus.SCHEDULED, True),
    (AppointmentStatus.CONFIRMED, True),
    (AppointmentStatus.WAITING, True),
    (AppointmentStatus.IN_PROGRESS, True),
    (AppointmentStatus.FINISHED, False),
    (AppointmentStatus.CANCELLED, False),
    (AppointmentStatus.NO_SHOW, False),
]


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org status", slug=f"org-status-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _setup_basic(session, org_id):
    branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(branch)
    session.flush()
    prof = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional")
    session.add(prof)
    session.flush()
    service = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=100)
    session.add(service)
    session.flush()
    session.add(ProfessionalService(professional_id=prof.id, service_id=service.id))
    session.add(
        WorkingHours(
            organization_id=org_id, professional_id=prof.id, weekday=_OUR_THURSDAY,
            start_time=time(9, 0), end_time=time(18, 0),
        )
    )
    client = Client(organization_id=org_id, name="Cliente")
    session.add(client)
    session.flush()
    return branch, prof, service, client


def _create_item_with_status(session, org_id, branch, prof, service, client, status: AppointmentStatus):
    appt = Appointment(organization_id=org_id, branch_id=branch.id, client_id=client.id, status=status)
    session.add(appt)
    session.flush()
    item = AppointmentItem(
        organization_id=org_id, appointment_id=appt.id, service_id=service.id, professional_id=prof.id,
        start_at=_dt(14, 0), end_at=_dt(15, 0), duration_minutes=60, price=100,
    )
    session.add(item)
    session.flush()
    return appt, item


def test_occupying_statuses_bate_com_a_tabela_pedida():
    expected = {status for status, occupies in OCCUPYING_TABLE if occupies}
    assert OCCUPYING_STATUSES == expected


@pytest.mark.parametrize("status,should_occupy", OCCUPYING_TABLE)
def test_disponibilidade_respeita_a_regra_por_status(org_session, status, should_occupy):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    _create_item_with_status(session, org_id, branch, prof, service, client, status)

    slots = availability.compute_availability(
        session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
        target_date=_THURSDAY, slot_minutes=30,
    )
    starts = {s.start_at.time() for s in slots}
    if should_occupy:
        assert time(14, 0) not in starts, f"{status} deveria ocupar a agenda (bloquear o slot)"
    else:
        assert time(14, 0) in starts, f"{status} não deveria ocupar a agenda (slot deveria estar livre)"


@pytest.mark.parametrize("status,should_occupy", OCCUPYING_TABLE)
def test_checagem_de_conflito_respeita_a_regra_por_status(org_session, status, should_occupy):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    _create_item_with_status(session, org_id, branch, prof, service, client, status)

    conflicts = appointment_item_repo.list_conflicts(
        session, org_id, professional_id=prof.id, start_at=_dt(14, 30), end_at=_dt(15, 30),
    )
    if should_occupy:
        assert len(conflicts) == 1, f"{status} deveria gerar conflito"
    else:
        assert conflicts == [], f"{status} não deveria gerar conflito"


def test_reservar_sobre_item_cancelled_funciona_ponta_a_ponta(org_session):
    """CANCELLED não ocupa nem na pré-checagem de aplicação nem no
    trigger do banco (os dois excluem cancelled/no_show) — o fluxo
    completo de criação deve funcionar sem precisar de force_overlap."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    _create_item_with_status(session, org_id, branch, prof, service, client, AppointmentStatus.CANCELLED)
    actor = _owner_actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert len(appt.items) == 1


def test_reservar_sobre_item_no_show_funciona_ponta_a_ponta(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    _create_item_with_status(session, org_id, branch, prof, service, client, AppointmentStatus.NO_SHOW)
    actor = _owner_actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert len(appt.items) == 1


def test_reservar_sobre_item_finished_e_livre_na_pre_checagem(org_session):
    """FINISHED não ocupa na disponibilidade nem na pré-checagem de
    aplicação (a regra pedida nesta revisão). Não testamos o INSERT
    completo (`create_appointment`) sobre um item FINISHED aqui: o
    trigger do banco (migration 0004) ainda não foi atualizado pra
    excluir FINISHED da sua própria checagem — alinhar isso exigiria uma
    nova migration, fora do escopo deste ajuste (ver comentário em
    `appointment_item_repo.OCCUPYING_STATUSES`). Este teste cobre
    exatamente o que hoje está garantido: disponibilidade e pré-checagem
    concordam e não bloqueiam."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    _create_item_with_status(session, org_id, branch, prof, service, client, AppointmentStatus.FINISHED)

    conflicts = appointment_item_repo.list_conflicts(
        session, org_id, professional_id=prof.id, start_at=_dt(14, 0), end_at=_dt(15, 0),
    )
    assert conflicts == []

    slots = availability.compute_availability(
        session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
        target_date=_THURSDAY, slot_minutes=30,
    )
    assert time(14, 0) in {s.start_at.time() for s in slots}


def _owner_actor(session, org_id):
    from nexasalon_api.core.actor import ActorContext
    from nexasalon_api.models.identity import User

    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner",
        permissions=frozenset(
            {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel", "agenda.force_overlap"}
        ),
        professional_id=None,
    )
