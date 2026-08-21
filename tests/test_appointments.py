"""Testes do serviço de Appointment (criação/edição/status/cancelamento)
— Etapa 3A. Mesma abordagem de `test_availability.py`: direto no
service layer via `SessionLocal`, sem rota HTTP (rotas ainda não
existem — próximo passo)."""
import dataclasses
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.models.audit import AuditLog
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    OrderStatus,
    ScheduleBlockScope,
    ScheduleBlockType,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, ScheduleBlock, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import (
    AppointmentCreate,
    AppointmentItemCreate,
    AppointmentItemUpdate,
    AppointmentReplace,
)
from nexasalon_api.services import appointment_state_machine, appointments, cash_register, orders
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


def test_price_override_substitui_preco_do_catalogo_sem_alterar_service(org_session):
    """Item "valor editável por serviço": `price_override` vale só PARA
    ESTE agendamento — o catálogo (`Service.default_price`) não muda."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[
            AppointmentItemCreate(
                professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0),
                price_override=Decimal("77.50"),
            )
        ],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.items[0].price == Decimal("77.50")
    # duração continua vindo do catálogo — override é só de preço.
    assert appt.items[0].duration_minutes == 60
    session.refresh(service)
    assert service.default_price == 100


def test_sem_price_override_usa_preco_efetivo_do_catalogo(org_session):
    """Comportamento antigo preservado: sem `price_override`, o preço
    continua vindo de `effective_duration_and_price` normalmente."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.items[0].price == Decimal("100.00")


def test_price_override_negativo_e_rejeitado():
    with pytest.raises(Exception):
        AppointmentItemCreate(
            professional_id=uuid.uuid4(), service_id=uuid.uuid4(),
            start_at=_dt(14, 0), price_override=Decimal("-1"),
        )


def test_fit_in_e_gravado_mas_nao_pula_nenhuma_validacao(org_session):
    """Item "encaixe conservador": `fit_in=true` é gravado no
    agendamento, mas jornada/bloqueio/conflito continuam validando
    exatamente igual — encaixe NÃO é um passe livre."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id, fit_in=True,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.fit_in is True

    # Um segundo "encaixe" sobre o MESMO horário continua 409 — fit_in
    # não contorna a checagem de conflito (só force_overlap+permissão
    # faz isso, e isto aqui não usa force_overlap).
    conflicting = AppointmentCreate(
        branch_id=branch.id, client_id=client.id, fit_in=True,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    with pytest.raises(ConflictError):
        appointments.create_appointment(session, actor, conflicting)


def test_fit_in_default_e_false(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.fit_in is False


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


def test_regressao_manual_de_status_operacional_e_permitida(org_session):
    """Item "status livre": não existe mais sequência linear obrigatória
    entre os 6 status operacionais — regredir manualmente (ex.:
    FINISHED -> IN_PROGRESS, CONFIRMED -> SCHEDULED, IN_PROGRESS ->
    WAITING) é permitido pra quem tem `agenda.edit`, sem precisar de
    nenhum fluxo especial."""
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

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.IN_PROGRESS)
    assert updated.status == AppointmentStatus.IN_PROGRESS

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.CONFIRMED)
    assert updated.status == AppointmentStatus.CONFIRMED

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.SCHEDULED)
    assert updated.status == AppointmentStatus.SCHEDULED


def test_avanco_direto_sem_passar_pelos_intermediarios_e_permitido(org_session):
    """Item "status livre": avançar direto (ex.: SCHEDULED -> FINISHED,
    pulando CONFIRMED/WAITING/IN_PROGRESS) também é permitido — o grafo
    é livre nos dois sentidos, não só "avançar em ordem"."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.status == AppointmentStatus.SCHEDULED

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.FINISHED)
    assert updated.status == AppointmentStatus.FINISHED


