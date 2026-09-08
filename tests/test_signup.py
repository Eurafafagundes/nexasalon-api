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


def _payload(email: str | None = None, cpf: str = "111.444.777-35") -> dict:
    return {
        "full_name": "Mariana Costa",
        "email": email or f"signup-{uuid.uuid4().hex[:10]}@example.com",
        "cpf": cpf,
        "phone": "11999999999",
        "password": "SenhaSegura123!",
        "business_name": "Studio Mariana",
        "business_type": "salao",
        "business_phone": "1133334444",
        "city": "São Paulo",
        "state": "SP",
    }


def _signup(
    client: TestClient, email: str | None = None, cpf: str = "111.444.777-35"
) -> tuple[dict, dict]:
    payload = _payload(email, cpf)
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
        assert membership.user.cpf == "11144477735"

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
    assert response.json()["error"]["message"] == "Já existe uma conta com este e-mail."


def test_signup_aceita_cpf_valido_formatado_e_armazena_so_digitos(client):
    _payload_data, tokens = _signup(client)
    membership_id = uuid.UUID(tokens["membership_id"])
    organization_id = uuid.UUID(tokens["organization_id"])

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(organization_id)},
        )
        membership = session.get(OrganizationMembership, membership_id)
        assert membership is not None
        assert membership.user.cpf == "11144477735"


def test_signup_rejeita_cpf_invalido_sem_criar_conta(client):
    payload = _payload()
    payload["cpf"] = "123.456.789-00"
    response = client.post("/api/v1/signup", json=payload)

    assert response.status_code == 422
    assert "Informe um CPF válido." in response.text
    with SessionLocal() as session:
        from nexasalon_api.repositories import user_repo

        assert user_repo.get_by_email(session, payload["email"]) is None


def test_signup_rejeita_cpf_duplicado_sem_expor_usuario(client):
    _signup(client)
    payload = _payload()
    response = client.post("/api/v1/signup", json=payload)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "type": "conflict",
        "message": "Este CPF já possui uma conta no NexaSalon.",
        "details": None,
    }


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
    _two, tokens_b = _signup(client, cpf="529.982.247-25")
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

    created_organization_id = None
    real_create_organization = signup_service.organization_repo.create

    def capture_organization(*args, **kwargs):
        nonlocal created_organization_id
        organization = real_create_organization(*args, **kwargs)
        created_organization_id = organization.id
        return organization

    def fail_session(*args, **kwargs):
        raise RuntimeError("falha ao emitir sessão")

    monkeypatch.setattr(
        signup_service.organization_repo, "create", capture_organization
    )
    monkeypatch.setattr(
        signup_service.auth_service, "issue_session_tokens", fail_session
    )
    with pytest.raises(RuntimeError, match="falha ao emitir sessão"):
        signup_service.create_account(
            signup_service.SignupRequest.model_validate(payload)
        )

    with SessionLocal() as session:
        assert signup_service.user_repo.get_by_email(session, payload["email"]) is None
        assert created_organization_id is not None
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, true)"),
            {"oid": str(created_organization_id)},
        )
        assert session.get(Organization, created_organization_id) is None


def test_usuarios_antigos_sem_cpf_continuam_validos():
    from nexasalon_api.repositories import user_repo

    suffix = uuid.uuid4().hex[:10]
    with SessionLocal.begin() as session:
        first = user_repo.create(
            session, email=f"legacy-a-{suffix}@example.com", name="Legado A"
        )
        second = user_repo.create(
            session, email=f"legacy-b-{suffix}@example.com", name="Legado B"
        )
        assert first.cpf is None
        assert second.cpf is None


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


# --- Feature flag NEXASALON_PUBLIC_SIGNUP_ENABLED -------------------------


def test_public_signup_enabled_true_cadastro_continua_funcionando_como_hoje(client, monkeypatch):
    """Item 1 do checklist: com a flag explicitamente True (o default),
    nada muda — mesmo caminho feliz de sempre."""
    monkeypatch.setattr(settings, "public_signup_enabled", True)
    payload, tokens = _signup(client)
    assert tokens["access_token"]

    with SessionLocal() as session:
        from nexasalon_api.repositories import user_repo

        assert user_repo.get_by_email(session, payload["email"]) is not None


