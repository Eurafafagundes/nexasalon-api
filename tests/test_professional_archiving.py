"""Testes de "Arquivamento/Desativação de Profissional" (decisão A
aprovada na auditoria) — `services/professionals.py::set_professional_active`.

Cobre: desativar sem agenda futura funciona; agendamento futuro ATIVO
bloqueia (com a quantidade no erro); cancelado/finalizado NUNCA
bloqueia (mesma semântica de `appointment_item_repo.OCCUPYING_STATUSES`,
já usada por disponibilidade/conflito); nenhum agendamento é cancelado
automaticamente; reativação preserva o mesmo `Professional.id` e todas
as configurações; AuditLog nos dois sentidos; RBAC (`professionals.manage`);
isolamento multi-tenant. Reaproveita os helpers HTTP já estabelecidos em
`test_appointment_routes.py`/`test_professionals.py` — não duplica CRUD,
jornada ou vínculo de serviço, já cobertos ali."""
import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.audit import AuditLog
from nexasalon_api.models.enums import AuditAction
from nexasalon_api.repositories import audit_log_repo

_FUTURE_START = (datetime.now(timezone.utc) + timedelta(days=60)).replace(microsecond=0)


def _setup_agenda(c, *, name="John"):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": name, "branch_id": branch["id"]}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    c.put(f"/api/v1/professionals/{professional['id']}/services", json={"items": [{"service_id": service["id"]}]})
    c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    client = c.post("/api/v1/clients", json={"name": "Cliente"}).json()
    return branch, professional, service, client


def _create_appointment(c, branch, professional, service, client, *, start_at: datetime):
    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": start_at.isoformat()}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _audit_logs(organization_id: uuid.UUID, professional_id: uuid.UUID) -> list[AuditLog]:
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(organization_id)})
        return audit_log_repo.list_for_entity(session, organization_id, "professional", professional_id)


# ---------------------------------------------------------------------
# Desativação sem agenda futura
# ---------------------------------------------------------------------


def test_desativar_sem_agendamento_futuro_funciona(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c)

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


def test_professional_permanece_no_banco_com_mesmo_id_apos_desativar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c)

    c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")

    resp = c.get(f"/api/v1/professionals/{professional['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == professional["id"]
    assert resp.json()["name"] == "John"


# ---------------------------------------------------------------------
# Agendamento futuro ATIVO bloqueia — decisão A
# ---------------------------------------------------------------------


def test_agendamento_futuro_ativo_bloqueia_desativacao_com_quantidade(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)
    _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START + timedelta(days=1))

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["type"] == "validation_error"
    assert body["error"]["details"]["future_appointments_count"] == 2

    # nunca desativa de fato quando recusado.
    assert c.get(f"/api/v1/professionals/{professional['id']}").json()["is_active"] is True


def test_desativacao_recusada_nao_cancela_nenhum_agendamento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    appt = _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 422

    reloaded = c.get(f"/api/v1/appointments/{appt['id']}")
    assert reloaded.json()["status"] == "scheduled"  # intocado — nada foi cancelado/transferido automaticamente


def test_agendamento_futuro_cancelado_nao_bloqueia(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    appt = _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)
    assert c.post(f"/api/v1/appointments/{appt['id']}/cancel").status_code == 200

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 200, resp.text


def test_agendamento_futuro_finalizado_nao_bloqueia(client_as, org_a_actor):
    """Mesma semântica real de `OCCUPYING_STATUSES`: `finished` (e
    `no_show`) nunca representam compromisso pendente, mesmo com
    `start_at` no futuro (ex.: atendimento adiantado/registrado antes
    da hora)."""
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    appt = _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)
    assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": "finished"}).status_code == 200

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 200, resp.text


def test_um_agendamento_futuro_ativo_e_outro_cancelado_conta_so_o_ativo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    cancelled = _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)
    c.post(f"/api/v1/appointments/{cancelled['id']}/cancel")
    _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START + timedelta(days=2))

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["future_appointments_count"] == 1


# ---------------------------------------------------------------------
# Reativação — mesmo Professional.id, configurações preservadas
# ---------------------------------------------------------------------


def test_reativar_mantem_mesmo_id_e_preserva_vinculos_e_horarios(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, service, _client = _setup_agenda(c)
    c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/activate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == professional["id"]
    assert resp.json()["is_active"] is True

    hours = c.get(f"/api/v1/professionals/{professional['id']}/working-hours").json()
    assert len(hours) == 7
    services = c.get(f"/api/v1/professionals/{professional['id']}/services").json()
    assert any(row["service_id"] == service["id"] for row in services)


def test_reativar_permite_novo_agendamento_de_novo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    c.patch(f"/api/v1/professionals/{professional['id']}/activate")

    appt = _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)
    assert appt["items"][0]["professional_id"] == professional["id"]


# ---------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------


def test_desativar_gera_audit_log(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c)

    c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")

    logs = _audit_logs(org_a_actor.organization_id, uuid.UUID(professional["id"]))
    assert len(logs) == 1
    log = logs[0]
    assert log.action == AuditAction.UPDATE
    assert log.user_id == org_a_actor.user_id
    assert log.organization_id == org_a_actor.organization_id
    assert log.new_values == {"change_type": "set_active", "is_active": False}
    assert log.old_values == {"is_active": True}


def test_ativar_gera_audit_log(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c)
    c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")

    c.patch(f"/api/v1/professionals/{professional['id']}/activate")

    logs = _audit_logs(org_a_actor.organization_id, uuid.UUID(professional["id"]))
    assert len(logs) == 2
    last = logs[-1]
    assert last.new_values == {"change_type": "set_active", "is_active": True}
    assert last.old_values == {"is_active": False}


def test_tentativa_recusada_nao_gera_audit_log(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    _create_appointment(c, branch, professional, service, client, start_at=_FUTURE_START)

    resp = c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 422

    logs = _audit_logs(org_a_actor.organization_id, uuid.UUID(professional["id"]))
    assert logs == []


# ---------------------------------------------------------------------
# RBAC — professionals.manage
# ---------------------------------------------------------------------


def test_ator_sem_professionals_manage_nao_pode_desativar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c)

    restricted = dataclasses.replace(org_a_actor, permissions=org_a_actor.permissions - {"professionals.manage"})
    resp = client_as(restricted).patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 403

    still_active = c.get(f"/api/v1/professionals/{professional['id']}")
    assert still_active.json()["is_active"] is True


def test_ator_sem_professionals_manage_nao_pode_reativar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c)
    c.patch(f"/api/v1/professionals/{professional['id']}/deactivate")

    restricted = dataclasses.replace(org_a_actor, permissions=org_a_actor.permissions - {"professionals.manage"})
    resp = client_as(restricted).patch(f"/api/v1/professionals/{professional['id']}/activate")
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# Isolamento multi-tenant
# ---------------------------------------------------------------------


def test_isolamento_multi_tenant_desativar_professional(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    _branch, professional, _service, _client = _setup_agenda(c_a)

    c_b = client_as(org_b_actor)
    resp = c_b.patch(f"/api/v1/professionals/{professional['id']}/deactivate")
    assert resp.status_code == 404

    c_a = client_as(org_a_actor)
    assert c_a.get(f"/api/v1/professionals/{professional['id']}").json()["is_active"] is True
