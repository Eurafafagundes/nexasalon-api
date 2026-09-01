import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.main import app
from nexasalon_api.models.enums import MembershipStatus, OrganizationStatus
from nexasalon_api.models.identity import OrganizationMembership
from nexasalon_api.models.organization import Organization
from nexasalon_api.models.professional import Professional


@pytest.fixture(autouse=True)
def _real_auth_and_no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    yield
    rate_limiter.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _payload(email: str | None = None) -> dict:
    return {
        "full_name": "Mariana Costa",
        "email": email or f"signup-{uuid.uuid4().hex[:10]}@example.com",
        "phone": "11999999999",
        "password": "SenhaSegura123!",
        "business_name": "Studio Mariana",
        "business_type": "salao",
        "business_phone": "1133334444",
        "city": "São Paulo",
        "state": "SP",
    }


def _signup(client: TestClient, email: str | None = None) -> tuple[dict, dict]:
    payload = _payload(email)
    response = client.post("/api/v1/signup", json=payload)
    assert response.status_code == 201, response.text
    return payload, response.json()["tokens"]


def test_signup_cria_tenant_owner_branch_trial_e_sessao(client):
    payload, tokens = _signup(client)
    organization_id = uuid.UUID(tokens["organization_id"])
    membership_id = uuid.UUID(tokens["membership_id"])

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(organization_id)},
        )
        organization = session.get(Organization, organization_id)
        membership = session.get(OrganizationMembership, membership_id)
        professional_count = (
            session.query(Professional)
            .filter_by(organization_id=organization_id)
            .count()
        )

        assert organization is not None
        assert organization.name == payload["business_name"]
        assert organization.status == OrganizationStatus.TRIAL
        assert organization.professional_limit == 3
        assert organization.trial_started_at is not None
        assert organization.trial_ends_at - organization.trial_started_at == timedelta(
            days=14
        )
        assert len(organization.branches) == 1
        assert membership is not None
        assert membership.status == MembershipStatus.ACTIVE
        assert membership.role.name == "OWNER"
        assert membership.professional is None
        assert professional_count == 0

    me = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["membership"]["role_name"] == "OWNER"


def test_signup_rejeita_email_duplicado(client):
    email = f"duplicado-{uuid.uuid4().hex[:8]}@example.com"
    _signup(client, email)
    response = client.post("/api/v1/signup", json=_payload(email))
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Este e-mail já está cadastrado."


def test_signup_nao_aceita_role_tenant_permissions_ou_trial_do_payload(client):
    payload = _payload()
    payload.update(
        {"role": "SUPERADMIN", "tenant_id": str(uuid.uuid4()), "permissions": ["*"]}
    )
    response = client.post("/api/v1/signup", json=payload)
    assert response.status_code == 422


def test_trial_permite_tres_profissionais_e_rejeita_o_quarto(client):
    _payload_data, tokens = _signup(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    for index in range(3):
        response = client.post(
            "/api/v1/professionals",
            json={"name": f"Profissional {index + 1}"},
            headers=headers,
        )
        assert response.status_code == 201, response.text

    fourth = client.post(
        "/api/v1/professionals", json={"name": "Profissional 4"}, headers=headers
    )
    assert fourth.status_code == 422
    assert (
        fourth.json()["error"]["message"]
        == "O período de teste permite no máximo 3 profissionais."
    )


def test_limite_de_profissionais_e_isolado_por_tenant(client):
    _one, tokens_a = _signup(client)
    _two, tokens_b = _signup(client)
    headers_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}

    for index in range(3):
        assert (
            client.post(
                "/api/v1/professionals", json={"name": f"A {index}"}, headers=headers_a
            ).status_code
            == 201
        )

    assert (
        client.post(
            "/api/v1/professionals", json={"name": "B 1"}, headers=headers_b
        ).status_code
        == 201
    )


def test_signup_faz_rollback_quando_a_criacao_falha(client, monkeypatch):
    from nexasalon_api.services import signup as signup_service

    payload = _payload()

    def fail_branch(*args, **kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(signup_service.branch_repo, "create", fail_branch)
    with pytest.raises(RuntimeError, match="falha simulada"):
        signup_service.create_account(
            signup_service.SignupRequest.model_validate(payload)
        )

    with SessionLocal() as session:
        assert signup_service.user_repo.get_by_email(session, payload["email"]) is None


def test_signup_faz_rollback_se_a_emissao_da_sessao_falhar(monkeypatch):
    from nexasalon_api.services import signup as signup_service

    payload = _payload()

    def fail_session(*args, **kwargs):
        raise RuntimeError("falha ao emitir sessão")

    monkeypatch.setattr(
        signup_service.auth_service, "issue_session_tokens", fail_session
    )
    with pytest.raises(RuntimeError, match="falha ao emitir sessão"):
        signup_service.create_account(
            signup_service.SignupRequest.model_validate(payload)
        )

    with SessionLocal() as session:
        assert signup_service.user_repo.get_by_email(session, payload["email"]) is None


def test_trial_expirado_nao_bloqueia_novo_profissional(client):
    _payload_data, tokens = _signup(client)
    organization_id = uuid.UUID(tokens["organization_id"])

    with SessionLocal.begin() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(organization_id)},
        )
        organization = session.get(Organization, organization_id)
        assert organization is not None
        organization.trial_ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    for index in range(4):
        response = client.post(
            "/api/v1/professionals",
            json={"name": f"Profissional pós-trial {index + 1}"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
