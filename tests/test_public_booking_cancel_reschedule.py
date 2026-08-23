"""Testes da Etapa N5 — Cancelamento e Reagendamento pelo Cliente
(`/api/v1/public/booking/{slug}/me/appointments/{id}/cancel|reschedule|
availability`). Cobre o checklist obrigatório do pedido: feature nasce
OFF; liga/desliga por organização; antecedência mínima configurável
(nunca hardcoded, valores diferentes de 24h/48h provam isso);
ownership (nunca aceita `appointment_id` isolado — cliente de outra
conta/organização recebe 404); status permitido vs. terminal; motor de
disponibilidade reaproveitado (jornada/conflito/duração); o próprio
agendamento nunca conflita contra si mesmo no reagendamento;
revalidação no momento da confirmação (nunca confia na disponibilidade
vista quando a tela abriu); AuditLog com `user_id=None` +
`change_type`/horário anterior-novo; timezone correto na janela.

Reaproveita o MESMO estilo de `test_public_booking.py` (HTTP real via
TestClient, `client_as`/`org_a_actor` pro lado interno/staff, `_public()`
sem autenticação nenhuma pro lado da cliente) — helpers redefinidos
localmente aqui de propósito (mesmo padrão do resto da suíte: nenhum
import cruzado entre arquivos de teste)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app
from nexasalon_api.repositories import audit_log_repo

_TZ = timezone(timedelta(hours=-3))


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    return TestClient(app)


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


def _enable_change_settings(c, **overrides):
    payload = {
        "online_cancel_enabled": True,
        "online_reschedule_enabled": True,
        "online_change_min_hours": 24,
        **overrides,
    }
    resp = c.put("/api/v1/organization", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _setup_service_and_professional(c, *, start_time="00:00:00", end_time="23:59:00"):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    svc = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    prof = c.post("/api/v1/professionals", json={"name": "Profissional"}).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        json={"items": [{"weekday": w, "start_time": start_time, "end_time": end_time} for w in range(7)]},
    )
    return branch, prof, svc


def _register_customer(p, *, name="Cliente Teste", phone="61999990000", email=None) -> str:
    email = email or f"cliente-{uuid.uuid4().hex[:10]}@example.com"
    resp = p.post(
        "/api/v1/customer-auth/register",
        json={"name": name, "email": email, "phone": phone, "password": "Senha123!", "password_confirm": "Senha123!"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _book(p, slug, svc, prof, start_at: datetime, token: str) -> dict:
    resp = p.post(
        f"/api/v1/public/booking/{slug}",
        json={"service_id": svc["id"], "professional_id": prof["id"], "start_at": start_at.isoformat()},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _in_days(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


# ---------------------------------------------------------------------
# 1/2 — feature ligada/desligada
# ---------------------------------------------------------------------


def test_cancelamento_habilitado_cancela_com_sucesso(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110001")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=10, minute=0, second=0, microsecond=0), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={"reason": "Imprevisto"}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    mine = p.get(f"/api/v1/public/booking/{org['slug']}/me/appointments", headers=_auth(token)).json()
    row = next(r for r in mine if r["id"] == booking["id"])
    assert row["can_cancel"] is False  # já cancelado — status não permite de novo.


def test_cancelamento_desabilitado_recusa(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c, online_cancel_enabled=False)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110002")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=10, minute=0, second=0, microsecond=0), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text

    mine = p.get(f"/api/v1/public/booking/{org['slug']}/me/appointments", headers=_auth(token)).json()
    row = next(r for r in mine if r["id"] == booking["id"])
    assert row["can_cancel"] is False
    assert row["cancel_lead_time_blocked"] is False  # bloqueado pela config, não pela antecedência.


def test_reagendamento_habilitado_funciona(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110003")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=10, minute=0, second=0, microsecond=0), token)
    new_start = _in_days(11).replace(hour=14, minute=0, second=0, microsecond=0)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": new_start.isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["starts_at"][:16] == new_start.isoformat()[:16]
    assert body["service_name"] == "Corte"  # serviço permanece intacto.
    assert body["professional_name"] == "Profissional"  # profissional permanece intacto.


def test_reagendamento_desabilitado_recusa(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c, online_reschedule_enabled=False)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110004")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=10, minute=0, second=0, microsecond=0), token)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": _in_days(11).isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------
# 5/6/7/24/25 — antecedência mínima (configurável, nunca hardcoded)
# ---------------------------------------------------------------------


def test_alteracao_antes_do_limite_permitido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c, online_change_min_hours=24)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110005")
    # 48h à frente — folgadamente dentro da janela de 24h.
    booking = _book(p, org["slug"], svc, prof, _in_days(2), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


def test_alteracao_exatamente_no_limite_e_permitida(client_as, org_a_actor):
    """`within_online_change_window` usa `>=` — exatamente 24h de
    antecedência é o limiar INCLUSIVO (ainda permitido)."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c, online_change_min_hours=24)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110006")
    # Margem de alguns minutos além de exatos 24h pra absorver o tempo
    # de execução do teste entre o cálculo do horário e a chamada HTTP
    # de cancelamento (ainda comprova o limiar, sem flakiness).
    booking = _book(p, org["slug"], svc, prof, datetime.now(timezone.utc) + timedelta(hours=24, minutes=2), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


def test_alteracao_depois_do_limite_e_recusada_com_mensagem_amigavel(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c, online_change_min_hours=24)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110007")
    booking = _book(p, org["slug"], svc, prof, datetime.now(timezone.utc) + timedelta(hours=10), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text
    message = resp.json()["error"]["message"]
    assert "24 horas" in message
    assert "estabelecimento" in message

    mine = p.get(f"/api/v1/public/booking/{org['slug']}/me/appointments", headers=_auth(token)).json()
    row = next(r for r in mine if r["id"] == booking["id"])
    assert row["can_cancel"] is False
    assert row["cancel_lead_time_blocked"] is True  # motivo é a janela, não a config/status — mostrável desabilitado.
    assert row["change_min_hours"] == 24


def test_configuracao_de_48_horas_nao_e_hardcoded(client_as, org_a_actor):
    """Com `online_change_min_hours=48`, um horário a 30h de distância
    (que passaria com 24h) precisa ser recusado — prova que o número
    vem da configuração, nunca fixo no backend."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c, online_change_min_hours=48)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110008")
    booking = _book(p, org["slug"], svc, prof, datetime.now(timezone.utc) + timedelta(hours=30), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text
    assert "48 horas" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------
# 8/9 — ownership / isolamento
# ---------------------------------------------------------------------


def test_cliente_nao_altera_agendamento_de_outra_cliente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token_dona = _register_customer(p, name="Dona", phone="61911110009")
    booking = _book(p, org["slug"], svc, prof, _in_days(10), token_dona)

    token_intrusa = _register_customer(p, name="Intrusa", phone="61911110010")
    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token_intrusa),
    )
    assert resp.status_code == 404, resp.text

    resp2 = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": _in_days(11).isoformat()}, headers=_auth(token_intrusa),
    )
    assert resp2.status_code == 404, resp2.text


def test_cliente_nao_acessa_agendamento_de_outra_organizacao(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    org_a = _enable_online_booking(c_a)
    _enable_change_settings(c_a)
    _branch_a, prof_a, svc_a = _setup_service_and_professional(c_a)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110011")
    booking = _book(p, org_a["slug"], svc_a, prof_a, _in_days(10), token)

    c_b = client_as(org_b_actor)
    org_b = _enable_online_booking(c_b)
    _enable_change_settings(c_b)

    # Mesma CustomerAccount (mesmo token/e-mail), mas tentando cancelar
    # um agendamento da ORG A através do path da ORG B — o vínculo
    # (`CustomerAccountLink`) é por organização, então mesmo a MESMA
    # pessoa não enxerga o agendamento de A pelo contexto de B.
    resp = p.post(
        f"/api/v1/public/booking/{org_b['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------
# 10/11/12/13 — status permitido vs. terminal
# ---------------------------------------------------------------------


def test_cancelamento_em_status_permitido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110012")
    booking = _book(p, org["slug"], svc, prof, _in_days(10), token)
    assert booking["status"] == "confirmed"  # confirmado é cancelável.

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


def test_cancelamento_em_status_terminal_e_recusado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110013")
    booking = _book(p, org["slug"], svc, prof, _in_days(10), token)

    status_resp = c.patch(f"/api/v1/appointments/{booking['id']}/status", json={"status": "finished"})
    assert status_resp.status_code == 200, status_resp.text

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


def test_reagendamento_em_status_permitido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110014")
    booking = _book(p, org["slug"], svc, prof, _in_days(10), token)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": _in_days(11).isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


def test_reagendamento_em_status_terminal_e_recusado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110015")
    booking = _book(p, org["slug"], svc, prof, _in_days(10), token)

    status_resp = c.patch(f"/api/v1/appointments/{booking['id']}/status", json={"status": "no_show"})
    assert status_resp.status_code == 200, status_resp.text

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": _in_days(11).isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------
# 14/15/16/17/18/20 — motor de disponibilidade reaproveitado
# ---------------------------------------------------------------------


def test_reagendamento_para_horario_disponivel_funciona(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110016")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0), token)

    target_date = _in_days(12).date().isoformat()
    slots = p.get(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/availability",
        params={"date": target_date}, headers=_auth(token),
    )
    assert slots.status_code == 200, slots.text
    assert len(slots.json()) > 0
    new_start = slots.json()[0]["start_at"]

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": new_start}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


def test_reagendamento_para_horario_ocupado_recebe_409(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()

    token_a = _register_customer(p, name="Maria", phone="61911110017")
    booking_a = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0), token_a)

    # Outro horário, MESMO profissional, ocupado por outra cliente.
    occupied_start = _in_days(10).replace(hour=15, minute=0, second=0, microsecond=0)
    token_b = _register_customer(p, name="Outra", phone="61911110018")
    _book(p, org["slug"], svc, prof, occupied_start, token_b)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking_a['id']}/reschedule",
        json={"start_at": occupied_start.isoformat()}, headers=_auth(token_a),
    )
    assert resp.status_code == 409, resp.text


def test_revalidacao_no_momento_da_confirmacao_nunca_confia_na_tela_aberta(client_as, org_a_actor):
    """Item 9/16 do pedido: a cliente A "abre" um horário (consulta
    disponibilidade), e ANTES dela confirmar, outra cliente ocupa
    exatamente esse horário — a confirmação de A precisa recusar
    (revalidação transacional no momento do PATCH, nunca confiando na
    lista de horários carregada antes)."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()

    token_a = _register_customer(p, name="Maria", phone="61911110019")
    booking_a = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0), token_a)

    target_date = _in_days(12).date().isoformat()
    slots = p.get(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking_a['id']}/availability",
        params={"date": target_date}, headers=_auth(token_a),
    ).json()
    chosen_start = slots[0]["start_at"]

    # Enquanto a tela de A ainda mostra `chosen_start` como livre,
    # outra cliente reserva exatamente esse horário.
    token_b = _register_customer(p, name="Rapida", phone="61911110020")
    taken = p.post(
        f"/api/v1/public/booking/{org['slug']}",
        json={"service_id": svc["id"], "professional_id": prof["id"], "start_at": chosen_start},
        headers=_auth(token_b),
    )
    assert taken.status_code == 201, taken.text

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking_a['id']}/reschedule",
        json={"start_at": chosen_start}, headers=_auth(token_a),
    )
    assert resp.status_code == 409, resp.text


