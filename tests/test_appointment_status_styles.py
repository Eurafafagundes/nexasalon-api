"""Testes de `appointment_status_styles` (Configurações > Status da
Agenda — personalização de nome/cor dos 8 status oficiais). Mistura
dois níveis, igual `test_appointment_routes.py`: regra de negócio
direto no service layer (mais rápido, sem HTTP) + os testes de
permissão (que só fazem sentido via rota, já que `require_permission`
é uma dependency do FastAPI, não uma checagem dentro do service)."""
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.audit import AuditLog
from nexasalon_api.models.client import Client
from nexasalon_api.models.identity import User
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.appointment_status_style import AppointmentStatusStyleUpdate
from nexasalon_api.services import appointments, appointment_status_styles
from nexasalon_api.services.appointment_state_machine import next_status

_ALL_PERMS = frozenset(
    {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel", "settings.manage"}
)
_THURSDAY = 4  # 0=domingo..6=sábado; 2026-08-13 é quinta.
_TZ = timezone(timedelta(hours=-3))


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org status styles", slug=f"org-styles-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_ALL_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _paid_appointment(session, org_id, actor):
    """Agendamento real, promovido a `paid` via `mark_paid` — usado só
    pelo teste que prova que personalizar a APRESENTAÇÃO de "paid" não
    muda nada no comportamento financeiro."""
    branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(branch)
    session.flush()
    prof = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional")
    session.add(prof)
    session.flush()
    service = Service(organization_id=org_id, name="Corte", default_duration_minutes=60, default_price=Decimal("100.00"))
    session.add(service)
    session.flush()
    session.add(ProfessionalService(professional_id=prof.id, service_id=service.id))
    session.add(WorkingHours(organization_id=org_id, professional_id=prof.id, weekday=_THURSDAY, start_time=time(9, 0), end_time=time(20, 0)))
    client = Client(organization_id=org_id, name="Cliente")
    session.add(client)
    session.flush()

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=datetime(2026, 8, 13, 9, 0, tzinfo=_TZ))],
    )
    appt = appointments.create_appointment(session, actor, data)
    return appointments.mark_paid(session, actor, appt.id)


# ---------------------------------------------------------------------
# Defaults / sparse / CRUD básico (service layer)
# ---------------------------------------------------------------------


