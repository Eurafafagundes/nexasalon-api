"""Testes do serviço de listagem da agenda (`services/agenda.py`) —
escopo view_own x view_all, filtros e isolamento multi-tenant."""
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.client import Client
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.services import agenda, appointments

_OUR_THURSDAY = 4
_TZ = timezone(timedelta(hours=-3))


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org agenda", slug=f"org-agenda-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions, professional_id=None) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="role", permissions=frozenset(permissions), professional_id=professional_id,
    )


def _branch(session, org_id) -> Branch:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _professional(session, org_id, branch_id, name="Prof") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Corte", duration=60, price=100) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=duration, default_price=price)
    session.add(s)
    session.flush()
    return s


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _setup_two_professionals(session, org_id):
    branch = _branch(session, org_id)
    prof_a = _professional(session, org_id, branch.id, "Profissional A")
    prof_b = _professional(session, org_id, branch.id, "Profissional B")
    service = _service(session, org_id)
    for p in (prof_a, prof_b):
        session.add(ProfessionalService(professional_id=p.id, service_id=service.id))
        session.add(
            WorkingHours(
                organization_id=org_id, professional_id=p.id, weekday=_OUR_THURSDAY,
                start_time=time(9, 0), end_time=time(18, 0),
            )
        )
    session.flush()
    client = Client(organization_id=org_id, name="Cliente")
    session.add(client)
    session.flush()
    return branch, prof_a, prof_b, service, client


def test_view_own_so_lista_itens_do_proprio_profissional(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all"})

    for prof, hour in ((prof_a, 10), (prof_b, 14)):
        appointments.create_appointment(
            session, owner,
            AppointmentCreate(
                branch_id=branch.id, client_id=client.id,
                items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(hour))],
            ),
        )

    prof_a_actor = _actor(session, org_id, permissions={"agenda.view_own"}, professional_id=prof_a.id)
    items = agenda.list_agenda(
        session, prof_a_actor, date_from=_dt(0), date_to=_dt(23, 59),
    )
    assert len(items) == 1
    assert items[0].professional_id == prof_a.id


def test_view_own_ignora_professional_id_de_outro_profissional(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all"})
    appointments.create_appointment(
        session, owner,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof_b.id, service_id=service.id, start_at=_dt(14))],
        ),
    )

    prof_a_actor = _actor(session, org_id, permissions={"agenda.view_own"}, professional_id=prof_a.id)
    # pede explicitamente a agenda do prof_b (outro profissional) sem ter view_all
    items = agenda.list_agenda(
        session, prof_a_actor, date_from=_dt(0), date_to=_dt(23, 59), professional_id=prof_b.id,
    )
    assert items == []


def test_view_all_lista_todos_profissionais(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all"})
    for prof, hour in ((prof_a, 10), (prof_b, 14)):
        appointments.create_appointment(
            session, owner,
            AppointmentCreate(
                branch_id=branch.id, client_id=client.id,
                items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(hour))],
            ),
        )

    receptionist = _actor(session, org_id, permissions={"agenda.view_all"})
    items = agenda.list_agenda(session, receptionist, date_from=_dt(0), date_to=_dt(23, 59))
    assert len(items) == 2


def test_sem_view_own_nem_view_all_lista_vazia(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all"})
    appointments.create_appointment(
        session, owner,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof_a.id, service_id=service.id, start_at=_dt(10))],
        ),
    )
    no_perms_actor = _actor(session, org_id, permissions=set())
    items = agenda.list_agenda(session, no_perms_actor, date_from=_dt(0), date_to=_dt(23, 59))
    assert items == []


def test_filtro_por_profissional_e_status(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all", "agenda.edit"})
    appt_a = appointments.create_appointment(
        session, owner,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof_a.id, service_id=service.id, start_at=_dt(10))],
        ),
    )
    appointments.create_appointment(
        session, owner,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof_b.id, service_id=service.id, start_at=_dt(14))],
        ),
    )

    items = agenda.list_agenda(
        session, owner, date_from=_dt(0), date_to=_dt(23, 59), professional_id=prof_a.id,
    )
    assert len(items) == 1
    assert items[0].professional_id == prof_a.id

    from nexasalon_api.models.enums import AppointmentStatus
    appointments.update_status(session, owner, appt_a.id, AppointmentStatus.CONFIRMED)
    confirmed = agenda.list_agenda(
        session, owner, date_from=_dt(0), date_to=_dt(23, 59), status=AppointmentStatus.CONFIRMED,
    )
    assert len(confirmed) == 1
    assert confirmed[0].professional_id == prof_a.id


def test_isolamento_entre_organizacoes(org_session):
    session, org_id = org_session
    branch, prof_a, prof_b, service, client = _setup_two_professionals(session, org_id)
    owner = _actor(session, org_id, permissions={"agenda.create", "agenda.view_all"})
    appointments.create_appointment(
        session, owner,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof_a.id, service_id=service.id, start_at=_dt(10))],
        ),
    )
    session.flush()

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_actor = _actor(other_session, other_org_id, permissions={"agenda.view_all"})
        items = agenda.list_agenda(other_session, other_actor, date_from=_dt(0), date_to=_dt(23, 59))
        assert items == []
        other_session.rollback()
