"""Testes de Status Personalizado de Agendamento (migration 0039,
`AppointmentCustomStatus`/`Appointment.custom_status_id`). Cobre:
CRUD do catálogo; atribuir/trocar/remover num agendamento; isolamento
por organização; desativado some da listagem padrão; NUNCA toca o
enum/estado operacional `AppointmentStatus`."""
import uuid
from datetime import time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.appointment_custom_status import (
    AppointmentCustomStatusCreate,
    AppointmentCustomStatusUpdate,
)
from nexasalon_api.services import appointment_custom_statuses as custom_statuses
from nexasalon_api.services import appointments

_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4
_PERMS = frozenset({"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel"})


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org status", slug=f"org-status-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _dt(hour, minute=0):
    from datetime import datetime

    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _appointment(session, org_id, actor):
    branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(branch)
    session.flush()
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
    return appointments.create_appointment(session, actor, data)


# ---------------------------------------------------------------------
# CRUD do catálogo
# ---------------------------------------------------------------------


def test_criar_listar_editar_desativar_status_personalizado(org_session):
    session, org_id = org_session

    created = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Retorno", color_hex="#8B5CF6")
    )
    assert created.name == "Retorno"
    assert created.is_active is True

    active = custom_statuses.list_custom_statuses(session, org_id)
    assert len(active) == 1

    updated = custom_statuses.update_custom_status(
        session, org_id, created.id, AppointmentCustomStatusUpdate(name="Retorno VIP", color_hex="#F59E0B", sort_order=1)
    )
    assert updated.name == "Retorno VIP"
    assert updated.color_hex == "#F59E0B"

    custom_statuses.set_custom_status_active(session, org_id, created.id, False)
    assert custom_statuses.list_custom_statuses(session, org_id) == []  # some do padrão
    assert len(custom_statuses.list_custom_statuses(session, org_id, include_inactive=True)) == 1  # continua existindo


# (`test_cor_invalida_e_rejeitada_pelo_schema` — pura, sem sessão —
# movida pra `tests_unit/test_schemas_pure.py`.)


def test_status_inexistente_404(org_session):
    session, org_id = org_session
    with pytest.raises(NotFoundError):
        custom_statuses.get_custom_status(session, org_id, uuid.uuid4())


def test_isolamento_multiempresa_catalogo(org_session):
    session, org_id = org_session
    created = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Retorno", color_hex="#8B5CF6")
    )

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-status-{other_org_id.hex[:8]}"))
        other_session.flush()
        with pytest.raises(NotFoundError):
            custom_statuses.get_custom_status(other_session, other_org_id, created.id)


# ---------------------------------------------------------------------
# Atribuir/trocar/remover num agendamento — ortogonal ao status real
# ---------------------------------------------------------------------


def test_atribuir_trocar_remover_status_personalizado_no_agendamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    status_a = custom_statuses.create_custom_status(session, org_id, AppointmentCustomStatusCreate(name="VIP", color_hex="#8B5CF6"))
    status_b = custom_statuses.create_custom_status(session, org_id, AppointmentCustomStatusCreate(name="Retorno", color_hex="#F59E0B"))

    updated = appointments.set_custom_status(session, actor, appt.id, status_a.id)
    assert updated.custom_status_id == status_a.id

    swapped = appointments.set_custom_status(session, actor, appt.id, status_b.id)
    assert swapped.custom_status_id == status_b.id

    removed = appointments.set_custom_status(session, actor, appt.id, None)
    assert removed.custom_status_id is None


def test_status_personalizado_de_outra_org_nao_pode_ser_atribuido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-status2-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_status = custom_statuses.create_custom_status(
            other_session, other_org_id, AppointmentCustomStatusCreate(name="Alheio", color_hex="#000000")
        )
        other_status_id = other_status.id
        other_session.commit()

    with pytest.raises(NotFoundError):
        appointments.set_custom_status(session, actor, appt.id, other_status_id)


def test_status_personalizado_nunca_altera_status_operacional(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    original_status = appt.status
    custom_status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Aguardando cabelo", color_hex="#22C55E")
    )

    updated = appointments.set_custom_status(session, actor, appt.id, custom_status.id)
    assert updated.status == original_status == AppointmentStatus.SCHEDULED

    # E o inverso: mudar o status operacional nunca mexe na etiqueta.
    changed = appointments.update_status(session, actor, appt.id, AppointmentStatus.CONFIRMED)
    assert changed.status == AppointmentStatus.CONFIRMED
    assert changed.custom_status_id == custom_status.id  # etiqueta preservada


