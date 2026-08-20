"""Testes obrigatórios da Etapa 2D — autenticação, RBAC (incluindo as
rotas herdadas da Etapa 2C), transporte de tokens (cookie + CSRF),
convite de usuário e rate limiting. Roda com PostgreSQL descartável real
e RLS de verdade (via conftest.py), como todo o resto da suite.

Diferente dos outros arquivos de teste (que usam `client_as`/`dev_client`
para simular o `ActorContext` via `dependency_overrides`), aqui o fluxo é
ponta a ponta de verdade: login com e-mail/senha, JWT emitido, cookie de
refresh setado pelo browser simulado (`TestClient` mantém cookie jar por
instância, como um browser real). Por isso o fixture `_real_auth_mode`
abaixo desliga o modo DEV só para os testes deste módulo — o resto da
suite continua rodando com `NEXASALON_DEV_AUTH_ENABLED=true` normalmente.

Rate limiting fica DESLIGADO por padrão neste módulo (fixture
`_disable_rate_limiting` abaixo): o limitador é um singleton em memória
compartilhado por todo o processo de teste, e este arquivo sozinho faz
dezenas de POST /auth/login — sem desligar, os últimos testes do arquivo
tomariam 429 por causa dos primeiros. `test_rate_limit_login_bloqueia_apos_o_limite`
religa e testa isoladamente."""
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.rate_limit import rate_limiter
from nexasalon_api.core.security import hash_password
from nexasalon_api.main import app
from nexasalon_api.models.enums import MembershipStatus, PermissionEffect
from nexasalon_api.models.identity import MembershipPermissionOverride, OrganizationMembership, User
from nexasalon_api.models.organization import Organization


@pytest.fixture(autouse=True)
def _real_auth_mode(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_enabled", False)


@pytest.fixture(autouse=True)
def _disable_rate_limiting(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    yield
    rate_limiter.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _csrf_headers(access_token: str | None = None) -> dict:
    headers = {settings.csrf_header_name: "1"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _role_id(session, name: str) -> uuid.UUID:
    return session.execute(
        text("SELECT id FROM roles WHERE name = :n AND organization_id IS NULL"), {"n": name}
    ).scalar()


def _new_org(session, label: str) -> Organization:
    # Organization tem RLS estrito (id = app.current_org_id) — precisa
    # gerar o id no cliente e setar a variável de sessão ANTES do INSERT,
    # já que ainda não existe nenhuma linha cujo id possamos reusar
    # (mesmo truque usado em conftest.seed_organization).
    org_id = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(org_id)})
    slug = f"{label}-{uuid.uuid4().hex[:8]}"
    org = Organization(id=org_id, name=f"Organização {label}", slug=slug)
    session.add(org)
    session.flush()
    return org


def _new_user(session, password: str = "Senha123!") -> User:
    user = User(
        email=f"user-{uuid.uuid4().hex[:10]}@example.com",
        name="Usuário de Teste",
        password_hash=hash_password(password),
    )
    session.add(user)
    session.flush()
    return user


def _new_membership(
    session, user: User, org: Organization, role_id: uuid.UUID, status: MembershipStatus = MembershipStatus.ACTIVE
) -> OrganizationMembership:
    # Mesma observação de _new_org: garante que app.current_org_id está
    # apontando para a organização certa antes do INSERT, já que várias
    # organizações são criadas na mesma sessão/transação de setup.
    session.execute(text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(org.id)})
    membership = OrganizationMembership(
        user_id=user.id, organization_id=org.id, role_id=role_id, status=status
    )
    session.add(membership)
    session.flush()
    return membership