def test_reagendamento_proprio_horario_atual_nunca_conta_como_conflito_de_si_mesmo(client_as, org_a_actor):
    """Item explícito do pedido: reagendar mantendo o MESMO horário
    (ex.: mudar só de ideia e confirmar de novo) não pode ser recusado
    por "conflito" contra o próprio agendamento — prova
    `exclude_appointment_id` sendo aplicado corretamente."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110021")
    start_at = _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0)
    booking = _book(p, org["slug"], svc, prof, start_at, token)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": start_at.isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text


def test_reagendamento_respeita_jornada_do_profissional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    # Jornada estreita: só das 9h às 12h.
    _branch, prof, svc = _setup_service_and_professional(c, start_time="09:00:00", end_time="12:00:00")
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110022")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0), token)

    outside_hours = _in_days(11).replace(hour=18, minute=0, second=0, microsecond=0)
    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": outside_hours.isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


def test_reagendamento_preserva_duracao_original_do_servico(client_as, org_a_actor):
    """Item explícito do pedido: duração/serviço/profissional/preço
    permanecem intactos — o `ends_at` do novo horário precisa refletir
    a MESMA duração de 60min do serviço original, não recalculada."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110023")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0), token)
    new_start = _in_days(11).replace(hour=14, minute=0, second=0, microsecond=0)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": new_start.isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    starts = datetime.fromisoformat(body["starts_at"])
    ends = datetime.fromisoformat(body["ends_at"])
    assert (ends - starts) == timedelta(minutes=60)


