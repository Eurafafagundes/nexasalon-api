"""Etapa "Disponibilidade por profissional x serviço no Agendamento
Online" — cobertura da NOVA flag `ProfessionalService.allow_online_booking`
(migration 0056), independente de `is_active` ("realiza o serviço").

Reaproveita os MESMOS helpers/fixtures de `tests/test_public_booking.py`
(`_enable_online_booking`, `_register_customer`, `_auth`) — nenhuma
segunda infraestrutura de teste pro fluxo público.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app
from tests.test_public_booking import (
    _auth,
    _enable_online_booking,
    _iso,
    _register_customer,
)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    return TestClient(app)


def _branch_service_professional(c, *, service_name: str = "Corte"):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    svc = c.post(
        "/api/v1/services",
        json={"name": service_name, "default_duration_minutes": 60, "default_price": "100.00"},
    ).json()
    prof = c.post("/api/v1/professionals", json={"name": f"Profissional {uuid.uuid4().hex[:6]}"}).json()
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    return branch, prof, svc


def _link(c, prof_id: str, service_id: str, *, is_active: bool = True, allow_online_booking: bool = True):
    resp = c.put(
        f"/api/v1/professionals/{prof_id}/services",
        json={
            "items": [
                {"service_id": service_id, "is_active": is_active, "allow_online_booking": allow_online_booking}
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()[0]


# ---------------------------------------------------------------------
# API administrativa — payload persiste, coerência com is_active
# ---------------------------------------------------------------------


def test_payload_administrativo_persiste_allow_online_booking(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, prof, svc = _branch_service_professional(c)

    row = _link(c, prof["id"], svc["id"], is_active=True, allow_online_booking=True)
    assert row["allow_online_booking"] is True

    row = _link(c, prof["id"], svc["id"], is_active=True, allow_online_booking=False)
    assert row["allow_online_booking"] is False

    read_back = c.get(f"/api/v1/professionals/{prof['id']}/services").json()
    assert read_back[0]["allow_online_booking"] is False


def test_is_active_false_com_allow_online_booking_true_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, prof, svc = _branch_service_professional(c)

    resp = c.put(
        f"/api/v1/professionals/{prof['id']}/services",
        json={"items": [{"service_id": svc["id"], "is_active": False, "allow_online_booking": True}]},
    )
    assert resp.status_code == 422, resp.text


def test_desligar_is_active_forca_online_false_mesmo_sem_enviar_o_campo(client_as, org_a_actor):
    """`is_active=False` sem `allow_online_booking` no payload usa o
    default do schema (`True`) — o backend precisa recusar mesmo assim,
    nunca aceitar silenciosamente uma combinação inconsistente só porque
    o campo não veio explícito."""
    c = client_as(org_a_actor)
    _branch, prof, svc = _branch_service_professional(c)

    resp = c.put(
        f"/api/v1/professionals/{prof['id']}/services",
        json={"items": [{"service_id": svc["id"], "is_active": False}]},
    )
    assert resp.status_code == 422, resp.text


def test_desligar_um_servico_nao_remove_nem_afeta_outros_servicos_do_mesmo_profissional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _branch, prof, svc_a = _branch_service_professional(c, service_name="Corte")
    svc_b = c.post(
        "/api/v1/services", json={"name": "Escova", "default_duration_minutes": 30, "default_price": "50.00"}
    ).json()

    resp = c.put(
        f"/api/v1/professionals/{prof['id']}/services",
        json={
            "items": [
                {"service_id": svc_a["id"], "is_active": True, "allow_online_booking": False},
                {"service_id": svc_b["id"], "is_active": True, "allow_online_booking": True},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    rows = {r["service_id"]: r for r in resp.json()}
    assert rows[svc_a["id"]]["is_active"] is True
    assert rows[svc_a["id"]]["allow_online_booking"] is False
    assert rows[svc_b["id"]]["is_active"] is True
    assert rows[svc_b["id"]]["allow_online_booking"] is True


# ---------------------------------------------------------------------
# Fluxo público — listagem e confirmação respeitam a flag por vínculo
# ---------------------------------------------------------------------


def test_vinculo_online_true_aparece_na_listagem_publica(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _branch_service_professional(c)
    _link(c, prof["id"], svc["id"], is_active=True, allow_online_booking=True)

    p = _public()
    resp = p.get(f"/api/v1/public/booking/{org['slug']}/professionals", params={"service_id": svc["id"]})
    assert resp.status_code == 200
    assert any(item["id"] == prof["id"] for item in resp.json())


def test_vinculo_online_false_nao_aparece_na_listagem_publica_nem_aceita_reserva(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _branch_service_professional(c)
    # Realiza o serviço normalmente, mas essa combinação NÃO deve
    # aparecer/ser aceita no online (exemplo exato do pedido: Ingrid
    # continua fazendo "Manutenção 3 Telas", só não online).
    _link(c, prof["id"], svc["id"], is_active=True, allow_online_booking=False)

    p = _public()
    listing = p.get(f"/api/v1/public/booking/{org['slug']}/professionals", params={"service_id": svc["id"]})
    assert listing.status_code == 200
    assert all(item["id"] != prof["id"] for item in listing.json())

    # Bypass: POST direto pra API pública, sem passar pela listagem —
    # o backend é a camada de segurança real, não a UI.
    start_at = _iso(
        (datetime.now(timezone.utc) + timedelta(days=14)).replace(hour=9, minute=0, second=0, microsecond=0)
    )
    token = _register_customer(p, name="Cliente Bypass", phone="61900001111")
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={"service_id": svc["id"], "professional_id": prof["id"], "start_at": start_at},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


def test_qualquer_profissional_nunca_resolve_para_combinacao_com_online_desabilitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof_offline, svc = _branch_service_professional(c)
    _link(c, prof_offline["id"], svc["id"], is_active=True, allow_online_booking=False)

    prof_online = c.post("/api/v1/professionals", json={"name": "Profissional Online"}).json()
    c.put(
        f"/api/v1/professionals/{prof_online['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    _link(c, prof_online["id"], svc["id"], is_active=True, allow_online_booking=True)

    p = _public()
    listing = p.get(f"/api/v1/public/booking/{org['slug']}/professionals", params={"service_id": svc["id"]})
    ids = {item["id"] for item in listing.json()}
    assert prof_online["id"] in ids
    assert prof_offline["id"] not in ids

    target_date = (datetime.now(timezone.utc) + timedelta(days=13)).date().isoformat()
    availability = p.get(
        f"/api/v1/public/booking/{org['slug']}/availability",
        params={"service_id": svc["id"], "date": target_date},
    )
    assert availability.status_code == 200
    assert len(availability.json()) > 0
    start_at = availability.json()[0]["start_at"]

    token = _register_customer(p, name="Cliente Qualquer", phone="61900002222")
    booking = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={"service_id": svc["id"], "professional_id": None, "start_at": start_at},
        headers=_auth(token),
    )
    assert booking.status_code == 201, booking.text
    appt = c.get(f"/api/v1/appointments/{booking.json()['id']}").json()
    assert appt["items"][0]["professional_id"] == prof_online["id"]


def test_todos_offline_para_o_servico_rejeita_qualquer_profissional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _branch_service_professional(c)
    _link(c, prof["id"], svc["id"], is_active=True, allow_online_booking=False)

    p = _public()
    start_at = _iso(
        (datetime.now(timezone.utc) + timedelta(days=16)).replace(hour=9, minute=0, second=0, microsecond=0)
    )
    token = _register_customer(p, name="Cliente Sem Opção", phone="61900003333")
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={"service_id": svc["id"], "professional_id": None, "start_at": start_at},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------
# Agenda interna — NUNCA restringida pela nova flag
# ---------------------------------------------------------------------


def test_agenda_interna_continua_permitindo_o_servico_mesmo_com_online_desabilitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch, prof, svc = _branch_service_professional(c)
    _link(c, prof["id"], svc["id"], is_active=True, allow_online_booking=False)

    client = c.post("/api/v1/clients", json={"name": "Cliente Interno"}).json()
    start_at = _iso(
        (datetime.now(timezone.utc) + timedelta(days=5)).replace(hour=10, minute=0, second=0, microsecond=0)
    )
    resp = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"],
            "client_id": client["id"],
            "items": [{"professional_id": prof["id"], "service_id": svc["id"], "start_at": start_at}],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["items"][0]["professional_id"] == prof["id"]


# ---------------------------------------------------------------------
# Backfill — preserva a elegibilidade que já existia antes da migration
# ---------------------------------------------------------------------


def test_backfill_da_migration_0056_deriva_allow_online_booking_de_is_active(client_as, org_a_actor):
    """Não é um teste de `alembic upgrade` (fora do escopo de
    integração), mas confirma a fórmula que o backfill precisa
    reproduzir: pra qualquer vínculo já existente, `allow_online_booking`
    inicial == `is_active` — nenhuma combinação some nem aparece a mais
    depois da migration."""
    from nexasalon_api.models.service import ProfessionalService

    c = client_as(org_a_actor)
    _branch, prof, svc = _branch_service_professional(c)
    # Simula o estado ANTES da migration: só `is_active` existia.
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})

    from sqlalchemy import text

    from nexasalon_api.core.db import SessionLocal

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        row = session.query(ProfessionalService).filter_by(professional_id=uuid.UUID(prof["id"])).one()
        # `is_active` default é True (payload não mandou `is_active`) —
        # o backfill real (`UPDATE ... SET allow_online_booking =
        # is_active`) preservaria exatamente esse valor.
        assert row.is_active is True
        assert row.allow_online_booking == row.is_active