def test_public_signup_enabled_false_recusa_com_erro_estruturado_do_padrao_existente(client, monkeypatch):
    """Item 2: recusado, status correto, `error.type` estruturado
    seguindo EXATAMENTE o mesmo contrato `{"error": {"type", "message",
    "details"}}` já usado por toda a API (`main.py::handle_domain_error`)
    — nenhum formato novo."""
    monkeypatch.setattr(settings, "public_signup_enabled", False)
    payload = _payload()

    response = client.post("/api/v1/signup", json=payload)

    assert response.status_code == 403, response.text
    assert response.json()["error"] == {
        "type": "public_signup_disabled",
        "message": "Novos cadastros estão temporariamente indisponíveis. O NexaSalon está em fase de testes.",
        "details": None,
    }

    # nenhuma conta foi criada pela tentativa recusada
    with SessionLocal() as session:
        from nexasalon_api.repositories import user_repo

        assert user_repo.get_by_email(session, payload["email"]) is None


def test_public_signup_enabled_false_nunca_abre_sessao_de_banco(monkeypatch):
    """Prova estrutural (mais forte que só checar ausência de dado
    depois): com a flag desligada, `create_account` nunca sequer ABRE
    uma sessão de banco — a checagem acontece ANTES de
    `with SessionLocal()`, então uma criação parcial de
    User/Organization/Branch/Membership é estruturalmente impossível,
    não só "não aconteceu desta vez"."""
    from nexasalon_api.core.exceptions import PublicSignupDisabledError
    from nexasalon_api.services import signup as signup_service

    monkeypatch.setattr(settings, "public_signup_enabled", False)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("SessionLocal não deveria ser chamado com o cadastro público desabilitado")

    monkeypatch.setattr(signup_service, "SessionLocal", _fail_if_called)

    payload = _payload()
    with pytest.raises(PublicSignupDisabledError) as exc_info:
        signup_service.create_account(signup_service.SignupRequest.model_validate(payload))

    assert exc_info.value.message == "Novos cadastros estão temporariamente indisponíveis. O NexaSalon está em fase de testes."


def test_public_signup_enabled_false_login_de_conta_existente_continua_funcionando(client, monkeypatch):
    """Item 3: a flag nunca afeta login de quem já tem conta — só a
    CRIAÇÃO pública de conta nova."""
    monkeypatch.setattr(settings, "public_signup_enabled", True)
    payload, _tokens = _signup(client)

    monkeypatch.setattr(settings, "public_signup_enabled", False)
    response = client.post(
        "/api/v1/auth/login", json={"email": payload["email"], "password": payload["password"]}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tokens"]["access_token"]


def test_public_signup_enabled_false_fluxo_autenticado_nao_e_afetado(client, monkeypatch):
    """Item 4: com a conta já existente, operação normal do salão (aqui,
    criar um profissional — um fluxo autenticado comum) continua
    funcionando normalmente mesmo com o cadastro público fechado."""
    monkeypatch.setattr(settings, "public_signup_enabled", True)
    _payload_data, tokens = _signup(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    monkeypatch.setattr(settings, "public_signup_enabled", False)
    response = client.post(
        "/api/v1/professionals", json={"name": "Profissional Normal"}, headers=headers
    )

    assert response.status_code == 201, response.text


def test_public_signup_enabled_false_nao_afeta_convite_de_funcionario(client, monkeypatch):
    """Convite/vínculo de funcionário dentro de um salão já existente
    (`POST /users`) é um fluxo AUTENTICADO totalmente separado do
    cadastro público (`POST /signup` — sem autenticação, cria
    organização nova) — a flag não deve tocar nele."""
    monkeypatch.setattr(settings, "public_signup_enabled", True)
    _payload_data, tokens = _signup(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    role_id = next(r["id"] for r in client.get("/api/v1/roles", headers=headers).json() if r["name"] == "RECEPTIONIST")

    monkeypatch.setattr(settings, "public_signup_enabled", False)
    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/users",
        json={
            "email": f"funcionario-{suffix}@example.com",
            "name": "Funcionário Convidado",
            "role_id": role_id,
            "password": "SenhaForte123!",
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
