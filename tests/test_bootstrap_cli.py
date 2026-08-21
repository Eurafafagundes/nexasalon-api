"""Etapa 3C — CLI de bootstrap do primeiro OWNER
(`cli/bootstrap_owner.py`). Verifica que ele cria Organization + Branch
+ User + Membership ACTIVE com o role de sistema OWNER, e — a prova
mais forte — que o usuário criado consegue de fato fazer login pela
rota HTTP real (`POST /auth/login`, não pelo caminho DEV ONLY nem por
`client_as`/dependency_override)."""
import re
import uuid

from sqlalchemy import select, text

from nexasalon_api.cli import bootstrap_owner
from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.enums import MembershipStatus
from nexasalon_api.models.identity import OrganizationMembership, User
from nexasalon_api.models.organization import Branch, Organization

_ORG_ID_RE = re.compile(r"organization_id:\s*([0-9a-fA-F-]{36})")


def _run_bootstrap(monkeypatch, *, org_slug, branch_slug, owner_email, password="senha-bem-forte-123"):
    inputs = iter(
        [
            "Salão Bootstrap Test",
            org_slug,
            "Matriz",
            branch_slug,
            "Dona do Salão",
            owner_email,
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(bootstrap_owner, "getpass", lambda prompt="": password)
    return bootstrap_owner.main()


def _scoped_session(org_id: str):
    """Sessão com `app.current_org_id` setado — necessário pra ver, sob
    RLS, as linhas de uma organização que acabou de ser criada pelo CLI
    (verificação de teste, não é bypass: é o mesmo contexto que a própria
    organização usaria)."""
    session = SessionLocal()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": org_id})
    return session


def test_bootstrap_cria_organization_branch_user_membership_owner(monkeypatch, capsys):
    org_slug = f"salao-bootstrap-{uuid.uuid4().hex[:8]}"
    branch_slug = "matriz"
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.com"

    exit_code = _run_bootstrap(monkeypatch, org_slug=org_slug, branch_slug=branch_slug, owner_email=owner_email)
    assert exit_code == 0

    printed = capsys.readouterr().out
    match = _ORG_ID_RE.search(printed)
    assert match, f"organization_id não encontrado na saída do CLI: {printed!r}"
    org_id = match.group(1)

    with _scoped_session(org_id) as session:
        organization = session.execute(select(Organization).where(Organization.slug == org_slug)).scalar_one()
        assert str(organization.id) == org_id
        branch = session.execute(select(Branch).where(Branch.organization_id == organization.id)).scalar_one()
        assert branch.slug == branch_slug

        # users não tem RLS (global) — consulta funciona independente do
        # contexto de org setado acima.
        user = session.execute(select(User).where(User.email == owner_email.lower())).scalar_one()
        assert user.is_active is True
        assert user.password_hash is not None

        membership = session.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.organization_id == organization.id,
            )
        ).scalar_one()
        assert membership.status == MembershipStatus.ACTIVE
        assert membership.role.name == "OWNER"
        assert membership.role.organization_id is None  # role de sistema, não fabricado


def test_bootstrap_normaliza_slug_com_espacos_maiusculas_e_acentos(monkeypatch, capsys):
    """Item "Agendamento Online — slug": o único caminho de CRIAÇÃO de
    organização (não existe rota HTTP de criação, só este CLI) gravava o
    slug cru digitado, sem passar pela mesma `normalize_slug` já usada no
    PATCH (`OrganizationUpdate`). Ex.: "Mega Hair Studio" tinha que virar
    "mega-hair-studio" — nunca ficar com espaço/maiúscula/acento."""
    org_id_suffix = uuid.uuid4().hex[:8]
    raw_org_slug = f"Mega Hair Studio {org_id_suffix}"
    raw_branch_slug = "Unidade Ãgua Fresca"
    owner_email = f"owner-normaliza-{org_id_suffix}@example.com"

    exit_code = _run_bootstrap(
        monkeypatch, org_slug=raw_org_slug, branch_slug=raw_branch_slug, owner_email=owner_email
    )
    assert exit_code == 0

    printed = capsys.readouterr().out
    match = _ORG_ID_RE.search(printed)
    assert match, f"organization_id não encontrado na saída do CLI: {printed!r}"
    org_id = match.group(1)

    expected_org_slug = f"mega-hair-studio-{org_id_suffix}"
    expected_branch_slug = "unidade-agua-fresca"
    assert "normalizado para" in printed

    with _scoped_session(org_id) as session:
        organization = session.execute(select(Organization).where(Organization.id == uuid.UUID(org_id))).scalar_one()
        assert organization.slug == expected_org_slug
        branch = session.execute(select(Branch).where(Branch.organization_id == organization.id)).scalar_one()
        assert branch.slug == expected_branch_slug