def test_desativar_status_nao_remove_de_agendamento_ja_atribuido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    custom_status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Retorno", color_hex="#8B5CF6")
    )
    appointments.set_custom_status(session, actor, appt.id, custom_status.id)

    custom_statuses.set_custom_status_active(session, org_id, custom_status.id, False)

    reloaded = appointments.get_appointment(session, actor, appt.id)
    assert reloaded.custom_status_id == custom_status.id  # continua atribuído
    assert custom_statuses.list_custom_statuses(session, org_id) == []  # mas some da lista de seleção


# ---------------------------------------------------------------------
# AUDITORIA "última correção pré-push" (item 3) — enforcement de
# is_active no BACKEND. Antes só a UI escondia status inativos da
# lista; uma chamada direta à API ainda conseguia atribuir um
# `custom_status_id` desativado.
# ---------------------------------------------------------------------


def test_selecionar_status_ativo_e_permitido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    active_status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="VIP", color_hex="#8B5CF6")
    )

    updated = appointments.set_custom_status(session, actor, appt.id, active_status.id)
    assert updated.custom_status_id == active_status.id


def test_selecionar_status_inativo_e_recusado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    inactive_status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Descontinuado", color_hex="#8B5CF6")
    )
    custom_statuses.set_custom_status_active(session, org_id, inactive_status.id, False)

    with pytest.raises(ValidationDomainError):
        appointments.set_custom_status(session, actor, appt.id, inactive_status.id)

    reloaded = appointments.get_appointment(session, actor, appt.id)
    assert reloaded.custom_status_id is None  # nunca foi atribuído


def test_agendamento_com_status_desativado_depois_continua_legivel(org_session):
    """"agendamento que JÁ possui status depois desativado continua
    válido e exibível" — desativar não invalida a atribuição já feita,
    só bloqueia NOVAS atribuições."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Retorno", color_hex="#8B5CF6")
    )
    appointments.set_custom_status(session, actor, appt.id, status.id)

    custom_statuses.set_custom_status_active(session, org_id, status.id, False)

    reloaded = appointments.get_appointment(session, actor, appt.id)
    assert reloaded.custom_status_id == status.id  # continua válido e exibível


def test_remover_status_inativo_existente_e_permitido(org_session):
    """"trocar/remover continua permitido" — mesmo com o status
    ATUALMENTE atribuído já desativado, `custom_status_id=None`
    (remoção) precisa continuar funcionando sem erro."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Retorno", color_hex="#8B5CF6")
    )
    appointments.set_custom_status(session, actor, appt.id, status.id)
    custom_statuses.set_custom_status_active(session, org_id, status.id, False)

    updated = appointments.set_custom_status(session, actor, appt.id, None)
    assert updated.custom_status_id is None


def test_trocar_de_status_inativo_para_status_ativo_e_permitido(org_session):
    """"trocar... continua permitido" — sair de um status já
    desativado em direção a um status ATIVO diferente precisa
    funcionar normalmente (só ATRIBUIR um inativo é bloqueado)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)
    old_status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Antigo", color_hex="#8B5CF6")
    )
    new_status = custom_statuses.create_custom_status(
        session, org_id, AppointmentCustomStatusCreate(name="Novo", color_hex="#22C55E")
    )
    appointments.set_custom_status(session, actor, appt.id, old_status.id)
    custom_statuses.set_custom_status_active(session, org_id, old_status.id, False)

    updated = appointments.set_custom_status(session, actor, appt.id, new_status.id)
    assert updated.custom_status_id == new_status.id


def test_isolamento_entre_organizacoes_preservado_com_enforcement_de_inativo(org_session):
    """Isolamento entre organizações continua 404, independente de
    `is_active` — nunca vaza se um status inativo de outra org existe."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _appointment(session, org_id, actor)

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-status3-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_status = custom_statuses.create_custom_status(
            other_session, other_org_id, AppointmentCustomStatusCreate(name="Alheio", color_hex="#000000")
        )
        custom_statuses.set_custom_status_active(other_session, other_org_id, other_status.id, False)
        other_status_id = other_status.id
        other_session.commit()

    with pytest.raises(NotFoundError):
        appointments.set_custom_status(session, actor, appt.id, other_status_id)
