"""Testes HTTP de `/api/v1/schedule-blocks` — CRUD de bloqueio/exceção
de agenda. O model+migration já existiam (Etapa 2A); esta rota é nova.
A parte difícil (bloqueio impedir agendamento) já era coberta por
`test_appointments.py`/`test_availability.py` inserindo `ScheduleBlock`
direto via model — aqui validamos que o mesmo efeito acontece passando
pela API nova de ponta a ponta."""
import uuid

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User

_START = "2026-08-13T14:00:00-03:00"  # quinta-feira


def _restricted_actor(base_actor: ActorContext, *, permissions) -> ActorContext:
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


def test_criar_listar_remover_bloqueio_de_profissional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()

    resp = c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": professional["id"],
            "block_type": "meeting", "title": "Dentista",
            "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    )
    assert resp.status_code == 201, resp.text
    block = resp.json()
    assert block["title"] == "Dentista"
    block_id = block["id"]

    resp = c.get("/api/v1/schedule-blocks", params={"date": "2026-08-18"})
    assert resp.status_code == 200, resp.text
    assert any(b["id"] == block_id for b in resp.json())

    resp = c.delete(f"/api/v1/schedule-blocks/{block_id}")
    assert resp.status_code == 204

    resp = c.get("/api/v1/schedule-blocks", params={"date": "2026-08-18"})
    assert all(b["id"] != block_id for b in resp.json())


def test_bloqueio_de_organizacao_nao_aceita_professional_id(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()

    resp = c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "organization", "professional_id": professional["id"],
            "block_type": "other", "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    )
    assert resp.status_code == 422


def test_bloqueio_professional_scope_exige_professional_id(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional",
            "block_type": "other", "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    )
    assert resp.status_code == 422


def test_end_at_deve_ser_maior_que_start_at(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    resp = c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": professional["id"],
            "block_type": "other", "start_at": "2026-08-18T16:00:00-03:00", "end_at": "2026-08-18T14:00:00-03:00",
        },
    )
    assert resp.status_code == 422


def test_professional_id_deve_pertencer_a_mesma_organizacao(client_as, org_a_actor, org_b_actor):
    professional_b = client_as(org_b_actor).post("/api/v1/professionals", json={"name": "De outra org"}).json()

    resp = client_as(org_a_actor).post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": professional_b["id"],
            "block_type": "other", "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "validation_error"


def test_isolamento_multi_tenant_na_listagem_e_remocao(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    professional_a = c_a.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    block = c_a.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": professional_a["id"],
            "block_type": "other", "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    ).json()

    c_b = client_as(org_b_actor)
    resp = c_b.get("/api/v1/schedule-blocks", params={"date": "2026-08-18"})
    assert all(b["id"] != block["id"] for b in resp.json())

    resp = c_b.delete(f"/api/v1/schedule-blocks/{block['id']}")
    assert resp.status_code == 404


def test_listagem_sem_professional_id_devolve_bloqueios_de_varios_profissionais(client_as, org_a_actor):
    """A Agenda principal mostra várias colunas (uma por profissional) ao
    mesmo tempo — a listagem pra exibição não pode ficar restrita a UM
    profissional só porque nenhum professional_id foi passado."""
    c = client_as(org_a_actor)
    prof_1 = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    prof_2 = c.post("/api/v1/professionals", json={"name": "Ingrid"}).json()

    c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": prof_1["id"],
            "block_type": "other", "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    )
    c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": prof_2["id"],
            "block_type": "other", "start_at": "2026-08-18T10:00:00-03:00", "end_at": "2026-08-18T11:00:00-03:00",
        },
    )

    resp = c.get("/api/v1/schedule-blocks", params={"date": "2026-08-18"})
    assert resp.status_code == 200, resp.text
    professional_ids = {b["professional_id"] for b in resp.json()}
    assert professional_ids == {prof_1["id"], prof_2["id"]}


def test_permissao_agenda_manage_blocks_e_exigida_para_criar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()

    restricted = _restricted_actor(org_a_actor, permissions={"agenda.view_all"})
    resp = client_as(restricted).post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": professional["id"],
            "block_type": "other", "start_at": "2026-08-18T14:00:00-03:00", "end_at": "2026-08-18T16:00:00-03:00",
        },
    )
    assert resp.status_code == 403


def test_listar_bloqueios_exige_agenda_view_all(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"agenda.manage_blocks"})
    resp = client_as(restricted).get("/api/v1/schedule-blocks", params={"date": "2026-08-18"})
    assert resp.status_code == 403


def test_bloqueio_criado_via_api_impede_agendamento_sobreposto(client_as, org_a_actor):
    """Ponta a ponta: bloqueio criado pela API nova é respeitado pelo
    motor de conflito já existente em `services/appointments.py`, sem
    nenhuma mudança na lógica de conflito em si."""
    c = client_as(org_a_actor)
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    c.put(
        f"/api/v1/professionals/{professional['id']}/services",
        json={"items": [{"service_id": service["id"]}]},
    )
    c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )
    client = c.post("/api/v1/clients", json={"name": "Maria"}).json()

    resp = c.post(
        "/api/v1/schedule-blocks",
        json={
            "scope": "professional", "professional_id": professional["id"],
            "block_type": "lunch", "title": "Almoço",
            "start_at": "2026-08-13T13:30:00-03:00", "end_at": "2026-08-13T15:00:00-03:00",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "bloqueio" in resp.json()["error"]["message"].lower()