def test_bootstrap_recusa_slug_que_normaliza_para_vazio(monkeypatch):
    """Guarda-costas simétrico: um slug feito só de caracteres inválidos
    (ex.: "!!!") normaliza para string vazia — o CLI precisa abortar em
    vez de criar uma organização com slug vazio/ambíguo."""
    exit_code = _run_bootstrap(
        monkeypatch,
        org_slug="!!!",
        branch_slug="matriz",
        owner_email=f"owner-vazio-{uuid.uuid4().hex[:8]}@example.com",
    )
    assert exit_code == 1


def test_bootstrap_recusa_slug_duplicado(monkeypatch):
    org_slug = f"salao-dup-{uuid.uuid4().hex[:8]}"
    first_email = f"owner-a-{uuid.uuid4().hex[:8]}@example.com"
    second_email = f"owner-b-{uuid.uuid4().hex[:8]}@example.com"

    assert _run_bootstrap(monkeypatch, org_slug=org_slug, branch_slug="matriz", owner_email=first_email) == 0
    exit_code = _run_bootstrap(monkeypatch, org_slug=org_slug, branch_slug="matriz-2", owner_email=second_email)
    assert exit_code == 1


def test_bootstrap_recusa_email_duplicado(monkeypatch):
    owner_email = f"owner-dup-{uuid.uuid4().hex[:8]}@example.com"

    assert _run_bootstrap(
        monkeypatch, org_slug=f"salao-{uuid.uuid4().hex[:8]}", branch_slug="matriz", owner_email=owner_email
    ) == 0
    exit_code = _run_bootstrap(
        monkeypatch, org_slug=f"salao-{uuid.uuid4().hex[:8]}", branch_slug="matriz", owner_email=owner_email
    )
    assert exit_code == 1


def test_owner_criado_pelo_bootstrap_consegue_logar_pela_api_real(dev_client, monkeypatch):
    org_slug = f"salao-login-{uuid.uuid4().hex[:8]}"
    branch_slug = "matriz"
    owner_email = f"owner-login-{uuid.uuid4().hex[:8]}@example.com"
    password = "outra-senha-bem-forte-456"

    exit_code = _run_bootstrap(
        monkeypatch, org_slug=org_slug, branch_slug=branch_slug, owner_email=owner_email, password=password
    )
    assert exit_code == 0

    # login real, SEM dependency_override — exercita `_get_real_current_actor`.
    # `/auth/login` já não depende de `get_current_actor`, então este
    # POST por si só prova autenticação real (hash de senha + emissão de
    # JWT), independente de qualquer atalho de dev.
    resp = dev_client.post("/api/v1/auth/login", json={"email": owner_email, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "session"
    assert body["tokens"]["access_token"]

    # `/auth/me` passa por `get_current_actor`, que na suíte de testes
    # roda com `NEXASALON_DEV_AUTH_ENABLED=true` (conftest.py) e por isso
    # SEMPRE curto-circuita pro ator DEV ONLY fixo, ignorando qualquer
    # Bearer token — inclusive um token real e válido como o de cima.
    # Isso é comportamento correto do dependency (documentado em
    # `dev_client`), não um bug: só desligamos o atalho aqui, pontualmente,
    # pra provar que o Bearer token emitido pro OWNER recém-criado também
    # funciona pelo caminho real (`_get_real_current_actor`).
    monkeypatch.setattr(settings, "dev_auth_enabled", False)
    me = dev_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['tokens']['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["membership"]["role_name"] == "OWNER"
