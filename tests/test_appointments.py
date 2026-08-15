"""Testes do serviço de Appointment (criação/edição/status/cancelamento)
— Etapa 3A. Mesma abordagem de `test_availability.py`: direto no
service layer via `SessionLocal`, sem rota HTTP (rotas ainda não
existem — próximo passo)."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationDomainError
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.audit import AuditLog
from nexasalon_api.models.client import Client
from nexasalon_api.models.identity import User
from nexasalon_api.models.enums import AppointmentStatus, ScheduleBlockScope, ScheduleBlockType
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, ScheduleBlock, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate, AppointmentReplace
from nexasalon_api.services import appointments
from nexasalon_api.services.appointment_state_machine import next_status

_ALL_AGENDA_PERMS = frozenset(
    {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel", "agenda.force_overlap"}
)
_THURSDAY = date(2026, 8, 13)
_OUR_THURSDAY = 4  # 0=domingo..6=sábado; 2026-08-13 é quinta.
_TZ = timezone(timedelta(hours=-3))


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org appointments", slug=f"org-appt-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_ALL_AGENDA_PERMS, professional_id=None) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions), professional_id=professional_id,
    )


def _branch(session, org_id) -> Branch:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _professional(session, org_id, branch_id=None, name="Profissional") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Corte", duration=60, price=100) -> Service:
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


def _setup_basic(session, org_id):
    """Um profissional + serviço prontos pra agendar, jornada 09-18."""
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    service = _service(session, org_id, duration=60, price=100)
    _link(session, prof.id, service.id)
    _working_hours(session, org_id, prof.id, _OUR_THURSDAY, time(9, 0), time(18, 0))
    client = _client(session, org_id)
    session.flush()
    return branch, prof, service, client


def test_criar_agendamento_simples(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert len(appt.items) == 1
    item = appt.items[0]
    assert item.duration_minutes == 60
    assert item.price == Decimal("100.00")
    assert item.start_at == _dt(14, 0)
    assert item.end_at == _dt(15, 0)
    assert appt.starts_at == _dt(14, 0)
    assert appt.ends_at == _dt(15, 0)


def test_criar_agendamento_multiplos_servicos_e_profissionais(org_session):
    """Réplica do exemplo do enunciado: Cliente Maria, 14:00-16:00
    Manutenção com Ianka, 16:00-17:30 Mechas com João."""
    session, org_id = org_session
    branch = _branch(session, org_id)
    ianka = _professional(session, org_id, branch.id, name="Ianka")
    joao = _professional(session, org_id, branch.id, name="João")
    manutencao = _service(session, org_id, name="Manutenção", duration=120, price=150)
    mechas = _service(session, org_id, name="Mechas", duration=90, price=200)
    _link(session, ianka.id, manutencao.id)
    _link(session, joao.id, mechas.id)
    _working_hours(session, org_id, ianka.id, _OUR_THURSDAY, time(9, 0), time(19, 0))
    _working_hours(session, org_id, joao.id, _OUR_THURSDAY, time(9, 0), time(19, 0))
    client = _client(session, org_id, name="Maria")
    session.flush()
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[
            AppointmentItemCreate(professional_id=ianka.id, service_id=manutencao.id, start_at=_dt(14, 0)),
            AppointmentItemCreate(professional_id=joao.id, service_id=mechas.id, start_at=_dt(16, 0)),
        ],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert len(appt.items) == 2
    assert appt.starts_at == _dt(14, 0)
    assert appt.ends_at == _dt(17, 30)


def test_profissional_de_outra_org_gera_404(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    other_org_id = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-{other_org_id.hex[:8]}"))
        other_session.flush()
        alien_prof = _professional(other_session, other_org_id, name="Alien")
        other_session.flush()
        alien_prof_id = alien_prof.id
        other_session.rollback()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=alien_prof_id, service_id=service.id, start_at=_dt(14, 0))],
    )
    with pytest.raises(NotFoundError):
        appointments.create_appointment(session, actor, data)


def test_horario_fora_da_jornada_gera_erro(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(20, 0))],
    )
    with pytest.raises(ValidationDomainError):
        appointments.create_appointment(session, actor, data)


def test_conflito_com_schedule_block_gera_erro(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    session.add(
        ScheduleBlock(
            organization_id=org_id, scope=ScheduleBlockScope.PROFESSIONAL, professional_id=prof.id,
            block_type=ScheduleBlockType.LUNCH, title="Almoço", start_at=_dt(14, 0), end_at=_dt(15, 0),
        )
    )
    session.flush()
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 30))],
    )
    with pytest.raises(ValidationDomainError):
        appointments.create_appointment(session, actor, data)


def test_conflito_com_agendamento_existente_gera_409(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    first = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appointments.create_appointment(session, actor, first)

    second = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 30))],
    )
    with pytest.raises(ConflictError):
        appointments.create_appointment(session, actor, second)


def test_force_overlap_sem_permissao_gera_403_mesmo_sem_conflito_real(org_session):
    """Ajuste pós-revisão: `force_overlap=true` sem a permission é
    recusado com 403 EXPLÍCITO — mesmo quando não há NENHUM conflito de
    verdade (ou seja, não é o 409 de conflito que está pegando isso por
    acidente; é uma checagem de permissão dedicada, e a mais cedo
    possível)."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor_sem_forcar = _actor(session, org_id, permissions=_ALL_AGENDA_PERMS - {"agenda.force_overlap"})

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id, force_overlap=True,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    with pytest.raises(ForbiddenError):
        appointments.create_appointment(session, actor_sem_forcar, data)

    # nada foi criado — o 403 aconteceu ANTES de qualquer inserção.
    logs = session.query(AuditLog).filter(AuditLog.organization_id == org_id).all()
    assert logs == []