def test_no_show_tambem_e_regressivel_manualmente(org_session):
    """Exemplo explícito do pedido: "Faltou -> Confirmado" precisa ser
    permitido manualmente quando fizer sentido — NO_SHOW não é mais um
    terminal do grafo livre (só PAID e CANCELLED continuam sendo)."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.NO_SHOW
    session.flush()

    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.CONFIRMED)
    assert updated.status == AppointmentStatus.CONFIRMED


def test_auditoria_de_mudanca_manual_de_status_e_registrada(org_session):
    """Preserva auditoria (item explícito do pedido) mesmo com o grafo
    livre — cada mudança manual de status continua gerando um
    `AuditLog` com o status antigo/novo."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.CONFIRMED
    session.flush()

    appointments.update_status(session, actor, appt.id, AppointmentStatus.SCHEDULED)

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == appt.id
    ).all()
    manual_logs = [log for log in logs if log.new_values and log.new_values.get("change_type") == "manual_status_change"]
    assert len(manual_logs) == 1
    assert manual_logs[0].old_values == {"status": "confirmed"}
    assert manual_logs[0].new_values["status"] == "scheduled"


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


def test_patch_generico_nao_marca_pago_manualmente(org_session):
    """Item "não misture status operacional com status financeiro":
    `PAID` nunca é um destino do PATCH genérico, nem a partir de
    FINISHED — só chega lá via `POST /orders/{id}/close`
    (`test_orders.py`)."""
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
        appointments.update_status(session, actor, appt.id, AppointmentStatus.PAID)


def test_paid_e_terminal_sem_transicoes_de_saida(org_session):
    """Item "Pago é diferente": nenhuma regressão/avanço manual sai de
    `PAID` — desfazer exige um fluxo financeiro de estorno, não
    implementado nesta rodada."""
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
        appointments.update_status(session, actor, appt.id, AppointmentStatus.SCHEDULED)

    with pytest.raises(ValidationDomainError):
        appointments.cancel_appointment(session, actor, appt.id)