def test_organizacao_sem_personalizacao_tem_lista_vazia(org_session):
    """Item "se nunca personalizou, usa o padrão de fábrica": no backend
    isso é representado por NENHUMA linha — quem decide o padrão visual
    é o frontend (`config/appointment-status.ts`)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    assert appointment_status_styles.list_styles(session, actor) == []


def test_personalizar_nome_e_cor_cria_override(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)

    style = appointment_status_styles.set_style(
        session, actor, AppointmentStatus.PAID,
        AppointmentStatusStyleUpdate(label="Recebido", color_hex="#059669"),
    )
    assert style.label == "Recebido"
    assert style.color_hex == "#059669"

    styles = appointment_status_styles.list_styles(session, actor)
    assert len(styles) == 1
    assert styles[0].status_code == AppointmentStatus.PAID


def test_personalizar_so_a_cor_mantem_nome_no_default(org_session):
    """Campos independentes: dá pra mandar só `color_hex`, deixando
    `label=None` — o registro existe (cor customizada), mas quem lê
    sabe que o nome ainda é o de fábrica (label None)."""
    session, org_id = org_session
    actor = _actor(session, org_id)

    style = appointment_status_styles.set_style(
        session, actor, AppointmentStatus.CONFIRMED, AppointmentStatusStyleUpdate(color_hex="#111111"),
    )
    assert style.label is None
    assert style.color_hex == "#111111"


def test_resetar_remove_a_personalizacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appointment_status_styles.set_style(
        session, actor, AppointmentStatus.WAITING, AppointmentStatusStyleUpdate(label="Na fila"),
    )
    assert len(appointment_status_styles.list_styles(session, actor)) == 1

    appointment_status_styles.reset_style(session, actor, AppointmentStatus.WAITING)
    assert appointment_status_styles.list_styles(session, actor) == []


def test_reset_de_status_nunca_personalizado_e_idempotente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appointment_status_styles.reset_style(session, actor, AppointmentStatus.NO_SHOW)
    assert appointment_status_styles.list_styles(session, actor) == []


def test_enviar_os_dois_campos_nulos_no_set_style_equivale_a_reset(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appointment_status_styles.set_style(
        session, actor, AppointmentStatus.SCHEDULED, AppointmentStatusStyleUpdate(label="Marcado"),
    )
    result = appointment_status_styles.set_style(
        session, actor, AppointmentStatus.SCHEDULED, AppointmentStatusStyleUpdate(label=None, color_hex=None),
    )
    assert result is None
    assert appointment_status_styles.list_styles(session, actor) == []


def test_personalizar_de_novo_sobrescreve_o_valor_anterior(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appointment_status_styles.set_style(
        session, actor, AppointmentStatus.FINISHED, AppointmentStatusStyleUpdate(label="Concluído", color_hex="#111111"),
    )
    updated = appointment_status_styles.set_style(
        session, actor, AppointmentStatus.FINISHED, AppointmentStatusStyleUpdate(label="Feito", color_hex="#222222"),
    )
    assert updated.label == "Feito"
    assert updated.color_hex == "#222222"
    assert len(appointment_status_styles.list_styles(session, actor)) == 1


def test_auditoria_registra_customizacao_e_reset(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appointment_status_styles.set_style(
        session, actor, AppointmentStatus.NO_SHOW, AppointmentStatusStyleUpdate(label="Não veio"),
    )
    appointment_status_styles.reset_style(session, actor, AppointmentStatus.NO_SHOW)

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_type == "appointment_status_style"
    ).all()
    actions = sorted(log.action.value for log in logs)
    assert actions == ["create", "delete"]
    assert any(log.new_values and log.new_values.get("label") == "Não veio" for log in logs)


# ---------------------------------------------------------------------
# Isolamento multiempresa
# ---------------------------------------------------------------------


def test_personalizacao_e_isolada_por_organizacao(org_session):
    session, org_id_a = org_session
    actor_a = _actor(session, org_id_a)
    appointment_status_styles.set_style(
        session, actor_a, AppointmentStatus.PAID, AppointmentStatusStyleUpdate(label="Recebido"),
    )

    org_id_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id_b)})
    session.add(Organization(id=org_id_b, name="Org B", slug=f"org-b-{org_id_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_id_b)

    assert appointment_status_styles.list_styles(session, actor_b) == []

    # Volta o contexto de RLS pra org A e confirma que a personalização
    # dela continua lá, intacta.
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id_a)})
    styles_a = appointment_status_styles.list_styles(session, actor_a)
    assert len(styles_a) == 1
    assert styles_a[0].label == "Recebido"


# ---------------------------------------------------------------------
# Validação de schema (HEX / tamanho do nome)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_hex", ["059669", "#0596", "#GGGGGG", "059669FF", "#12345"])
def test_color_hex_invalido_e_rejeitado(bad_hex):
    with pytest.raises(ValueError):
        AppointmentStatusStyleUpdate(color_hex=bad_hex)


def test_color_hex_valido_e_aceito():
    update = AppointmentStatusStyleUpdate(color_hex="#059669")
    assert update.color_hex == "#059669"


def test_label_vazio_e_rejeitado():
    with pytest.raises(ValueError):
        AppointmentStatusStyleUpdate(label="")


def test_label_maior_que_40_caracteres_e_rejeitado():
    with pytest.raises(ValueError):
        AppointmentStatusStyleUpdate(label="x" * 41)


# ---------------------------------------------------------------------
# "Pago é diferente" — personalizar apresentação NÃO muda comportamento
# financeiro (item explícito do pedido)
# ---------------------------------------------------------------------


def test_personalizar_paid_nao_altera_o_comportamento_da_maquina_de_estados(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _paid_appointment(session, org_id, actor)
    assert appt.status == AppointmentStatus.PAID

    appointment_status_styles.set_style(
        session, actor, AppointmentStatus.PAID, AppointmentStatusStyleUpdate(label="Recebido", color_hex="#059669"),
    )

    # O Appointment continua com o CÓDIGO interno "paid" — a
    # personalização não reescreveu `appointment.status` nem nada
    # relacionado, é uma tabela totalmente separada.
    session.refresh(appt)
    assert appt.status == AppointmentStatus.PAID

    # E a regra "PAID é terminal pro grafo livre" continua valendo
    # exatamente igual — `next_status` não sabe nem precisa saber que
    # "paid" agora se chama "Recebido" na tela.
    from nexasalon_api.core.exceptions import ValidationDomainError

    with pytest.raises(ValidationDomainError):
        next_status(AppointmentStatus.PAID, AppointmentStatus.SCHEDULED)
    with pytest.raises(ValidationDomainError):
        appointments.mark_paid(session, actor, appt.id)  # já pago — recusa igual antes.


def test_personalizar_paid_nao_cria_nenhuma_relacao_com_appointment_ou_order(org_session):
    """Reforço estrutural do teste acima: a tabela de estilo não tem
    nenhuma FK partindo de `Appointment`/`Order`/`Payment` — customizar
    "paid" é uma operação isolada que só grava em
    `appointment_status_styles`, nunca em `appointments`."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt = _paid_appointment(session, org_id, actor)
    before = (appt.status, appt.updated_at)

    appointment_status_styles.set_style(
        session, actor, AppointmentStatus.PAID, AppointmentStatusStyleUpdate(label="Recebido"),
    )
    session.refresh(appt)
    assert (appt.status, appt.updated_at) == before


