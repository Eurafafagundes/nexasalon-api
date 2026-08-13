"""Testes do serviço de disponibilidade (Etapa 3A).

Vai direto no `SessionLocal`/`services.availability`, sem passar por rota
HTTP — ainda não existem rotas de agenda (isso é o próximo passo, #55).
Cada teste seta `app.current_org_id` manualmente pra respeitar RLS, igual
ao `seed_organization` do conftest.
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.organization import Branch
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus, ScheduleBlockScope, ScheduleBlockType
from nexasalon_api.models.organization import Organization
from nexasalon_api.models.professional import Professional, ScheduleBlock, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.services import availability


@pytest.fixture()
def org_session():
    """Sessão com `app.current_org_id` setado — RLS liberado — devolve
    (session, organization_id). Cada teste cria sua própria org, então
    não há necessidade de rollback entre testes (bancos isolados por org)."""
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org disponibilidade", slug=f"org-disp-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _make_branch(session, org_id, timezone_name: str | None = None) -> Branch:
    branch = Branch(
        organization_id=org_id,
        name="Unidade Teste",
        slug=f"unidade-{uuid.uuid4().hex[:8]}",
        timezone=timezone_name,
    )
    session.add(branch)
    session.flush()
    return branch


def _make_professional(session, org_id, branch_id=None) -> Professional:
    prof = Professional(organization_id=org_id, branch_id=branch_id, name="Profissional Teste")
    session.add(prof)
    session.flush()
    return prof


def _make_service(session, org_id, duration=60, price=100) -> Service:
    svc = Service(organization_id=org_id, name="Corte", default_duration_minutes=duration, default_price=price)
    session.add(svc)
    session.flush()
    return svc


def _link(session, professional_id, service_id, duration_override=None, price_override=None) -> ProfessionalService:
    ps = ProfessionalService(
        professional_id=professional_id,
        service_id=service_id,
        duration_override_minutes=duration_override,
        price_override=price_override,
    )
    session.add(ps)
    session.flush()
    return ps


def _working_hours(session, org_id, professional_id, weekday, start, end):
    wh = WorkingHours(
        organization_id=org_id, professional_id=professional_id, weekday=weekday, start_time=start, end_time=end
    )
    session.add(wh)
    session.flush()
    return wh


# nossa convenção do domínio: 0=domingo..6=sábado. 2026-08-13 é quinta-feira
# (python weekday=3) -> nossa convenção = (3+1)%7 = 4 = quinta.
_THURSDAY = date(2026, 8, 13)
_OUR_THURSDAY = 4


def test_disponibilidade_normal_sem_conflitos(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id, duration=60)
    _link(session, prof.id, service.id)
    _working_hours(session, org_id, prof.id, _OUR_THURSDAY, time(9, 0), time(12, 0))
    session.flush()

    slots = availability.compute_availability(
        session,
        org_id,
        branch_id=branch.id,
        professional_id=prof.id,
        service_id=service.id,
        target_date=_THURSDAY,
        slot_minutes=15,
    )
    assert len(slots) > 0
    # janela 09:00-12:00, serviço de 60min, grade de 15min -> últimos
    # candidatos possíveis começam às 11:00 (11:00+60=12:00 cabe).
    starts = sorted(s.start_at.time() for s in slots)
    assert starts[0] == time(9, 0)
    assert starts[-1] == time(11, 0)
    # a grade não altera a duração real do serviço:
    for s in slots:
        assert (s.end_at - s.start_at) == timedelta(minutes=60)


def test_duracao_especifica_do_professional_service(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id, duration=60)
    _link(session, prof.id, service.id, duration_override=30)
    _working_hours(session, org_id, prof.id, _OUR_THURSDAY, time(9, 0), time(10, 0))
    session.flush()

    slots = availability.compute_availability(
        session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
        target_date=_THURSDAY, slot_minutes=15,
    )
    # com override de 30min numa janela de 1h (09:00-10:00), o último
    # início possível é 09:30 (09:30+30=10:00 cabe; 09:45+30=10:15 não cabe).
    starts = sorted(s.start_at.time() for s in slots)
    assert starts == [time(9, 0), time(9, 15), time(9, 30)]
    for s in slots:
        assert (s.end_at - s.start_at) == timedelta(minutes=30)


def test_fora_da_jornada_sem_working_hours_no_dia(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id)
    _link(session, prof.id, service.id)
    # jornada só na sexta (5), não na quinta (4) -> nenhum slot na quinta.
    _working_hours(session, org_id, prof.id, 5, time(9, 0), time(18, 0))
    session.flush()

    slots = availability.compute_availability(
        session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
        target_date=_THURSDAY,
    )
    assert slots == []


def test_schedule_block_remove_intervalo(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id, duration=60)
    _link(session, prof.id, service.id)
    _working_hours(session, org_id, prof.id, _OUR_THURSDAY, time(9, 0), time(12, 0))
    block_start = datetime(2026, 8, 13, 10, 0, tzinfo=timezone(timedelta(hours=-3)))
    block_end = datetime(2026, 8, 13, 11, 0, tzinfo=timezone(timedelta(hours=-3)))
    session.add(
        ScheduleBlock(
            organization_id=org_id,
            scope=ScheduleBlockScope.PROFESSIONAL,
            professional_id=prof.id,
            block_type=ScheduleBlockType.OTHER,
            title="Bloqueio",
            start_at=block_start,
            end_at=block_end,
        )
    )
    session.flush()

    slots = availability.compute_availability(
        session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
        target_date=_THURSDAY, slot_minutes=30,
    )
    starts = sorted(s.start_at.time() for s in slots)
    # bloqueio 10:00-11:00 corta a janela 09:00-12:00 em [09:00-10:00) e [11:00-12:00)
    # com serviço de 60min só cabe 09:00 no primeiro pedaço, e 11:00 no segundo.
    assert time(10, 0) not in starts
    assert time(9, 0) in starts
    assert time(11, 0) in starts


def test_agendamento_existente_gera_conflito_na_disponibilidade(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id, duration=60)
    _link(session, prof.id, service.id)
    _working_hours(session, org_id, prof.id, _OUR_THURSDAY, time(9, 0), time(12, 0))
    client = Client(organization_id=org_id, name="Cliente Teste")
    session.add(client)
    session.flush()
    appt = Appointment(organization_id=org_id, branch_id=branch.id, client_id=client.id, status=AppointmentStatus.SCHEDULED)
    session.add(appt)
    session.flush()
    item = AppointmentItem(
        organization_id=org_id,
        appointment_id=appt.id,
        service_id=service.id,
        professional_id=prof.id,
        start_at=datetime(2026, 8, 13, 9, 0, tzinfo=timezone(timedelta(hours=-3))),
        end_at=datetime(2026, 8, 13, 10, 0, tzinfo=timezone(timedelta(hours=-3))),
        duration_minutes=60,
        price=100,
    )
    session.add(item)
    session.flush()

    slots = availability.compute_availability(
        session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
        target_date=_THURSDAY, slot_minutes=15,
    )
    starts = sorted(s.start_at.time() for s in slots)
    assert time(9, 0) not in starts
    assert time(10, 0) in starts


def test_service_nao_prestado_pelo_profissional_gera_erro(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id)
    session.flush()  # sem ProfessionalService

    with pytest.raises(ValidationDomainError):
        availability.compute_availability(
            session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
            target_date=_THURSDAY,
        )


def test_profissional_de_outra_unidade_gera_erro(org_session):
    session, org_id = org_session
    branch1 = _make_branch(session, org_id)
    branch2 = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch1.id)
    service = _make_service(session, org_id)
    _link(session, prof.id, service.id)
    session.flush()

    with pytest.raises(ValidationDomainError):
        availability.compute_availability(
            session, org_id, branch_id=branch2.id, professional_id=prof.id, service_id=service.id,
            target_date=_THURSDAY,
        )


def test_profissional_inexistente_gera_404(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    service = _make_service(session, org_id)
    session.flush()

    with pytest.raises(NotFoundError):
        availability.compute_availability(
            session, org_id, branch_id=branch.id, professional_id=uuid.uuid4(), service_id=service.id,
            target_date=_THURSDAY,
        )


def test_slot_minutes_invalido_gera_erro(org_session):
    session, org_id = org_session
    branch = _make_branch(session, org_id)
    prof = _make_professional(session, org_id, branch.id)
    service = _make_service(session, org_id)
    _link(session, prof.id, service.id)
    session.flush()

    with pytest.raises(ValidationDomainError):
        availability.compute_availability(
            session, org_id, branch_id=branch.id, professional_id=prof.id, service_id=service.id,
            target_date=_THURSDAY, slot_minutes=7,
        )