def test_mark_paid_promove_de_qualquer_status_operacional(org_session):
    """`appointments.mark_paid` (chamado só por `services/orders.py`) não
    exige `FINISHED` — item "não condicione a Comanda a ter passado por
    todos os status"."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.status == AppointmentStatus.SCHEDULED

    updated = appointments.mark_paid(session, actor, appt.id)
    assert updated.status == AppointmentStatus.PAID

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == appt.id
    ).all()
    change_types = {log.new_values.get("change_type") for log in logs if log.new_values}
    assert "payment_settled" in change_types


def test_mark_paid_recusa_agendamento_ja_pago_ou_cancelado(org_session):
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
        appointments.mark_paid(session, actor, appt.id)

    data2 = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(16, 0))],
    )
    appt2 = appointments.create_appointment(session, actor, data2)
    appt2.status = AppointmentStatus.CANCELLED
    session.flush()
    with pytest.raises(ValidationDomainError):
        appointments.mark_paid(session, actor, appt2.id)


def test_state_machine_next_status_grafo_livre_operacional(org_session):
    """Unidade pura da máquina de estados: qualquer par operacional é
    válido nos dois sentidos; `CANCELLED`/`PAID` como alvo e `PAID`
    como origem continuam recusados."""
    session, org_id = org_session  # fixture só pra manter o padrão do arquivo, não usada aqui.
    assert next_status(AppointmentStatus.IN_PROGRESS, AppointmentStatus.WAITING) == AppointmentStatus.WAITING
    assert next_status(AppointmentStatus.NO_SHOW, AppointmentStatus.CONFIRMED) == AppointmentStatus.CONFIRMED

    with pytest.raises(ValidationDomainError):
        next_status(AppointmentStatus.SCHEDULED, AppointmentStatus.CANCELLED)
    with pytest.raises(ValidationDomainError):
        next_status(AppointmentStatus.FINISHED, AppointmentStatus.PAID)
    with pytest.raises(ValidationDomainError):
        next_status(AppointmentStatus.PAID, AppointmentStatus.SCHEDULED)
    with pytest.raises(ValidationDomainError):
        next_status(AppointmentStatus.CANCELLED, AppointmentStatus.SCHEDULED)

    assert appointment_state_machine.mark_paid(AppointmentStatus.SCHEDULED) == AppointmentStatus.PAID
    with pytest.raises(ValidationDomainError):
        appointment_state_machine.mark_paid(AppointmentStatus.PAID)
    with pytest.raises(ValidationDomainError):
        appointment_state_machine.mark_paid(AppointmentStatus.CANCELLED)


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


# ---------------------------------------------------------------------
# PATCH /appointments/{id}/items/{item_id} — Etapa F, item "editar valor
# e duração no agendamento" + "drag and drop" (services/appointments.py::
# update_appointment_item). Edição EM PLACE de um item já existente
# (nunca apaga+recria como o PUT), pra sobreviver a uma Comanda aberta
# já linkada (FK RESTRICT em `OrderItem.appointment_item_id`).
# ---------------------------------------------------------------------


def test_editar_preco_do_item_sem_motivo_e_recusado(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    item = appt.items[0]

    with pytest.raises(ValidationDomainError):
        appointments.update_appointment_item(
            session, actor, appt.id, item.id, AppointmentItemUpdate(price_override=Decimal("310.00")),
        )


def test_editar_preco_e_duracao_do_item_com_motivo_aplica_recalcula_ends_at_e_audita(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    item = appt.items[0]
    assert item.price == Decimal("100.00")
    assert item.duration_minutes == 60

    updated = appointments.update_appointment_item(
        session, actor, appt.id, item.id,
        AppointmentItemUpdate(price_override=Decimal("310.00"), duration_override=90, reason="Serviço adicional"),
    )
    updated_item = updated.items[0]
    assert updated_item.price == Decimal("310.00")
    assert updated_item.duration_minutes == 90
    # ends_at recalculado a partir da NOVA duração (trigger recalc_appointment_bounds).
    assert updated_item.end_at == _dt(14, 0) + timedelta(minutes=90)
    assert updated.ends_at == _dt(14, 0) + timedelta(minutes=90)

    # Nunca escreve de volta no catálogo.
    session.refresh(service)
    assert service.default_price == 100

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == item.id, AuditLog.entity_type == "appointment_item",
    ).all()
    assert len(logs) == 1
    change_types = logs[0].new_values["change_type"]
    assert "manual_price_edit" in change_types
    assert "manual_duration_edit" in change_types
    assert logs[0].old_values["price"] == "100.00"
    assert logs[0].new_values["reason"] == "Serviço adicional"


def test_editar_apenas_duracao_sem_mudar_preco_nao_exige_motivo(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    item = appt.items[0]

    updated = appointments.update_appointment_item(
        session, actor, appt.id, item.id, AppointmentItemUpdate(duration_override=45),
    )
    assert updated.items[0].duration_minutes == 45
    assert updated.items[0].price == Decimal("100.00")


def test_mover_horario_recalcula_e_detecta_conflito_com_outro_agendamento(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(10, 0))],
        ),
    )
    other_client = _client(session, org_id, "Outra Cliente")
    other_appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=other_client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(15, 0))],
        ),
    )

    # Move (drag-and-drop) o primeiro pra cima do horário do segundo -> conflito.
    with pytest.raises(ConflictError):
        appointments.update_appointment_item(
            session, actor, appt.id, appt.items[0].id, AppointmentItemUpdate(start_at=_dt(15, 30)),
        )

    # Um horário livre funciona normalmente e recalcula end_at.
    moved = appointments.update_appointment_item(
        session, actor, appt.id, appt.items[0].id, AppointmentItemUpdate(start_at=_dt(11, 0)),
    )
    assert moved.items[0].start_at == _dt(11, 0)
    assert moved.items[0].end_at == _dt(12, 0)
    assert other_appt.id != appt.id  # sanity


def test_mover_para_profissional_sem_permissao_de_edicao_e_bloqueado(org_session):
    """Item explícito: "visualizar" != "editar" — ver a agenda da Ianka
    não autoriza mover um atendimento PRA ELA."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    other_prof = _professional(session, org_id, branch.id, name="Ianka")
    _link(session, other_prof.id, service.id)
    _working_hours(session, org_id, other_prof.id, _OUR_THURSDAY, time(9, 0), time(18, 0))
    actor_full = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor_full,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )

    # Ator só ENXERGA os dois profissionais, mas só EDITA o primeiro.
    base_restricted = _actor(
        session, org_id,
        permissions=frozenset({"agenda.view_own", "agenda.view_all", "agenda.edit"}),
    )
    restricted = dataclasses.replace(
        base_restricted,
        agenda_viewable_professional_ids=frozenset({prof.id, other_prof.id}),
        agenda_editable_professional_ids=frozenset({prof.id}),
    )

    with pytest.raises(ForbiddenError):
        appointments.update_appointment_item(
            session, restricted, appt.id, appt.items[0].id,
            AppointmentItemUpdate(professional_id=other_prof.id),
        )


