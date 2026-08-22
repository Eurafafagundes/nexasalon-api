"""Etapa M, P0 — correção do bug de integridade CustomerAccount ↔ Client.

Bug relatado em produção: uma cliente criava a própria conta no
Agendamento Online e o agendamento acabava vinculado a um `Client`
ANTIGO/errado, cadastrado manualmente. Causa raiz: a resolução usava
`client_repo.get_by_phone` (só a coluna `phone`) e aceitava o primeiro
resultado sem checar se era único — ver
`services/customer_accounts.py::resolve_client_for_customer_account`.

Estes testes cobrem exatamente os cenários de identidade pedidos:
nomes parecidos, telefones diferentes, `Client` antigo com mesmo nome,
mesma conta com dois agendamentos, duas organizações, e o caso
ambíguo (dois `Client`s antigos compartilhando o mesmo número) — em
NENHUM desses casos o vínculo pode cair no `Client` errado."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(p, *, name: str = "Cliente Teste", phone: str = "61999990000", email: str | None = None) -> dict:
    email = email or f"cliente-{uuid.uuid4().hex[:10]}@example.com"
    payload = {
        "name": name,
        "email": email,
        "password": "Senha123!",
        "password_confirm": "Senha123!",
        "phone": phone,
    }
    resp = p.post("/api/v1/customer-auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _enable_online_booking(c, **overrides):
    payload = {
        "online_booking_enabled": True,
        "online_booking_auto_confirm": True,
        "online_booking_min_lead_minutes": 0,
        "online_booking_max_lead_days": 3650,
        **overrides,
    }
    resp = c.put("/api/v1/organization", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _setup_service_and_professional(c):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    svc = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    prof = c.post("/api/v1/professionals", json={"name": "Profissional"}).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)]},
    )
    return branch, prof, svc


def _create_manual_client(c, *, name: str, phone: str | None = None, whatsapp: str | None = None) -> dict:
    payload: dict = {"name": name}
    if phone is not None:
        payload["phone"] = phone
    if whatsapp is not None:
        payload["whatsapp"] = whatsapp
    resp = c.post("/api/v1/clients", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _book(p, slug, token, *, service_id, professional_id, start_at) -> dict:
    resp = p.post(
        f"/api/v1/public/booking/{slug}",
        json={"service_id": service_id, "professional_id": professional_id, "start_at": start_at},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _next_slot(days: int, hour: int) -> str:
    base = datetime.now(timezone.utc) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


# ---------------------------------------------------------------------
# Duas clientes com nomes parecidos, telefones diferentes.
# ---------------------------------------------------------------------


def test_nomes_parecidos_telefones_diferentes_nunca_se_confundem(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    slug = org["slug"]

    token_1 = _register(p, name="Amanda Souza", phone="61911112222")["access_token"]
    token_2 = _register(p, name="Amanda Souza Lima", phone="61933334444")["access_token"]

    b1 = _book(p, slug, token_1, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(21, 9))
    b2 = _book(p, slug, token_2, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(21, 11))

    appt1 = c.get(f"/api/v1/appointments/{b1['id']}").json()
    appt2 = c.get(f"/api/v1/appointments/{b2['id']}").json()
    assert appt1["client_id"] != appt2["client_id"]


# ---------------------------------------------------------------------
# Client antigo com o MESMO nome, mas telefone diferente -> nunca reusa.
# ---------------------------------------------------------------------


def test_client_antigo_mesmo_nome_telefone_diferente_nao_e_reusado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    old_client = _create_manual_client(c, name="Juliana Alves", phone="61955556666")

    p = _public()
    token = _register(p, name="Juliana Alves", phone="61977778888")["access_token"]

    booking = _book(p, org["slug"], token, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(22, 9))
    appt = c.get(f"/api/v1/appointments/{booking['id']}").json()
    assert appt["client_id"] != old_client["id"]


# ---------------------------------------------------------------------
# O BUG REAL: Client antigo cadastrado só com `whatsapp` (phone nulo) —
# a conta nova deve encontrar ESTE Client, não duplicar.
# ---------------------------------------------------------------------


def test_client_antigo_cadastrado_so_com_whatsapp_e_encontrado_nao_duplica(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    old_client = _create_manual_client(c, name="Beatriz Prado", whatsapp="61944445555")

    p = _public()
    token = _register(p, name="Beatriz Prado", phone="61944445555")["access_token"]

    booking = _book(p, org["slug"], token, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(23, 9))
    appt = c.get(f"/api/v1/appointments/{booking['id']}").json()
    assert appt["client_id"] == old_client["id"]

    clients = c.get("/api/v1/clients").json()
    matching = [cl for cl in clients if cl["id"] == old_client["id"] or (cl.get("whatsapp") == "61944445555" or cl.get("phone") == "61944445555")]
    assert len(matching) == 1, "não pode ter duplicado o Client"


# ---------------------------------------------------------------------
# Ambiguidade: dois Clients antigos distintos compartilhando o MESMO
# número (dado legado) -> nunca escolhe arbitrariamente, cria um novo.
# ---------------------------------------------------------------------


def test_numero_ambiguo_entre_dois_clients_antigos_nunca_escolhe_arbitrariamente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    old_1 = _create_manual_client(c, name="Cliente Antiga 1", phone="61966667777")
    old_2 = _create_manual_client(c, name="Cliente Antiga 2", whatsapp="61966667777")

    p = _public()
    token = _register(p, name="Cliente Nova", phone="61966667777")["access_token"]

    booking = _book(p, org["slug"], token, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(24, 9))
    appt = c.get(f"/api/v1/appointments/{booking['id']}").json()
    assert appt["client_id"] not in {old_1["id"], old_2["id"]}


# ---------------------------------------------------------------------
# Mesma CustomerAccount, dois agendamentos -> mesmo Client sempre.
# ---------------------------------------------------------------------


def test_mesma_conta_dois_agendamentos_mesmo_client(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()

    token = _register(p, name="Cliente Fiel Dois", phone="61988889999")["access_token"]
    b1 = _book(p, org["slug"], token, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(25, 9))
    b2 = _book(p, org["slug"], token, service_id=svc["id"], professional_id=prof["id"], start_at=_next_slot(25, 11))

    appt1 = c.get(f"/api/v1/appointments/{b1['id']}").json()
    appt2 = c.get(f"/api/v1/appointments/{b2['id']}").json()
    assert appt1["client_id"] == appt2["client_id"]


# ---------------------------------------------------------------------
# Duas organizações diferentes -> client_id diferente em cada uma.
# ---------------------------------------------------------------------


def test_duas_organizacoes_geram_clients_diferentes_para_mesma_conta(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    org_a = _enable_online_booking(c_a)
    _branch_a, prof_a, svc_a = _setup_service_and_professional(c_a)

    c_b = client_as(org_b_actor)
    org_b = _enable_online_booking(c_b)
    _branch_b, prof_b, svc_b = _setup_service_and_professional(c_b)

    p = _public()
    token = _register(p, name="Cliente Multi-Salão P0", phone="61922223333")["access_token"]

    booking_a = _book(p, org_a["slug"], token, service_id=svc_a["id"], professional_id=prof_a["id"], start_at=_next_slot(26, 9))
    booking_b = _book(p, org_b["slug"], token, service_id=svc_b["id"], professional_id=prof_b["id"], start_at=_next_slot(26, 9))

    c_a = client_as(org_a_actor)
    appt_a = c_a.get(f"/api/v1/appointments/{booking_a['id']}").json()
    c_b = client_as(org_b_actor)
    appt_b = c_b.get(f"/api/v1/appointments/{booking_b['id']}").json()
    assert appt_a["client_id"] != appt_b["client_id"]


# ---------------------------------------------------------------------
# Fluxo completo: o Client criado pelo Agendamento Online aparece
# corretamente na Ficha (origem online_booking, próximo agendamento).
# ---------------------------------------------------------------------


def test_agendamento_online_aparece_na_ficha_do_client_correto(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()

    token = _register(p, name="Cliente Ficha", phone="61911119999")["access_token"]
    start_at = _next_slot(27, 9)
    booking = _book(p, org["slug"], token, service_id=svc["id"], professional_id=prof["id"], start_at=start_at)
    appt = c.get(f"/api/v1/appointments/{booking['id']}").json()
    client_id = appt["client_id"]

    profile = c.get(f"/api/v1/clients/{client_id}/profile")
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["next_appointment"] is not None
    assert body["next_appointment"]["id"] == booking["id"]
    assert any(a["id"] == booking["id"] and a.get("source") == "public_booking" for a in body["timeline"])