class Scenario:
    """Guarda ids simples (uuid/str) — nunca objetos ORM presos a uma
    sessão já fechada."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture()
def scenario() -> Scenario:
    session = SessionLocal()

    org_a = _new_org(session, "A")
    org_b = _new_org(session, "B")
    # Captura os ids em variáveis Python IMEDIATAMENTE (a sessão ainda
    # está com o flush feito, não expirado) — nunca depois do commit.
    # `SET LOCAL`/`set_config(..., true)` desfaz seu efeito no commit; a
    # primeira vez que uma GUC custom (`app.current_org_id`) é setada
    # numa conexão, o Postgres reverte pra STRING VAZIA (não NULL) depois
    # do commit — então um SELECT feito depois do commit (como o refresh
    # automático de atributo expirado do SQLAlchemy) quebraria com
    # `invalid input syntax for type uuid: ""` ao avaliar a policy RLS.
    org_a_id, org_b_id = org_a.id, org_b.id

    owner_role = _role_id(session, "OWNER")
    admin_role = _role_id(session, "ADMIN")
    recep_role = _role_id(session, "RECEPTIONIST")
    prof_role = _role_id(session, "PROFESSIONAL")

    password = "Senha123!"

    single_org_user = _new_user(session, password)
    single_org_email, single_org_user_id = single_org_user.email, single_org_user.id
    single_membership = _new_membership(session, single_org_user, org_a, owner_role)
    single_membership_id = single_membership.id

    multi_org_user = _new_user(session, password)
    multi_org_email, multi_org_user_id = multi_org_user.email, multi_org_user.id
    multi_membership_a = _new_membership(session, multi_org_user, org_a, admin_role)
    multi_membership_a_id = multi_membership_a.id
    multi_membership_b = _new_membership(session, multi_org_user, org_b, recep_role)
    multi_membership_b_id = multi_membership_b.id

    inactive_user = _new_user(session, password)
    inactive_email, inactive_user_id = inactive_user.email, inactive_user.id
    inactive_membership = _new_membership(
        session, inactive_user, org_a, recep_role, status=MembershipStatus.SUSPENDED
    )
    inactive_membership_id = inactive_membership.id

    prof_user = _new_user(session, password)
    prof_email, prof_user_id = prof_user.email, prof_user.id
    prof_membership = _new_membership(session, prof_user, org_a, prof_role)
    prof_membership_id = prof_membership.id

    recep_user = _new_user(session, password)
    recep_email, recep_user_id = recep_user.email, recep_user.id
    recep_membership = _new_membership(session, recep_user, org_a, recep_role)
    recep_membership_id = recep_membership.id

    session.commit()
    session.close()

    return Scenario(
        password=password,
        org_a_id=org_a_id,
        org_b_id=org_b_id,
        owner_role=owner_role,
        admin_role=admin_role,
        recep_role=recep_role,
        prof_role=prof_role,
        single_org_email=single_org_email,
        single_org_user_id=single_org_user_id,
        single_membership_id=single_membership_id,
        multi_org_email=multi_org_email,
        multi_org_user_id=multi_org_user_id,
        multi_membership_a_id=multi_membership_a_id,
        multi_membership_b_id=multi_membership_b_id,
        inactive_email=inactive_email,
        inactive_user_id=inactive_user_id,
        inactive_membership_id=inactive_membership_id,
        prof_email=prof_email,
        prof_user_id=prof_user_id,
        prof_membership_id=prof_membership_id,
        recep_email=recep_email,
        recep_user_id=recep_user_id,
        recep_membership_id=recep_membership_id,
    )


def _login(client: TestClient, email: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


# ---------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------


def test_login_com_credenciais_corretas_single_org(client, scenario):
    body = _login(client, scenario.single_org_email, scenario.password)
    assert body["status"] == "session"
    assert body["tokens"]["organization_id"] == str(scenario.org_a_id)
    assert body["tokens"]["access_token"]
    assert "refresh_token" not in body["tokens"], "refresh token não pode ir no corpo JSON"
    assert settings.refresh_cookie_name in client.cookies, "refresh token deveria vir como cookie"


def test_login_senha_incorreta(client, scenario):
    resp = client.post(
        "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": "senha-errada"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "unauthorized"


def test_login_email_inexistente_retorna_mesma_mensagem_que_senha_incorreta(client, scenario):
    r1 = client.post("/api/v1/auth/login", json={"email": "ninguem@example.com", "password": "qualquer"})
    r2 = client.post(
        "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": "senha-errada"}
    )
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]


def test_login_membership_inativa_nao_concede_acesso(client, scenario):
    """Único membership do usuário está SUSPENDED — login deve negar
    (nenhuma organização ativa disponível), não apenas "funcionar mesmo
    assim"."""
    resp = client.post(
        "/api/v1/auth/login", json={"email": scenario.inactive_email, "password": scenario.password}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_login_multiplas_empresas_pede_selecao(client, scenario):
    body = _login(client, scenario.multi_org_email, scenario.password)
    assert body["status"] == "select_organization"
    assert body["org_selection_token"]
    org_ids = {o["organization_id"] for o in body["organizations"]}
    assert org_ids == {str(scenario.org_a_id), str(scenario.org_b_id)}


# ---------------------------------------------------------------------
# Seleção de organização
# ---------------------------------------------------------------------


def test_select_organization_emite_sessao_completa(client, scenario):
    body = _login(client, scenario.multi_org_email, scenario.password)
    resp = client.post(
        "/api/v1/auth/select-organization",
        json={"org_selection_token": body["org_selection_token"], "organization_id": str(scenario.org_a_id)},
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    assert tokens["organization_id"] == str(scenario.org_a_id)
    assert "refresh_token" not in tokens

    me = client.get("/api/v1/auth/me", headers=_auth_headers(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["organization"]["id"] == str(scenario.org_a_id)


def test_select_organization_cross_tenant_negado(client, scenario):
    """Usuário single-org tentando selecionar uma organização onde ele
    NÃO tem membership — mesmo com um org_selection_token de verdade."""
    from nexasalon_api.core.security import create_org_selection_token

    forged = create_org_selection_token(user_id=uuid.UUID(str(scenario.single_org_user_id)))
    resp = client.post(
        "/api/v1/auth/select-organization",
        json={"org_selection_token": forged, "organization_id": str(scenario.org_b_id)},
    )
    assert resp.status_code == 403


def test_select_organization_token_invalido(client, scenario):
    resp = client.post(
        "/api/v1/auth/select-organization",
        json={"org_selection_token": "lixo-nao-e-jwt", "organization_id": str(scenario.org_a_id)},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------
# Token inválido / expirado / tipo errado
# ---------------------------------------------------------------------


def test_me_sem_token(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_me_com_token_invalido(client):
    resp = client.get("/api/v1/auth/me", headers=_auth_headers("token.completamente.invalido"))
    assert resp.status_code == 401


def test_me_com_token_expirado(client, scenario):
    now_body = _login(client, scenario.single_org_email, scenario.password)
    import time

    payload = jwt.decode(now_body["tokens"]["access_token"], options={"verify_signature": False})
    payload["iat"] = int(time.time()) - 4000
    payload["exp"] = int(time.time()) - 3600
    expired = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

    resp = client.get("/api/v1/auth/me", headers=_auth_headers(expired))
    assert resp.status_code == 401


def test_me_com_token_de_tipo_errado(client, scenario):
    """org_selection_token não pode ser usado como access_token."""
    body = _login(client, scenario.multi_org_email, scenario.password)
    resp = client.get("/api/v1/auth/me", headers=_auth_headers(body["org_selection_token"]))
    assert resp.status_code == 401


# ---------------------------------------------------------------------
# Refresh token: cookie HttpOnly, nunca no corpo JSON
# ---------------------------------------------------------------------


def test_refresh_emite_novo_par_de_tokens_via_cookie(client, scenario):
    body = _login(client, scenario.single_org_email, scenario.password)
    old_cookie = client.cookies.get(settings.refresh_cookie_name)

    resp = client.post("/api/v1/auth/refresh", headers=_csrf_headers())
    assert resp.status_code == 200, resp.text
    new_tokens = resp.json()
    assert new_tokens["access_token"] != body["tokens"]["access_token"]
    assert "refresh_token" not in new_tokens
    assert client.cookies.get(settings.refresh_cookie_name) != old_cookie


def test_refresh_sem_cookie(client):
    resp = client.post("/api/v1/auth/refresh", headers=_csrf_headers())
    assert resp.status_code == 401


def test_refresh_reuso_de_token_ja_rotacionado_revoga_tudo(client, scenario):
    _login(client, scenario.single_org_email, scenario.password)

    first = client.post("/api/v1/auth/refresh", headers=_csrf_headers())
    assert first.status_code == 200
    cookie_after_first_refresh = client.cookies.get(settings.refresh_cookie_name)

    # Simula reapresentar um refresh token JÁ rotacionado: chama o
    # service diretamente com o valor bruto do cookie ANTES da primeira
    # rotação (o TestClient só guarda o cookie ATUAL, não o histórico —
    # por isso pegamos o valor antigo via um segundo client isolado que
    # nunca chamou /refresh).
    from nexasalon_api.services import auth as auth_service

    # refaz o login noutro client só pra capturar o refresh_token bruto
    # (setado como cookie) antes de qualquer rotação
    client2 = TestClient(app)
    body2 = client2.post(
        "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": scenario.password}
    ).json()
    old_raw_refresh = client2.cookies.get(settings.refresh_cookie_name)
    client2.post("/api/v1/auth/refresh", headers=_csrf_headers())  # rotaciona -> old_raw_refresh fica revogado

    import pytest as _pytest
    from nexasalon_api.core.exceptions import UnauthorizedError

    with _pytest.raises(UnauthorizedError):
        auth_service.refresh(old_raw_refresh)

    new_raw_refresh = client2.cookies.get(settings.refresh_cookie_name)
    # o token novo (emitido pela rotação) também foi revogado em cascata
    with _pytest.raises(UnauthorizedError):
        auth_service.refresh(new_raw_refresh)


def test_refresh_token_invalido_via_service(scenario):
    """Cobre o caso de valor de cookie adulterado/nunca existiu — via
    chamada direta ao service, já que o TestClient não permite setar um
    cookie arbitrário facilmente sem passar por /login primeiro."""
    from nexasalon_api.core.exceptions import UnauthorizedError
    from nexasalon_api.services import auth as auth_service

    with pytest.raises(UnauthorizedError):
        auth_service.refresh("nunca-existiu")


# ---------------------------------------------------------------------
# Logout / revogação
# ---------------------------------------------------------------------


def test_logout_revoga_o_refresh_token(client, scenario):
    _login(client, scenario.single_org_email, scenario.password)

    logout_resp = client.post("/api/v1/auth/logout", headers=_csrf_headers())
    assert logout_resp.status_code == 204
    assert settings.refresh_cookie_name not in client.cookies, "cookie deveria ser limpo no logout"

    refresh_resp = client.post("/api/v1/auth/refresh", headers=_csrf_headers())
    assert refresh_resp.status_code == 401


def test_logout_sem_cookie_e_idempotente(client):
    resp = client.post("/api/v1/auth/logout", headers=_csrf_headers())
    assert resp.status_code == 204


# ---------------------------------------------------------------------
# CSRF: refresh/logout exigem o header customizado (proteção contra
# form-based CSRF, já que o cookie é enviado automaticamente pelo browser)
# ---------------------------------------------------------------------


def test_refresh_sem_header_csrf_e_negado(client, scenario):
    _login(client, scenario.single_org_email, scenario.password)
    resp = client.post("/api/v1/auth/refresh")  # sem o header
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_logout_sem_header_csrf_e_negado(client, scenario):
    _login(client, scenario.single_org_email, scenario.password)
    resp = client.post("/api/v1/auth/logout")  # sem o header
    assert resp.status_code == 403


def test_login_nao_exige_header_csrf(client, scenario):
    """Login não depende de nenhum cookie ambiente — o próprio corpo já
    prova posse das credenciais, não precisa de defesa CSRF."""
    resp = client.post(
        "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": scenario.password}
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------
# Membership inativa corta acesso imediatamente
# ---------------------------------------------------------------------


def test_membership_desativada_corta_acesso_imediatamente(client, scenario):
    """Sessão válida, token de acesso ainda dentro do prazo — mas a
    membership foi desativada no meio do caminho. O PRÓXIMO request
    autenticado (aqui, /auth/me) deve negar na hora, sem esperar o token
    expirar."""
    body = _login(client, scenario.recep_email, scenario.password)
    access_token = body["tokens"]["access_token"]

    me_before = client.get("/api/v1/auth/me", headers=_auth_headers(access_token))
    assert me_before.status_code == 200

    session = SessionLocal()
    session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(scenario.org_a_id)}
    )
    membership = session.get(OrganizationMembership, scenario.recep_membership_id)
    membership.status = MembershipStatus.SUSPENDED
    session.commit()
    session.close()

    me_after = client.get("/api/v1/auth/me", headers=_auth_headers(access_token))
    assert me_after.status_code == 403

    refresh_after = client.post("/api/v1/auth/refresh", headers=_csrf_headers())
    assert refresh_after.status_code == 403


# ---------------------------------------------------------------------
# Permissions por role / overrides / view_own x view_all
# ---------------------------------------------------------------------


def test_owner_continua_com_todas_as_permissoes_do_catalogo(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    owner_me = client.get("/api/v1/auth/me", headers=_auth_headers(owner_body["tokens"]["access_token"]))
    session = SessionLocal()
    all_keys = set(session.scalars(text("SELECT key FROM permissions")).all())
    session.close()
    assert set(owner_me.json()["permissions"]) == all_keys


def test_permissions_efetivas_por_role(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    owner_me = client.get("/api/v1/auth/me", headers=_auth_headers(owner_body["tokens"]["access_token"]))
    assert set(owner_me.json()["permissions"]) == {
        "organization.manage", "users.manage", "branches.view", "branches.manage",
        "professionals.view", "professionals.manage", "services.view", "services.manage",
        "clients.view", "clients.manage", "agenda.view_own", "agenda.view_all", "agenda.create",
        "agenda.edit", "agenda.cancel", "agenda.force_overlap", "agenda.manage_blocks",
        "finance.view", "finance.manage", "reports.view", "settings.manage",
        # Comanda/Pagamento (migration 0013) — OWNER recebe as 4.
        "orders.view", "orders.manage", "orders.edit_price", "payments.register",
        # Estoque (migration 0020) — OWNER recebe as 3.
        "inventory.view", "inventory.view_cost", "inventory.manage",
    }

    recep_body = _login(client, scenario.recep_email, scenario.password)
    recep_me = client.get("/api/v1/auth/me", headers=_auth_headers(recep_body["tokens"]["access_token"]))
    assert set(recep_me.json()["permissions"]) == {
        "clients.view", "clients.manage", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel",
        "professionals.view", "services.view", "agenda.manage_blocks",
        # Comanda/Pagamento (migration 0013) — RECEPTIONIST fecha o
        # caixa/comanda no balcão, mas não edita preço (orders.edit_price
        # fica só com OWNER/ADMIN, ver docstring da migration 0013).
        "orders.view", "orders.manage", "payments.register",
        # Caixa Diário (migration 0014) — RECEPTIONIST ganha finance.view
        # pra poder selecionar um caixa aberto ao registrar pagamento;
        # finance.manage (abrir/fechar caixa, sangria, suprimento)
        # continua só com OWNER/ADMIN.
        "finance.view",
        # Estoque (migration 0020) — RECEPTIONIST só vê (precisa saber o
        # que tem em estoque pra vender/avisar o cliente), nunca gerencia
        # nem vê custo (ver docstring da migration 0020).
        "inventory.view",
    }

    prof_body = _login(client, scenario.prof_email, scenario.password)
    prof_me = client.get("/api/v1/auth/me", headers=_auth_headers(prof_body["tokens"]["access_token"]))
    assert set(prof_me.json()["permissions"]) == {
        "agenda.view_own", "agenda.edit", "clients.view", "professionals.view", "services.view",
    }


def test_professional_nao_tem_permissoes_de_financeiro_ou_configuracoes(client, scenario):
    prof_body = _login(client, scenario.prof_email, scenario.password)
    perms = set(
        client.get("/api/v1/auth/me", headers=_auth_headers(prof_body["tokens"]["access_token"])).json()[
            "permissions"
        ]
    )
    assert perms.isdisjoint({"finance.view", "finance.manage", "settings.manage", "organization.manage"})


def test_view_own_x_view_all_preparado_corretamente(client, scenario):
    """RECEPTIONIST enxerga a agenda de todo mundo (view_all);
    PROFESSIONAL só a própria (view_own) — nunca os dois ao mesmo tempo
    pro profissional comum, e nunca nenhum dos dois faltando pra quem
    deveria ter."""
    recep_body = _login(client, scenario.recep_email, scenario.password)
    recep_perms = set(
        client.get("/api/v1/auth/me", headers=_auth_headers(recep_body["tokens"]["access_token"])).json()[
            "permissions"
        ]
    )
    assert "agenda.view_all" in recep_perms
    assert "agenda.view_own" not in recep_perms

    prof_body = _login(client, scenario.prof_email, scenario.password)
    prof_perms = set(
        client.get("/api/v1/auth/me", headers=_auth_headers(prof_body["tokens"]["access_token"])).json()[
            "permissions"
        ]
    )
    assert "agenda.view_own" in prof_perms
    assert "agenda.view_all" not in prof_perms


def test_override_grant_adiciona_permissao_fora_do_role(client, scenario):
    session = SessionLocal()
    session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(scenario.org_a_id)}
    )
    session.add(
        MembershipPermissionOverride(
            membership_id=scenario.prof_membership_id,
            permission_key="agenda.view_all",
            effect=PermissionEffect.GRANT,
        )
    )
    session.commit()
    session.close()

    body = _login(client, scenario.prof_email, scenario.password)
    me = client.get("/api/v1/auth/me", headers=_auth_headers(body["tokens"]["access_token"]))
    assert "agenda.view_all" in me.json()["permissions"]


def test_override_deny_vence_a_permissao_concedida_pelo_role(client, scenario):
    """DENY de override precisa vencer o que o role concederia — mesmo
    o RECEPTIONIST tendo `clients.manage` pelo role, um DENY explícito
    nesta membership deve remover só essa permissão, sem afetar o resto."""
    session = SessionLocal()
    session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(scenario.org_a_id)}
    )
    session.add(
        MembershipPermissionOverride(
            membership_id=scenario.recep_membership_id,
            permission_key="clients.manage",
            effect=PermissionEffect.DENY,
        )
    )
    session.commit()
    session.close()

    body = _login(client, scenario.recep_email, scenario.password)
    me = client.get("/api/v1/auth/me", headers=_auth_headers(body["tokens"]["access_token"]))
    perms = me.json()["permissions"]
    assert "clients.manage" not in perms  # DENY venceu
    assert "clients.view" in perms  # resto do role continua intacto

    # e a rota de verdade respeita isso: RECEPTIONIST normalmente pode
    # criar cliente, mas com o DENY não pode mais.
    resp = client.post(
        "/api/v1/clients", json={"name": "Cliente Teste"}, headers=_auth_headers(body["tokens"]["access_token"])
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# RBAC nas rotas administrativas (/api/v1/users) — backend como
# autoridade final, não o frontend escondendo um botão.
# ---------------------------------------------------------------------


def test_receptionist_nao_gerencia_usuarios_sem_permission(client, scenario):
    recep_body = _login(client, scenario.recep_email, scenario.password)
    resp = client.get("/api/v1/users", headers=_auth_headers(recep_body["tokens"]["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_rota_administrativa_exige_permission_users_manage(client, scenario):
    prof_body = _login(client, scenario.prof_email, scenario.password)
    resp = client.get("/api/v1/users", headers=_auth_headers(prof_body["tokens"]["access_token"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


def test_rota_administrativa_permite_com_permission(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    resp = client.get("/api/v1/users", headers=_auth_headers(owner_body["tokens"]["access_token"]))
    assert resp.status_code == 200


# ---------------------------------------------------------------------
# RBAC nas rotas herdadas da Etapa 2C (branches/professionals/services/
# clients) — antes desta revisão, só exigiam autenticação; agora exigem
# a permission certa (view para leitura, manage para escrita).
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_status_for_prof",
    [
        ("/api/v1/professionals", 200),  # PROFESSIONAL tem professionals.view
        ("/api/v1/services", 200),  # PROFESSIONAL tem services.view
        ("/api/v1/clients", 200),  # PROFESSIONAL tem clients.view
        ("/api/v1/branches", 403),  # PROFESSIONAL NÃO tem branches.view
    ],
)
def test_leitura_dos_recursos_2c_respeita_permission_view(client, scenario, path, expected_status_for_prof):
    body = _login(client, scenario.prof_email, scenario.password)
    resp = client.get(path, headers=_auth_headers(body["tokens"]["access_token"]))
    assert resp.status_code == expected_status_for_prof


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/v1/professionals", {"name": "X"}),
        ("/api/v1/services", {"name": "X", "default_duration_minutes": 30, "default_price": "10.00"}),
        ("/api/v1/clients", {"name": "X"}),
        ("/api/v1/branches", {"name": "X", "slug": "x"}),
    ],
)
def test_escrita_dos_recursos_2c_exige_permission_manage(client, scenario, path, payload):
    """PROFESSIONAL não tem NENHUMA permission `.manage` — todas as
    rotas de escrita (mesmo as que ele consegue LER) devem barrar."""
    body = _login(client, scenario.prof_email, scenario.password)
    resp = client.post(path, json=payload, headers=_auth_headers(body["tokens"]["access_token"]))
    assert resp.status_code == 403, resp.text


def test_owner_gerencia_todos_os_recursos_2c(client, scenario):
    body = _login(client, scenario.single_org_email, scenario.password)
    headers = _auth_headers(body["tokens"]["access_token"])

    assert client.post("/api/v1/branches", json={"name": "Matriz", "slug": "matriz"}, headers=headers).status_code == 201
    assert client.post("/api/v1/professionals", json={"name": "Profissional"}, headers=headers).status_code == 201
    assert client.post(
        "/api/v1/services",
        json={"name": "Corte", "default_duration_minutes": 30, "default_price": "50.00"},
        headers=headers,
    ).status_code == 201
    assert client.post("/api/v1/clients", json={"name": "Cliente"}, headers=headers).status_code == 201


def test_receptionist_ve_mas_nao_gerencia_profissionais_e_servicos(client, scenario):
    body = _login(client, scenario.recep_email, scenario.password)
    headers = _auth_headers(body["tokens"]["access_token"])

    assert client.get("/api/v1/professionals", headers=headers).status_code == 200
    assert client.get("/api/v1/services", headers=headers).status_code == 200
    assert client.post("/api/v1/professionals", json={"name": "X"}, headers=headers).status_code == 403
    assert client.post(
        "/api/v1/services",
        json={"name": "X", "default_duration_minutes": 30, "default_price": "10.00"},
        headers=headers,
    ).status_code == 403


def test_organization_acessivel_a_qualquer_autenticado_sem_permission_dedicada(client, scenario):
    """Decisão documentada em `api/v1/organizations.py`: dado básico da
    própria org, não precisa de permission específica."""
    for email in (scenario.prof_email, scenario.recep_email, scenario.single_org_email):
        body = _login(client, email, scenario.password)
        resp = client.get("/api/v1/organization", headers=_auth_headers(body["tokens"]["access_token"]))
        assert resp.status_code == 200


# ---------------------------------------------------------------------
# Fluxo de convite: administrador nunca define/vê a senha do funcionário
# ---------------------------------------------------------------------


def test_convite_cria_membership_invited_com_invite_token(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    headers = _auth_headers(owner_body["tokens"]["access_token"])

    resp = client.post(
        "/api/v1/users",
        json={"email": "nova@example.com", "name": "Nova", "role_id": str(scenario.recep_role)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["membership"]["status"] == "invited"
    assert body["invite_token"]


def test_usuario_convidado_nao_consegue_logar_antes_de_aceitar(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    client.post(
        "/api/v1/users",
        json={"email": "pendente@example.com", "name": "Pendente", "role_id": str(scenario.recep_role)},
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    resp = client.post("/api/v1/auth/login", json={"email": "pendente@example.com", "password": "qualquer"})
    assert resp.status_code == 401


def test_aceitar_convite_ativa_membership_e_ja_loga(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    invite_resp = client.post(
        "/api/v1/users",
        json={"email": "ianka@example.com", "name": "Ianka", "role_id": str(scenario.recep_role)},
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    invite_token = invite_resp.json()["invite_token"]

    accept_resp = client.post(
        "/api/v1/auth/accept-invite", json={"invite_token": invite_token, "password": "SenhaDaIanka1!"}
    )
    assert accept_resp.status_code == 200, accept_resp.text
    assert accept_resp.json()["access_token"]

    # já consegue logar normalmente com a senha que ELA definiu
    login_resp = client.post(
        "/api/v1/auth/login", json={"email": "ianka@example.com", "password": "SenhaDaIanka1!"}
    )
    assert login_resp.status_code == 200


def test_aceitar_o_mesmo_convite_duas_vezes_falha_na_segunda(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    invite_resp = client.post(
        "/api/v1/users",
        json={"email": "unico@example.com", "name": "Unico", "role_id": str(scenario.recep_role)},
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    invite_token = invite_resp.json()["invite_token"]

    first = client.post(
        "/api/v1/auth/accept-invite", json={"invite_token": invite_token, "password": "Senha1234!"}
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/auth/accept-invite", json={"invite_token": invite_token, "password": "OutraSenha!"}
    )
    assert second.status_code == 403


def test_aceitar_convite_com_token_invalido(client):
    resp = client.post(
        "/api/v1/auth/accept-invite", json={"invite_token": "token-invalido", "password": "Senha1234!"}
    )
    assert resp.status_code == 401


def test_resend_invite_gera_novo_token_e_falha_se_ja_ativo(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    headers = _auth_headers(owner_body["tokens"]["access_token"])

    invite_resp = client.post(
        "/api/v1/users",
        json={"email": "marco@example.com", "name": "Marco", "role_id": str(scenario.recep_role)},
        headers=headers,
    )
    membership_id = invite_resp.json()["membership"]["id"]
    first_token = invite_resp.json()["invite_token"]

    resend_resp = client.post(f"/api/v1/users/{membership_id}/resend-invite", headers=headers)
    assert resend_resp.status_code == 200
    assert resend_resp.json()["invite_token"] != first_token

    # aceita e tenta reenviar de novo -> conflito (não está mais pendente)
    client.post(
        "/api/v1/auth/accept-invite",
        json={"invite_token": resend_resp.json()["invite_token"], "password": "Senha1234!"},
    )
    conflict_resp = client.post(f"/api/v1/users/{membership_id}/resend-invite", headers=headers)
    assert conflict_resp.status_code == 409


# ---------------------------------------------------------------------
# Rate limiting (endpoints sensíveis)
# ---------------------------------------------------------------------


def test_rate_limit_login_bloqueia_apos_o_limite(client, scenario, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_login_max_attempts", 3)
    monkeypatch.setattr(settings, "rate_limit_login_window_seconds", 60)
    rate_limiter.reset()

    for _ in range(3):
        resp = client.post(
            "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": "errada"}
        )
        assert resp.status_code == 401

    blocked = client.post(
        "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": scenario.password}
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "rate_limited"


def test_rate_limit_e_por_ip_nao_por_credencial(client, monkeypatch):
    """Confirma que o limite bloqueia novas tentativas mesmo trocando de
    e-mail — é o IP de origem que conta, não a conta sendo atacada."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_login_max_attempts", 2)
    monkeypatch.setattr(settings, "rate_limit_login_window_seconds", 60)
    rate_limiter.reset()

    client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "x"})
    client.post("/api/v1/auth/login", json={"email": "b@example.com", "password": "x"})
    resp = client.post("/api/v1/auth/login", json={"email": "c@example.com", "password": "x"})
    assert resp.status_code == 429


def test_rate_limit_desligado_por_configuracao_nao_bloqueia(client, scenario, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    rate_limiter.reset()
    for _ in range(10):
        resp = client.post(
            "/api/v1/auth/login", json={"email": scenario.single_org_email, "password": "errada"}
        )
        assert resp.status_code == 401  # nunca 429


def test_producao_recusa_subir_com_rate_limit_desligado():
    from nexasalon_api.core.config import Settings

    with pytest.raises(Exception):
        Settings(
            environment="production",
            rate_limit_enabled=False,
            jwt_secret="x" * 40,
            dev_auth_enabled=False,
            refresh_cookie_secure=True,
        )


# ---------------------------------------------------------------------
# Cross-tenant: usuário de uma org nunca lê/edita dados da outra,
# mesmo manipulando IDs manualmente.
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Redefinição de senha por administrador (Etapa A) — mesma garantia do
# convite: admin nunca vê/define a senha do funcionário, só relaya um
# link/token de uso único.
# ---------------------------------------------------------------------


def test_admin_reset_password_gera_link_e_membership_continua_active(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    headers = _auth_headers(owner_body["tokens"]["access_token"])

    resp = client.post(f"/api/v1/users/{scenario.recep_membership_id}/reset-password", headers=headers)
    assert resp.status_code == 200, resp.text
    reset_token = resp.json()["reset_token"]
    assert reset_token

    # a senha ANTIGA continua funcionando até o link ser de fato usado
    old_login = client.post(
        "/api/v1/auth/login", json={"email": scenario.recep_email, "password": scenario.password}
    )
    assert old_login.status_code == 200


def test_consumir_reset_password_troca_a_senha_e_ja_loga(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    reset_resp = client.post(
        f"/api/v1/users/{scenario.recep_membership_id}/reset-password",
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    reset_token = reset_resp.json()["reset_token"]

    consume_resp = client.post(
        "/api/v1/auth/reset-password", json={"reset_token": reset_token, "password": "NovaSenha123!"}
    )
    assert consume_resp.status_code == 200, consume_resp.text
    assert consume_resp.json()["access_token"]

    # senha antiga não funciona mais; a nova sim
    old_login = client.post(
        "/api/v1/auth/login", json={"email": scenario.recep_email, "password": scenario.password}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login", json={"email": scenario.recep_email, "password": "NovaSenha123!"}
    )
    assert new_login.status_code == 200


def test_reset_password_de_membership_suspensa_troca_senha_mas_recusa_sessao(client, scenario):
    """A senha é redefinida com sucesso mesmo assim (não é o convite —
    já existe conta), mas a resposta recusa devolver uma sessão logada
    porque a membership está SUSPENDED; login subsequente também
    continua bloqueado até reativação."""
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    reset_resp = client.post(
        f"/api/v1/users/{scenario.inactive_membership_id}/reset-password",
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    reset_token = reset_resp.json()["reset_token"]

    consume_resp = client.post(
        "/api/v1/auth/reset-password", json={"reset_token": reset_token, "password": "NovaSenha123!"}
    )
    assert consume_resp.status_code == 403

    login_resp = client.post(
        "/api/v1/auth/login", json={"email": scenario.inactive_email, "password": "NovaSenha123!"}
    )
    assert login_resp.status_code == 403


def test_reset_password_token_invalido(client):
    resp = client.post(
        "/api/v1/auth/reset-password", json={"reset_token": "token-invalido", "password": "Senha1234!"}
    )
    assert resp.status_code == 401


def test_receptionist_nao_pode_gerar_reset_password_de_outra_membership(client, scenario):
    recep_body = _login(client, scenario.recep_email, scenario.password)
    resp = client.post(
        f"/api/v1/users/{scenario.single_membership_id}/reset-password",
        headers=_auth_headers(recep_body["tokens"]["access_token"]),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# Catálogo de roles/permissions + overrides por membership (perfil
# "Personalizado") — Etapa A.
# ---------------------------------------------------------------------


def test_list_roles_retorna_os_4_roles_de_sistema(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    resp = client.get("/api/v1/roles", headers=_auth_headers(owner_body["tokens"]["access_token"]))
    assert resp.status_code == 200, resp.text
    names = {r["name"] for r in resp.json()}
    assert {"OWNER", "ADMIN", "RECEPTIONIST", "PROFESSIONAL"} <= names
    assert all(r["is_system"] for r in resp.json() if r["name"] in {"OWNER", "ADMIN", "RECEPTIONIST", "PROFESSIONAL"})


def test_list_permissions_retorna_catalogo_com_modulo(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    resp = client.get("/api/v1/permissions", headers=_auth_headers(owner_body["tokens"]["access_token"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert any(p["key"] == "agenda.edit" for p in body)
    assert all("module" in p for p in body)


def test_permission_overrides_get_put_roundtrip(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    headers = _auth_headers(owner_body["tokens"]["access_token"])

    empty = client.get(f"/api/v1/users/{scenario.prof_membership_id}/permission-overrides", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == []

    put_resp = client.put(
        f"/api/v1/users/{scenario.prof_membership_id}/permission-overrides",
        json={"overrides": [{"permission_key": "agenda.view_all", "effect": "grant"}]},
        headers=headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json() == [{"permission_key": "agenda.view_all", "effect": "grant"}]

    get_resp = client.get(f"/api/v1/users/{scenario.prof_membership_id}/permission-overrides", headers=headers)
    assert get_resp.json() == [{"permission_key": "agenda.view_all", "effect": "grant"}]

    # e o efeito realmente se reflete no /me da própria pessoa
    prof_body = _login(client, scenario.prof_email, scenario.password)
    prof_me = client.get("/api/v1/auth/me", headers=_auth_headers(prof_body["tokens"]["access_token"]))
    assert "agenda.view_all" in prof_me.json()["permissions"]


def test_permission_overrides_com_chave_desconhecida_e_rejeitado(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    resp = client.put(
        f"/api/v1/users/{scenario.prof_membership_id}/permission-overrides",
        json={"overrides": [{"permission_key": "isso.nao.existe", "effect": "grant"}]},
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    assert resp.status_code == 422


def test_agenda_access_default_e_all_all_sem_grants(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    resp = client.get(
        f"/api/v1/users/{scenario.prof_membership_id}/agenda-access",
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["view_scope"] == "all"
    assert body["edit_scope"] == "all"
    assert body["grants"] == []


def test_agenda_access_edit_scope_all_sem_view_scope_all_e_rejeitado(client, scenario):
    owner_body = _login(client, scenario.single_org_email, scenario.password)
    resp = client.put(
        f"/api/v1/users/{scenario.prof_membership_id}/agenda-access",
        json={
            "view_scope": "selected", "edit_scope": "all",
            "viewable_professional_ids": [], "editable_professional_ids": [],
        },
        headers=_auth_headers(owner_body["tokens"]["access_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_receptionist_nao_gerencia_agenda_access_de_outra_membership(client, scenario):
    recep_body = _login(client, scenario.recep_email, scenario.password)
    resp = client.get(
        f"/api/v1/users/{scenario.prof_membership_id}/agenda-access",
        headers=_auth_headers(recep_body["tokens"]["access_token"]),
    )
    assert resp.status_code == 403


def test_cross_tenant_nao_acessa_membership_de_outra_organizacao(client, scenario):
    """multi_org_user tem membership ACTIVE em A (ADMIN) e em B
    (RECEPTIONIST) — usando o token da sessão em B, tentar manipular a
    membership de A (que ele nem deveria enxergar) via ID cru."""
    login_body = _login(client, scenario.multi_org_email, scenario.password)
    select_resp = client.post(
        "/api/v1/auth/select-organization",
        json={
            "org_selection_token": login_body["org_selection_token"],
            "organization_id": str(scenario.org_b_id),
        },
    )
    assert select_resp.status_code == 200
    token_b = select_resp.json()["access_token"]

    # multi_org_user em B é RECEPTIONIST (sem users.manage) -> 403, não 404;
    # mas o ponto central é que ele NUNCA vê dado de A por esse token.
    resp = client.get("/api/v1/users", headers=_auth_headers(token_b))
    assert resp.status_code == 403

    me_resp = client.get("/api/v1/auth/me", headers=_auth_headers(token_b))
    assert me_resp.json()["organization"]["id"] == str(scenario.org_b_id)
    assert me_resp.json()["organization"]["id"] != str(scenario.org_a_id)