def test_mover_para_profissional_que_nao_faz_o_servico_e_bloqueado(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    other_prof = _professional(session, org_id, branch.id, name="Sem o serviço")
    _working_hours(session, org_id, other_prof.id, _OUR_THURSDAY, time(9, 0), time(18, 0))
    # NUNCA vinculado ao serviço (sem `_link`).
    actor = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )

    with pytest.raises(ValidationDomainError):
        appointments.update_appointment_item(
            session, actor, appt.id, appt.items[0].id, AppointmentItemUpdate(professional_id=other_prof.id),
        )


def test_faltou_libera_o_horario_para_um_novo_agendamento(org_session):
    """Item "Faltou libera o horário" — `OCCUPYING_STATUSES` já exclui
    NO_SHOW da checagem de conflito; este teste confirma isso na
    ÍNTEGRA (criar -> marcar NO_SHOW -> criar outro no MESMO horário/
    profissional não conflita)."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    first = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    appointments.update_status(session, actor, first.id, AppointmentStatus.NO_SHOW)

    other_client = _client(session, org_id, "Segunda Cliente")
    second = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=other_client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    assert second.items[0].start_at == _dt(14, 0)

    # O agendamento faltoso continua existindo (histórico), só não ocupa mais.
    session.refresh(first)
    assert first.status == AppointmentStatus.NO_SHOW


def test_faltou_permanece_no_historico_do_appointment(org_session):
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    updated = appointments.update_status(session, actor, appt.id, AppointmentStatus.NO_SHOW)
    assert updated.status == AppointmentStatus.NO_SHOW
    # Nada foi apagado — segue recuperável por id, com os itens intactos.
    reloaded = appointments.get_appointment(session, actor, appt.id)
    assert reloaded.status == AppointmentStatus.NO_SHOW
    assert len(reloaded.items) == 1


def test_editar_item_com_comanda_aberta_sincroniza_order_item(org_session):
    """Regra ÚNICA do pedido: Agenda e Comanda nunca divergem em
    silêncio enquanto a comanda está ABERTA — editar o item da Agenda
    reflete automaticamente na linha correspondente da Comanda."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(session, org_id, permissions=_ALL_AGENDA_PERMS | frozenset({"orders.manage"}))
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    order = orders.create_order(session, actor, appt.id)
    order_item = order.items[0]
    assert order_item.price == Decimal("100.00")

    appointments.update_appointment_item(
        session, actor, appt.id, appt.items[0].id,
        AppointmentItemUpdate(price_override=Decimal("150.00"), duration_override=75, reason="Ajuste"),
    )

    session.refresh(order_item)
    assert order_item.price == Decimal("150.00")
    assert order_item.duration_minutes == 75

    sync_logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == order_item.id, AuditLog.entity_type == "order_item",
    ).all()
    assert any(log.new_values.get("change_type") == "synced_from_appointment_edit" for log in sync_logs)


def test_editar_preco_com_comanda_fechada_e_bloqueado(org_session):
    """Item explícito: comanda fechada/paga nunca sofre alteração
    financeira silenciosa vinda da Agenda."""
    session, org_id = org_session
    branch, prof, service, client = _setup_basic(session, org_id)
    actor = _actor(
        session, org_id,
        permissions=_ALL_AGENDA_PERMS | frozenset({"orders.manage", "orders.view", "payments.register"}),
    )
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(14, 0))],
        ),
    )
    order = orders.create_order(session, actor, appt.id)
    # Fecha a comanda direto no banco (sem depender do fluxo de Caixa,
    # que não é o que este teste está verificando).
    order.status = OrderStatus.CLOSED
    order.closed_at = datetime.now(timezone.utc)
    session.flush()

    with pytest.raises(ValidationDomainError):
        appointments.update_appointment_item(
            session, actor, appt.id, appt.items[0].id,
            AppointmentItemUpdate(price_override=Decimal("999.00"), reason="Tentativa pós-fechamento"),
        )

    # Duração/horário continuam livres (não são alteração financeira).
    moved = appointments.update_appointment_item(
        session, actor, appt.id, appt.items[0].id, AppointmentItemUpdate(duration_override=90),
    )
    assert moved.items[0].duration_minutes == 90
