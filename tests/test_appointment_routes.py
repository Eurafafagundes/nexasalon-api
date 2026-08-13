"""Testes HTTP das rotas de Agenda/Appointment (Etapa 3A) — exercitam a
pilha inteira (rota -> require_permission/require_any_permission ->
service -> repository -> trigger do banco), diferente de
`test_appointments.py`/`test_agenda.py`, que vão direto no service."""
import uuid

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User

_START = "2026-08-13T14:00:00-03:00"  # quinta-feira


def _setup_agenda(c, *, duration=60, price="100.00"):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": duration, "default_price": price}
    ).json()
    resp = c.put(
        f"/api/v1/professionals/{professional['id']}/services",
        json={"items": [{"service_id": service["id"]}]},
    )
    assert resp.status_code == 200, resp.text
    resp = c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )
    assert resp.status_code == 200, resp.text
    client = c.post("/api/v1/clients", json={"name": "Maria"}).json()
    return branch, professional, service, client


def _restricted_actor(base_actor: ActorContext, *, permissions, professional_id=None) -> ActorContext:
    """Ator com permissions restritas na MESMA organização — cria um User
    real (FK de created_by) via sessão direta, fora do fluxo HTTP."""
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
        professional_id=professional_id,
    )


def test_fluxo_completo_via_http(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)

    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    assert resp.status_code == 201, resp.text
    appointment = resp.json()
    assert len(appointment["items"]) == 1
    assert appointment["items"][0]["duration_minutes"] == 60
    assert appointment["items"][0]["price"] == "100.00"
    appointment_id = appointment["id"]

    resp = c.get(f"/api/v1/appointments/{appointment_id}")
    assert resp.status_code == 200

    resp = c.put(
        f"/api/v1/appointments/{appointment_id}",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [
                {"professional_id": professional["id"], "service_id": service["id"],
                 "start_at": "2026-08-13T16:00:00-03:00"}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["start_at"].startswith("2026-08-13T16:00:00")

    resp = c.patch(f"/api/v1/appointments/{appointment_id}/status", json={"status": "confirmed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    # PATCH genérico não pode cancelar (tem que usar o endpoint dedicado).
    resp = c.patch(f"/api/v1/appointments/{appointment_id}/status", json={"status": "cancelled"})
    assert resp.status_code == 422, resp.text

    resp = c.post(f"/api/v1/appointments/{appointment_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_criar_agendamento_sem_permissao_da_403(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)

    restricted = _restricted_actor(org_a_actor, permissions={"agenda.view_all"})
    resp = client_as(restricted).post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    assert resp.status_code == 403


def test_agenda_listagem_com_filtro(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    professional_2 = c.post("/api/v1/professionals", json={"name": "João"}).json()
    c.put(
        f"/api/v1/professionals/{professional_2['id']}/services",
        json={"items": [{"service_id": service["id"]}]},
    )
    c.put(
        f"/api/v1/professionals/{professional_2['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )

    c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [
                {"professional_id": professional_2["id"], "service_id": service["id"],
                 "start_at": "2026-08-13T10:00:00-03:00"}
            ],
        },
    )

    resp = c.get("/api/v1/agenda", params={"date": "2026-08-13", "branch_id": branch["id"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = c.get(
        "/api/v1/agenda",
        params={"date": "2026-08-13", "branch_id": branch["id"], "professional_id": professional["id"]},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["professional_id"] == professional["id"]


def test_availability_via_http(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)

    resp = c.get(
        "/api/v1/agenda/availability",
        params={
            "branch_id": branch["id"], "professional_id": professional["id"], "service_id": service["id"],
            "date": "2026-08-13", "slot_minutes": 30,
        },
    )
    assert resp.status_code == 200, resp.text
    slots = resp.json()
    assert len(slots) > 0
    assert slots[0]["start_at"].startswith("2026-08-13T09:00:00")


def test_isolamento_entre_organizacoes_via_http(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c_a)
    resp = c_a.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    appointment_id = resp.json()["id"]

    resp_b = client_as(org_b_actor).get(f"/api/v1/appointments/{appointment_id}")
    assert resp_b.status_code == 404


def test_view_own_via_http(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    appointment_id = resp.json()["id"]

    other_professional_id = uuid.UUID(c.post("/api/v1/professionals", json={"name": "Outro"}).json()["id"])
    stranger = _restricted_actor(
        org_a_actor, permissions={"agenda.view_own"}, professional_id=other_professional_id
    )
    resp = client_as(stranger).get(f"/api/v1/appointments/{appointment_id}")
    assert resp.status_code == 404

    owner_prof = _restricted_actor(
        org_a_actor, permissions={"agenda.view_own"}, professional_id=uuid.UUID(professional["id"])
    )
    resp = client_as(owner_prof).get(f"/api/v1/appointments/{appointment_id}")
    assert resp.status_code == 200


def test_force_overlap_sem_permissao_via_http_gera_403(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )

    restricted = _restricted_actor(org_a_actor, permissions={"agenda.create"})
    resp = client_as(restricted).post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"], "force_overlap": True,
            "items": [
                {"professional_id": professional["id"], "service_id": service["id"],
                 "start_at": "2026-08-13T14:30:00-03:00"}
            ],
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["type"] == "forbidden"


def test_force_overlap_sem_permissao_sem_conflito_tambem_e_403(client_as, org_a_actor):
    """403 é sobre a permissão em si, não sobre existir (ou não) um
    conflito real por trás — o pedido é recusado mesmo num horário livre."""
    c = client_as(org_a_actor)
    branch, professional, service, client = _setup_agenda(c)
    restricted = _restricted_actor(org_a_actor, permissions={"agenda.create"})
    resp = client_as(restricted).post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"], "force_overlap": True,
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    assert resp.status_code == 403, resp.text