def test_force_overlap_sem_permissao_com_conflito_real_tambem_e_403(org_session):
    """Com conflito real de verdade envolvido, o erro ainda é 403 (falha
    de permissão) — não 409 (conflito) — porque a checagem de permissão
    acontece antes da checagem de conflito."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor_full = _actor(session, org_id)
    first = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appointments.create_appointment(session, actor_full, first)

    actor_sem_forcar = _actor(session, org_id, permissions=_ALL_AGENDA_PERMS - {"agenda.force_overlap"})
    second = AppointmentCreate(
        branch_id=branch.id, client_id=client.id, force_overlap=True,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 30))],
    )
    with pytest.raises(ForbiddenError):
        appointments.create_appointment(session, actor_sem_forcar, second)


def test_force_overlap_false_com_conflito_real_continua_409(org_session):
    """Sem `force_overlap` nenhum (o caso comum), um conflito real
    continua sendo 409 normalmente — o ajuste não mudou esse caminho."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    first = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appointments.create_appointment(session, actor, first)

    second = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 30))],
    )
    with pytest.raises(ConflictError):
        appointments.create_appointment(session, actor, second)


def test_force_overlap_com_permissao_funciona_e_audita(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    first = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appointments.create_appointment(session, actor, first)

    second_data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id, force_overlap=True,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 30))],
    )
    second = appointments.create_appointment(session, actor, second_data)
    assert len(second.items) == 1

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == second.id
    ).all()
    change_types = {log.new_values.get("change_type") for log in logs if log.new_values}
    assert "force_overlap" in change_types


