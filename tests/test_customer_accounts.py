"""Testes da Conta da Cliente (Etapa L, Blocos 5-11) —
`/api/v1/customer-auth/*` + `/api/v1/public/booking/{slug}/me/appointments`.
Cobre: cadastro manual, login e-mail/senha (respostas genéricas
anti-enumeração), login/cadastro via Google com verifier mockado
(`GoogleIdentityVerifier` — nunca precisa de rede/token real do Google),
Bloco 8 (mesma conta sempre reusa o mesmo `Client` na mesma organização),
Bloco 7 (uma `CustomerAccount` NUNCA acessa rota interna de funcionário),
Bloco 10 ("Meus agendamentos" nunca vaza entre contas) e Bloco 11 (teto
de agendamentos futuros ativos por conta)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nexasalon_api.core.config import settings
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app
from nexasalon_api.services.google_oauth import GoogleIdentity, get_google_verifier


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _public() -> TestClient:
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _csrf() -> dict:
    return {settings.csrf_header_name: "1"}


def _register(p, *, name: str = "Cliente Teste", phone: str | None = "61999990000", email: str | None = None) -> dict:
    email = email or f"cliente-{uuid.uuid4().hex[:10]}@example.com"
    payload = {
        "name": name,
        "email": email,
        "password": "Senha123!",
        "password_confirm": "Senha123!",
    }
    if phone is not None:
        payload["phone"] = phone
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


def _book(p, slug, token, *, service_id, professional_id, start_at):
    resp = p.post(
        f"/api/v1/public/booking/{slug}",
        json={"service_id": service_id, "professional_id": professional_id, "start_at": start_at},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------
# Bloco 5/9 — cadastro manual, login e-mail/senha.
# ---------------------------------------------------------------------


def test_registro_manual_devolve_token_pronto_pra_uso():
    p = _public()
    body = _register(p, name="Maria Cliente", phone="61988887777")
    assert body["access_token"]
    assert body["customer"]["name"] == "Maria Cliente"
    assert body["phone_required"] is False  # já veio com telefone no cadastro


def test_registro_com_email_duplicado_recebe_409():
    p = _public()
    email = f"duplicado-{uuid.uuid4().hex[:8]}@example.com"
    _register(p, email=email)
    resp = p.post(
        "/api/v1/customer-auth/register",
        json={
            "name": "Outra Pessoa", "email": email, "phone": "61988889999",
            "password": "Senha123!", "password_confirm": "Senha123!",
        },
    )
    assert resp.status_code == 409, resp.text


def test_login_com_credenciais_corretas_funciona():
    p = _public()
    email = f"login-{uuid.uuid4().hex[:8]}@example.com"
    _register(p, email=email, phone="61988880000")
    resp = p.post("/api/v1/customer-auth/login", json={"email": email, "password": "Senha123!"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


def test_login_com_senha_errada_e_email_inexistente_recebem_o_mesmo_401_generico():
    """Bloco 9 — "respostas que não facilitem enumeração de contas": os
    dois casos precisam devolver EXATAMENTE o mesmo status/mensagem."""
    p = _public()
    email = f"senha-errada-{uuid.uuid4().hex[:8]}@example.com"
    _register(p, email=email, phone="61988881111")

    wrong_password = p.post("/api/v1/customer-auth/login", json={"email": email, "password": "SenhaErrada!"})
    nonexistent = p.post(
        "/api/v1/customer-auth/login",
        json={"email": f"nao-existe-{uuid.uuid4().hex[:8]}@example.com", "password": "Qualquer123!"},
    )
    assert wrong_password.status_code == 401
    assert nonexistent.status_code == 401
    assert wrong_password.json()["error"]["message"] == nonexistent.json()["error"]["message"]


# ---------------------------------------------------------------------
# Bloco 6 — login/cadastro via Google (verifier mockado).
# ---------------------------------------------------------------------


class _FakeGoogleVerifier:
    def __init__(self, identity: GoogleIdentity) -> None:
        self._identity = identity

    def verify(self, id_token: str) -> GoogleIdentity:
        return self._identity


def test_google_login_cria_conta_nova_sem_telefone_e_marca_phone_required():
    identity = GoogleIdentity(
        subject=f"google-{uuid.uuid4().hex[:12]}", email=f"google-{uuid.uuid4().hex[:8]}@example.com",
        email_verified=True, name="Cliente Google",
    )
    app.dependency_overrides[get_google_verifier] = lambda: _FakeGoogleVerifier(identity)
    try:
        p = _public()
        resp = p.post("/api/v1/customer-auth/google", json={"id_token": "fake-token"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["customer"]["email"] == identity.email
        assert body["customer"]["phone"] is None
        assert body["phone_required"] is True

        # Bloco 6 — "pedir WhatsApp se necessário": completar o telefone
        # depois do login via Google.
        patch_resp = p.patch(
            "/api/v1/customer-auth/me", json={"phone": "61999998888"}, headers=_auth(body["access_token"])
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["phone"] == "61999998888"
    finally:
        app.dependency_overrides.pop(get_google_verifier, None)


def test_google_login_com_mesmo_subject_reusa_a_mesma_conta():
    identity = GoogleIdentity(
        subject=f"google-{uuid.uuid4().hex[:12]}", email=f"google-{uuid.uuid4().hex[:8]}@example.com",
        email_verified=True, name="Cliente Google",
    )
    app.dependency_overrides[get_google_verifier] = lambda: _FakeGoogleVerifier(identity)
    try:
        p = _public()
        first = p.post("/api/v1/customer-auth/google", json={"id_token": "fake-token-1"}).json()
        second = p.post("/api/v1/customer-auth/google", json={"id_token": "fake-token-2"}).json()
        assert first["customer"]["id"] == second["customer"]["id"]
    finally:
        app.dependency_overrides.pop(get_google_verifier, None)


def test_google_login_indisponivel_sem_client_id_configurado_retorna_503():
    # Sem override de `get_google_verifier` — settings.google_client_id
    # não está configurado no ambiente de teste (nenhuma env var setada).
    p = _public()
    resp = p.post("/api/v1/customer-auth/google", json={"id_token": "qualquer"})
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------
# Bloco 7 — CustomerAccount é uma identidade separada, NUNCA acessa rota
# interna de funcionário (nem com dev auth desligado, autenticação real).
# ---------------------------------------------------------------------


def test_customer_account_nao_acessa_endpoint_interno(monkeypatch):
    from nexasalon_api.core.config import settings

    monkeypatch.setattr(settings, "dev_auth_enabled", False)
    p = _public()
    body = _register(p, phone="61900007777")

    resp = p.get("/api/v1/clients", headers=_auth(body["access_token"]))
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------
# Bloco 8 — mesma conta SEMPRE reusa o mesmo Client na mesma organização.
# ---------------------------------------------------------------------


def test_mesma_conta_faz_duas_reservas_e_continua_com_um_unico_client(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    slug = org["slug"]

    body = _register(p, name="Cliente Fiel", phone="61900008888")
    token = body["access_token"]

    base = datetime.now(timezone.utc) + timedelta(days=17)
    first = _book(
        p, slug, token, service_id=svc["id"], professional_id=prof["id"],
        start_at=base.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
    )
    second = _book(
        p, slug, token, service_id=svc["id"], professional_id=prof["id"],
        start_at=base.replace(hour=11, minute=0, second=0, microsecond=0).isoformat(),
    )

    appt1 = c.get(f"/api/v1/appointments/{first['id']}").json()
    appt2 = c.get(f"/api/v1/appointments/{second['id']}").json()
    assert appt1["client_id"] == appt2["client_id"]


def test_mesma_conta_em_organizacoes_diferentes_gera_client_ids_diferentes(client_as, org_a_actor, org_b_actor):
    """Bloco 7 — "capacidade futura de multi-organização": a MESMA
    `CustomerAccount` pode ter um `Client` diferente em cada organização
    (o vínculo é por par conta+organização, nunca global)."""
    c_a = client_as(org_a_actor)
    org_a = _enable_online_booking(c_a)
    _branch_a, prof_a, svc_a = _setup_service_and_professional(c_a)

    c_b = client_as(org_b_actor)
    org_b = _enable_online_booking(c_b)
    _branch_b, prof_b, svc_b = _setup_service_and_professional(c_b)

    p = _public()
    body = _register(p, name="Cliente Multi-Salão", phone="61900009999")
    token = body["access_token"]

    base = datetime.now(timezone.utc) + timedelta(days=18)
    booking_a = _book(
        p, org_a["slug"], token, service_id=svc_a["id"], professional_id=prof_a["id"],
        start_at=base.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
    )
    booking_b = _book(
        p, org_b["slug"], token, service_id=svc_b["id"], professional_id=prof_b["id"],
        start_at=base.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
    )

    # `client_as` reatribui o override de ator no `app` compartilhado a
    # cada chamada — reatribuir `c_a`/`c_b` aqui de novo, na ordem certa,
    # em vez de reusar as variáveis antigas (ver docstring de `client_as`
    # em `conftest.py`).
    c_a = client_as(org_a_actor)
    appt_a = c_a.get(f"/api/v1/appointments/{booking_a['id']}").json()
    c_b = client_as(org_b_actor)
    appt_b = c_b.get(f"/api/v1/appointments/{booking_b['id']}").json()
    assert appt_a["client_id"] != appt_b["client_id"]


# ---------------------------------------------------------------------
# Bloco 10 — "Meus agendamentos" nunca vaza entre contas diferentes.
# ---------------------------------------------------------------------


def test_meus_agendamentos_nao_vaza_entre_contas_diferentes(client_as, org_a_actor):
    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    slug = org["slug"]

    token_a = _register(p, name="Cliente A", phone="61900010000")["access_token"]
    token_b = _register(p, name="Cliente B", phone="61900011111")["access_token"]

    start_at = (datetime.now(timezone.utc) + timedelta(days=19)).replace(
        hour=9, minute=0, second=0, microsecond=0
    ).isoformat()
    booking = _book(p, slug, token_a, service_id=svc["id"], professional_id=prof["id"], start_at=start_at)

    my_a = p.get(f"/api/v1/public/booking/{slug}/me/appointments", headers=_auth(token_a))
    my_b = p.get(f"/api/v1/public/booking/{slug}/me/appointments", headers=_auth(token_b))
    assert my_a.status_code == 200, my_a.text
    assert my_b.status_code == 200, my_b.text
    assert any(item["id"] == booking["id"] for item in my_a.json())
    assert all(item["id"] != booking["id"] for item in my_b.json())
    assert my_b.json() == []


# ---------------------------------------------------------------------
# Bloco 11 — teto de agendamentos futuros ATIVOS por conta (anti-fake).
# ---------------------------------------------------------------------


def test_teto_de_agendamentos_futuros_ativos_por_conta(client_as, org_a_actor, monkeypatch):
    from nexasalon_api.core.config import settings

    monkeypatch.setattr(settings, "max_active_future_appointments_per_customer", 1)

    c = client_as(org_a_actor)
    org = _enable_online_booking(c)
    _branch, prof, svc = _setup_service_and_professional(c)
    p = _public()
    slug = org["slug"]

    token = _register(p, name="Cliente Ansiosa", phone="61900012222")["access_token"]
    base = datetime.now(timezone.utc) + timedelta(days=20)

    first = p.post(
        f"/api/v1/public/booking/{slug}",
        json={
            "service_id": svc["id"], "professional_id": prof["id"],
            "start_at": base.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
        },
        headers=_auth(token),
    )
    assert first.status_code == 201, first.text

    second = p.post(
        f"/api/v1/public/booking/{slug}",
        json={
            "service_id": svc["id"], "professional_id": prof["id"],
            "start_at": base.replace(hour=11, minute=0, second=0, microsecond=0).isoformat(),
        },
        headers=_auth(token),
    )
    assert second.status_code == 422, second.text


# ---------------------------------------------------------------------
# Ajuste pós-Etapa L — persistência segura da sessão da cliente: cookie
# HttpOnly `nexasalon_customer_refresh_token` + rotação + detecção de
# reuso + logout, espelhando `test_auth.py` (funcionário), tabela e
# cookie 100% separados.
# ---------------------------------------------------------------------


def test_registro_seta_cookie_de_refresh_proprio_da_cliente():
    p = _public()
    _register(p, phone="61900020000")
    assert settings.customer_refresh_cookie_name in p.cookies
    # nunca o cookie de staff, mesmo por acidente de nome
    assert p.cookies.get(settings.customer_refresh_cookie_name) != p.cookies.get("nexasalon_refresh_token")
    assert "refresh_token" not in _register(_public(), phone="61900020001"), (
        "refresh token bruto nunca pode ir no corpo JSON"
    )


def test_refresh_emite_novo_par_de_tokens_via_cookie_da_cliente():
    p = _public()
    body = _register(p, phone="61900020002")
    old_cookie = p.cookies.get(settings.customer_refresh_cookie_name)

    resp = p.post("/api/v1/customer-auth/refresh", headers=_csrf())
    assert resp.status_code == 200, resp.text
    new_body = resp.json()
    assert new_body["access_token"] != body["access_token"]
    assert new_body["customer"]["id"] == body["customer"]["id"]
    assert "refresh_token" not in new_body
    assert p.cookies.get(settings.customer_refresh_cookie_name) != old_cookie


def test_refresh_sem_cookie_da_cliente():
    p = _public()
    resp = p.post("/api/v1/customer-auth/refresh", headers=_csrf())
    assert resp.status_code == 401


def test_refresh_reuso_de_token_ja_rotacionado_revoga_todas_as_sessoes_da_cliente():
    """Mesmo cenário de `test_auth.py::test_refresh_reuso_de_token_ja_rotacionado_revoga_tudo`,
    mas na tabela `customer_refresh_tokens` — reapresentar um refresh
    token já rotacionado é tratado como possível roubo e revoga TODAS as
    sessões ativas daquela conta, inclusive a nova (multi-dispositivo)."""
    from nexasalon_api.core.exceptions import UnauthorizedError
    from nexasalon_api.services import customer_accounts as customer_accounts_service

    p = _public()
    _register(p, phone="61900020003")
    old_raw_refresh = p.cookies.get(settings.customer_refresh_cookie_name)

    p.post("/api/v1/customer-auth/refresh", headers=_csrf())  # rotaciona -> old_raw_refresh fica revogado
    new_raw_refresh = p.cookies.get(settings.customer_refresh_cookie_name)
    assert new_raw_refresh != old_raw_refresh

    from nexasalon_api.core.db import SessionLocal

    session = SessionLocal()
    try:
        with pytest.raises(UnauthorizedError):
            customer_accounts_service.refresh_session(session, old_raw_refresh)
        session.commit()
    finally:
        session.close()

    # a sessão nova (emitida pela própria rotação) também foi revogada em
    # cascata — a cliente precisa logar de novo em TODOS os dispositivos.
    session = SessionLocal()
    try:
        with pytest.raises(UnauthorizedError):
            customer_accounts_service.refresh_session(session, new_raw_refresh)
        session.commit()
    finally:
        session.close()


def test_refresh_token_de_cliente_expirado_e_rejeitado():
    from nexasalon_api.core.db import SessionLocal
    from nexasalon_api.core.exceptions import UnauthorizedError
    from nexasalon_api.core.security import hash_opaque_token
    from nexasalon_api.repositories import customer_refresh_token_repo
    from nexasalon_api.services import customer_accounts as customer_accounts_service

    p = _public()
    body = _register(p, phone="61900020004")

    session = SessionLocal()
    try:
        raw = f"token-expirado-de-teste-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        customer_refresh_token_repo.create(
            session,
            customer_account_id=uuid.UUID(body["customer"]["id"]),
            token_hash=hash_opaque_token(raw),
            issued_at=now - timedelta(days=40),
            expires_at=now - timedelta(days=10),
        )
        session.commit()
        with pytest.raises(UnauthorizedError):
            customer_accounts_service.refresh_session(session, raw)
        session.commit()
    finally:
        session.close()


def test_refresh_token_de_cliente_invalido():
    from nexasalon_api.core.db import SessionLocal
    from nexasalon_api.core.exceptions import UnauthorizedError
    from nexasalon_api.services import customer_accounts as customer_accounts_service

    session = SessionLocal()
    try:
        with pytest.raises(UnauthorizedError):
            customer_accounts_service.refresh_session(session, "nunca-existiu")
        session.commit()
    finally:
        session.close()


def test_refresh_da_cliente_sem_header_csrf_e_negado():
    p = _public()
    _register(p, phone="61900020005")
    resp = p.post("/api/v1/customer-auth/refresh")  # sem o header CSRF
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_logout_revoga_o_refresh_token_da_cliente():
    p = _public()
    _register(p, phone="61900020006")

    logout_resp = p.post("/api/v1/customer-auth/logout", headers=_csrf())
    assert logout_resp.status_code == 204
    assert settings.customer_refresh_cookie_name not in p.cookies, "cookie deveria ser limpo no logout"

    refresh_resp = p.post("/api/v1/customer-auth/refresh", headers=_csrf())
    assert refresh_resp.status_code == 401


def test_logout_da_cliente_sem_cookie_e_idempotente():
    p = _public()
    resp = p.post("/api/v1/customer-auth/logout", headers=_csrf())
    assert resp.status_code == 204


def test_logout_da_cliente_sem_header_csrf_e_negado():
    p = _public()
    _register(p, phone="61900020007")
    resp = p.post("/api/v1/customer-auth/logout")  # sem o header
    assert resp.status_code == 403


def test_logout_revoga_apenas_a_sessao_do_dispositivo_apresentado():
    """Multi-dispositivo: duas sessões independentes da mesma conta
    (dois "aparelhos" simulados por dois `TestClient`, cada um com seu
    próprio cookie jar). Logout num deles não derruba o outro."""
    p1 = _public()
    body = _register(p1, phone="61900020008")

    p2 = _public()
    login2 = p2.post(
        "/api/v1/customer-auth/login",
        json={"email": body["customer"]["email"], "password": "Senha123!"},
    )
    assert login2.status_code == 200, login2.text

    logout1 = p1.post("/api/v1/customer-auth/logout", headers=_csrf())
    assert logout1.status_code == 204

    refresh1 = p1.post("/api/v1/customer-auth/refresh", headers=_csrf())
    assert refresh1.status_code == 401, "sessão de p1 devia estar revogada"

    refresh2 = p2.post("/api/v1/customer-auth/refresh", headers=_csrf())
    assert refresh2.status_code == 200, "sessão de p2 (outro dispositivo) não devia ser afetada"


def test_reabertura_do_navegador_restaura_sessao_via_refresh():
    """Simula F5/reabertura: só o cookie HttpOnly sobrevive (o
    access_token vivia em memória e "sumiu"); `/refresh` sozinho basta
    pra restaurar a sessão sem novo login."""
    p = _public()
    body = _register(p, name="Cliente Fiel Reload", phone="61900020009")

    resp = p.post("/api/v1/customer-auth/refresh", headers=_csrf())
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer"]["id"] == body["customer"]["id"]
    assert resp.json()["customer"]["name"] == "Cliente Fiel Reload"


# ---------------------------------------------------------------------
# Isolamento staff × customer (Bloco 7) — agora também no nível de
# SESSÃO/refresh, não só de rota: token/refresh de um mundo nunca é
# aceito no outro, mesmo direto no service layer.
# ---------------------------------------------------------------------


def test_customer_access_token_nao_acessa_rota_de_staff(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_enabled", False)
    p = _public()
    body = _register(p, phone="61900020010")

    resp = p.get("/api/v1/auth/me", headers=_auth(body["access_token"]))
    assert resp.status_code == 401, resp.text


def test_staff_access_token_nao_acessa_rota_de_cliente(monkeypatch):
    from nexasalon_api.core.db import SessionLocal
    from tests.test_auth import _login, _new_membership, _new_org, _new_user, _role_id

    monkeypatch.setattr(settings, "dev_auth_enabled", False)

    session = SessionLocal()
    org = _new_org(session, "Isolamento")
    owner_role = _role_id(session, "OWNER")
    password = "Senha123!"
    user = _new_user(session, password)
    _new_membership(session, user, org, owner_role)
    staff_email = user.email
    session.commit()
    session.close()

    staff_client = TestClient(app)
    staff_body = _login(staff_client, staff_email, password)

    resp = staff_client.get("/api/v1/customer-auth/me", headers=_auth(staff_body["tokens"]["access_token"]))
    assert resp.status_code == 401, resp.text


def test_refresh_token_de_staff_nao_e_aceito_pelo_service_da_cliente_e_vice_versa(monkeypatch):
    """Prova a separação também no nível de TABELA: o refresh token
    (valor bruto, cookie) de uma sessão de staff não existe em
    `customer_refresh_tokens`, e o de cliente não existe em
    `refresh_tokens` — cada service só enxerga a própria tabela."""
    from nexasalon_api.core.db import SessionLocal
    from nexasalon_api.core.exceptions import UnauthorizedError
    from nexasalon_api.services import auth as staff_auth_service
    from nexasalon_api.services import customer_accounts as customer_accounts_service
    from tests.test_auth import _login, _new_membership, _new_org, _new_user, _role_id

    monkeypatch.setattr(settings, "dev_auth_enabled", False)

    session = SessionLocal()
    org = _new_org(session, "Isolamento2")
    owner_role = _role_id(session, "OWNER")
    password = "Senha123!"
    user = _new_user(session, password)
    _new_membership(session, user, org, owner_role)
    staff_email = user.email
    session.commit()
    session.close()

    staff_client = TestClient(app)
    _login(staff_client, staff_email, password)
    staff_raw_refresh = staff_client.cookies.get(settings.refresh_cookie_name)
    assert staff_raw_refresh

    customer_client = _public()
    _register(customer_client, phone="61900020011")
    customer_raw_refresh = customer_client.cookies.get(settings.customer_refresh_cookie_name)
    assert customer_raw_refresh
    assert customer_raw_refresh != staff_raw_refresh

    inner_session = SessionLocal()
    try:
        with pytest.raises(UnauthorizedError):
            customer_accounts_service.refresh_session(inner_session, staff_raw_refresh)
        inner_session.commit()
    finally:
        inner_session.close()

    with pytest.raises(UnauthorizedError):
        staff_auth_service.refresh(customer_raw_refresh)