# ---------------------------------------------------------------------
# Permissão — só testável via HTTP, `require_permission` é uma
# dependency de rota (não uma checagem dentro do service). Reaproveita
# `client_as`/`org_a_actor` de `conftest.py` (mesmo padrão de
# `test_appointment_routes.py`).
# ---------------------------------------------------------------------


def _restricted_actor(base_actor: ActorContext, *, permissions) -> ActorContext:
    """Ator com permissions restritas na MESMA organização — mesmo
    helper de `test_appointment_routes.py::_restricted_actor`, duplicado
    aqui de propósito (cada arquivo de teste é self-contained no
    projeto)."""
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Restrito")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Restrito", permissions=frozenset(permissions),
    )


def test_get_nao_exige_settings_manage_qualquer_autenticado_ve(client_as, org_a_actor):
    """"demais usuários apenas enxergam a configuração aplicada" — sem
    `settings.manage`, ainda assim consegue GET."""
    owner = client_as(org_a_actor)
    put_resp = owner.put("/api/v1/appointment-status-styles/paid", json={"label": "Recebido"})
    assert put_resp.status_code == 200, put_resp.text

    restricted = _restricted_actor(org_a_actor, permissions={"agenda.view_own"})
    resp = client_as(restricted).get("/api/v1/appointment-status-styles")
    assert resp.status_code == 200, resp.text
    assert any(item["status_code"] == "paid" and item["label"] == "Recebido" for item in resp.json())


def test_put_sem_settings_manage_da_403(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"agenda.view_own"})
    resp = client_as(restricted).put("/api/v1/appointment-status-styles/confirmed", json={"label": "Confirmadíssimo"})
    assert resp.status_code == 403, resp.text


def test_delete_sem_settings_manage_da_403(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"agenda.view_own"})
    resp = client_as(restricted).delete("/api/v1/appointment-status-styles/confirmed")
    assert resp.status_code == 403, resp.text


def test_owner_com_settings_manage_personaliza_e_reseta_via_http(client_as, org_a_actor):
    c = client_as(org_a_actor)

    put_resp = c.put("/api/v1/appointment-status-styles/waiting", json={"label": "Na fila", "color_hex": "#D97706"})
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["label"] == "Na fila"

    list_resp = c.get("/api/v1/appointment-status-styles")
    assert any(item["status_code"] == "waiting" for item in list_resp.json())

    delete_resp = c.delete("/api/v1/appointment-status-styles/waiting")
    assert delete_resp.status_code == 204, delete_resp.text

    list_resp = c.get("/api/v1/appointment-status-styles")
    assert not any(item["status_code"] == "waiting" for item in list_resp.json())


def test_status_code_invalido_na_url_da_422(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.put("/api/v1/appointment-status-styles/nao_existe", json={"label": "X"})
    assert resp.status_code == 422, resp.text


def test_hex_invalido_via_http_da_422(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.put("/api/v1/appointment-status-styles/paid", json={"color_hex": "vermelho"})
    assert resp.status_code == 422, resp.text