def test_snapshot_preserva_preco_e_duracao_apos_mudanca_no_service(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    original_price = appt.items[0].price
    original_duration = appt.items[0].duration_minutes

    service.default_price = 999
    service.default_duration_minutes = 15
    session.flush()

    reloaded = appointments.get_appointment(session, actor, appt.id)
    assert reloaded.items[0].price == original_price
    assert reloaded.items[0].duration_minutes == original_duration


def test_view_own_so_ve_proprio_agendamento(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    owner_actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, owner_actor, data)

    other_prof = _professional(session, org_id, branch.id, name="Outro")
    session.flush()
    view_own_actor = _actor(
        session, org_id, permissions={"agenda.view_own", "agenda.edit"}, professional_id=other_prof.id
    )
    with pytest.raises(NotFoundError):
        appointments.get_appointment(session, view_own_actor, appt.id)

    same_prof_actor = _actor(session, org_id, permissions={"agenda.view_own"}, professional_id=prof.id)
    found = appointments.get_appointment(session, same_prof_actor, appt.id)
    assert found.id == appt.id


def test_view_all_ve_qualquer_agendamento(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    owner_actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, owner_actor, data)

    receptionist_actor = _actor(session, org_id, permissions={"agenda.view_all"}, professional_id=None)
    found = appointments.get_appointment(session, receptionist_actor, appt.id)
    assert found.id == appt.id


def test_transicao_de_status_valida(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.status == AppointmentStatus.SCHEDULED

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.CONFIRMED)
    assert updated.status == AppointmentStatus.CONFIRMED


def test_transicao_finished_para_in_progress_e_bloqueada(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()

    with pytest.raises(ValidationDomainError):
        appointments.update_status(session, actor, appt.id, AppointmentStatus.IN_PROGRESS)


def test_patch_generico_nao_cancela(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    with pytest.raises(ValidationDomainError):
        appointments.update_status(session, actor, appt.id, AppointmentStatus.CANCELLED)


def test_cancelamento_funciona_e_audita(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    cancelled = appointments.cancel_appointment(session, actor, appt.id)
    assert cancelled.status == AppointmentStatus.CANCELLED

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == appt.id
    ).all()
    change_types = {log.new_values.get("change_type") for log in logs if log.new_values}
    assert "cancel" in change_types


def test_cancelar_finished_gera_erro(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()

    with pytest.raises(ValidationDomainError):
        appointments.cancel_appointment(session, actor, appt.id)


def test_transicao_finished_para_paid_e_permitida(org_session):
    # Item "padronizar 8 status oficiais" — FINISHED -> PAID continua
    # sendo uma transição manual válida (PATCH genérico de status). A
    # Comanda (`services/orders.py`, `test_orders.py`) reaproveita esta
    # MESMA validação pra promover automaticamente ao fechar o
    # pagamento — não duplica a regra.
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.PAID)
    assert updated.status == AppointmentStatus.PAID


def test_paid_e_terminal_sem_transicoes_de_saida(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.PAID
    session.flush()

    with pytest.raises(ValidationDomainError):
        appointments.update_status(session, actor, appt.id, AppointmentStatus.FINISHED)

    with pytest.raises(ValidationDomainError):
        appointments.cancel_appointment(session, actor, appt.id)


def test_reagendamento_via_put_gera_auditoria_de_reschedule(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)

    replace_data = AppointmentReplace(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(16, 0))],
    )
    updated = appointments.replace_appointment(session, actor, appt.id, replace_data)
    assert updated.items[0].start_at == _dt(16, 0)

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == appt.id, AuditLog.action == "update"
    ).all()
    all_change_types = set()
    for log in logs:
        ct = log.new_values.get("change_type") if log.new_values else None
        if isinstance(ct, list):
            all_change_types.update(ct)
        elif ct:
            all_change_types.add(ct)
    assert "reschedule" in all_change_types


def test_troca_de_profissional_via_put_gera_auditoria(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    other_prof = _professional(session, org_id, branch.id, name="Outro")
    _link(session, other_prof.id, service.id)
    _working_hours(session, org_id, other_prof.id, _OUR_THURSDAY, time(9, 0), time(18, 0))
    session.flush()
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)

    replace_data = AppointmentReplace(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=other_prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    updated = appointments.replace_appointment(session, actor, appt.id, replace_data)
    assert updated.items[0].professional_id == other_prof.id

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == appt.id, AuditLog.action == "update"
    ).all()
    all_change_types = set()
    for log in logs:
        ct = log.new_values.get("change_type") if log.new_values else None
        if isinstance(ct, list):
            all_change_types.update(ct)
        elif ct:
            all_change_types.add(ct)
    assert "professional_change" in all_change_types


def test_auditoria_de_criacao_registrada(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == appt.id, AuditLog.action == "create"
    ).all()
    assert len(logs) == 1