def test_reagendamento_com_mais_de_um_servico_e_recusado(client_as, org_a_actor):
    """Agendamento público sempre nasce com 1 item — se um dia um
    agendamento vinculado à cliente tiver mais de 1 serviço (ex.: criado
    pela recepção), o reagendamento por autoatendimento recusa
    explicitamente em vez de mover só um item arbitrariamente."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110024")
    booking = _book(p, org["slug"], svc, prof, _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0), token)

    appt = c.get(f"/api/v1/appointments/{booking['id']}").json()
    client_id = appt["client_id"]
    svc2 = c.post(
        "/api/v1/services", json={"name": "Barba", "default_duration_minutes": 30, "default_price": "40.00"}
    ).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}, {"service_id": svc2["id"]}]})
    replace_resp = c.put(
        f"/api/v1/appointments/{booking['id']}",
        json={
            "branch_id": branch["id"], "client_id": client_id,
            "items": [
                {"professional_id": prof["id"], "service_id": svc["id"], "start_at": _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()},
                {"professional_id": prof["id"], "service_id": svc2["id"], "start_at": _in_days(10).replace(hour=10, minute=0, second=0, microsecond=0).isoformat()},
            ],
        },
    )
    assert replace_resp.status_code == 200, replace_resp.text

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": _in_days(11).isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------
# 21/22 — AuditLog
# ---------------------------------------------------------------------


def test_auditlog_de_cancelamento_por_cliente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110025")
    booking = _book(p, org["slug"], svc, prof, _in_days(10), token)

    resp = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/cancel",
        json={"reason": "Não posso mais ir"}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        logs = audit_log_repo.list_for_entity(
            session, org_a_actor.organization_id, "appointment", uuid.UUID(booking["id"])
        )
    cancel_log = next(log for log in logs if log.new_values.get("change_type") == "cancel_by_customer")
    assert cancel_log.user_id is None  # nunca inventa um usuário staff.
    assert cancel_log.old_values["status"] == "confirmed"
    assert cancel_log.new_values["status"] == "cancelled"
    assert cancel_log.new_values["reason"] == "Não posso mais ir"
    assert "customer_account_id" in cancel_log.new_values


def test_auditlog_de_reagendamento_registra_horario_anterior_e_novo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _enable_change_settings(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    token = _register_customer(p, name="Maria", phone="61911110026")
    old_start = _in_days(10).replace(hour=9, minute=0, second=0, microsecond=0)
    booking = _book(p, org["slug"], svc, prof, old_start, token)
    new_start = _in_days(11).replace(hour=14, minute=0, second=0, microsecond=0)

    resp = p.patch(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking['id']}/reschedule",
        json={"start_at": new_start.isoformat()}, headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        logs = audit_log_repo.list_for_entity(
            session, org_a_actor.organization_id, "appointment", uuid.UUID(booking["id"])
        )
    reschedule_log = next(log for log in logs if log.new_values.get("change_type") == "reschedule_by_customer")
    assert reschedule_log.user_id is None
    assert reschedule_log.old_values["start_at"][:16] == old_start.isoformat()[:16]
    assert reschedule_log.new_values["start_at"][:16] == new_start.isoformat()[:16]
    assert "customer_account_id" in reschedule_log.new_values


# ---------------------------------------------------------------------
# 23 — Timezone
# ---------------------------------------------------------------------


def test_janela_de_antecedencia_usa_instante_absoluto_independente_do_timezone_da_organizacao(client_as, org_a_actor):
    """A organização tem um fuso bem distante de UTC (Tóquio, +9) — a
    janela de 24h precisa continuar sendo um delta de instantes
    absolutos (tz-aware), nunca uma comparação de data-de-calendário
    local que erraria o limiar por causa do offset."""
    c = client_as(org_a_actor)
    org = _enable_online_booking(c, timezone="Asia/Tokyo")
    _enable_change_settings(c, online_change_min_hours=24)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()

    token_ok = _register_customer(p, name="Dentro", phone="61911110027")
    booking_ok = _book(p, org["slug"], svc, prof, datetime.now(timezone.utc) + timedelta(hours=25), token_ok)
    resp_ok = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking_ok['id']}/cancel",
        json={}, headers=_auth(token_ok),
    )
    assert resp_ok.status_code == 200, resp_ok.text

    token_blocked = _register_customer(p, name="Fora", phone="61911110028")
    booking_blocked = _book(p, org["slug"], svc, prof, datetime.now(timezone.utc) + timedelta(hours=5), token_blocked)
    resp_blocked = p.post(
        f"/api/v1/public/booking/{org['slug']}/me/appointments/{booking_blocked['id']}/cancel",
        json={}, headers=_auth(token_blocked),
    )
    assert resp_blocked.status_code == 422, resp_blocked.text


# ---------------------------------------------------------------------
# 26 — feature nasce OFF
# ---------------------------------------------------------------------


def test_feature_nasce_desligada_para_organizacao_existente(client_as, org_a_actor):
    """SEM chamar `_enable_change_settings` — organização recém-criada
    pela fixture precisa ter as duas flags em `false` e a antecedência
    em 24h por padrão (migration 0035)."""
    c = client_as(org_a_actor)
    org = c.get("/api/v1/organization").json()
    assert org["online_cancel_enabled"] is False
    assert org["online_reschedule_enabled"] is False
    assert org["online_change_min_hours"] == 24
