"""
Setup de teste: Postgres descartável (pgserver, embutido) + migrations
reais + role `nexasalon_app` restrito (mesmo papel que a API usa em
produção — os testes de RLS não valem nada se rodarem como superuser).

Tudo isso acontece em nível de MÓDULO, antes de qualquer import de
`nexasalon_api`, porque `Settings` lê variável de ambiente na primeira
instanciação (`core/config.py`) e `core/db.py` cria a engine a partir
dela — não dá pra trocar depois que o app já foi importado.
"""
import os
import subprocess
import uuid
from pathlib import Path

import psycopg
import pgserver

REPO_ROOT = Path(__file__).resolve().parent.parent
_PGDATA = str(REPO_ROOT / ".pgdata_test")

os.environ.pop("NEXASALON_DATABASE_URL", None)

_srv = pgserver.get_server(_PGDATA)
try:
    _srv.psql("CREATE DATABASE nexasalon_test;")
except Exception:
    pass  # já existe de uma execução anterior que não limpou

_ADMIN_URL = f"postgresql+psycopg://postgres:@/nexasalon_test?host={_PGDATA}"

# migrations rodam com o usuário dono do schema (postgres), nunca com o
# role restrito que a API usa — mesma separação de papéis do README.
_migration_env = os.environ.copy()
_migration_env["NEXASALON_DATABASE_URL"] = _ADMIN_URL
_result = subprocess.run(
    ["alembic", "upgrade", "head"],
    cwd=str(REPO_ROOT),
    env=_migration_env,
    capture_output=True,
    text=True,
)
if _result.returncode != 0:
    raise RuntimeError(f"Falha ao rodar migrations no banco de teste:\n{_result.stdout}\n{_result.stderr}")

with psycopg.connect(f"host={_PGDATA} dbname=nexasalon_test user=postgres", autocommit=True) as _conn:
    with _conn.cursor() as _cur:
        # idempotente: se o role já existir de uma execução anterior que
        # não limpou o .pgdata_test, revoga antes de tentar recriar.
        _cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nexasalon_app'")
        if _cur.fetchone() is not None:
            _cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM nexasalon_app")
            _cur.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM nexasalon_app")
            _cur.execute("DROP ROLE nexasalon_app")
        _cur.execute("CREATE ROLE nexasalon_app LOGIN PASSWORD 'test' NOSUPERUSER NOBYPASSRLS")
        _cur.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO nexasalon_app")
        _cur.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO nexasalon_app")

os.environ["NEXASALON_DATABASE_URL"] = (
    f"postgresql+psycopg://nexasalon_app:test@/nexasalon_test?host={_PGDATA}"
)
os.environ["NEXASALON_ENVIRONMENT"] = "test"
os.environ["NEXASALON_DEV_AUTH_ENABLED"] = "true"
# TestClient fala com o app via http (não https) — o atributo Secure do
# cookie de refresh (core/config.py) faria o httpx simplesmente descartar
# o cookie de volta, quebrando qualquer teste que dependa dele. Só testes
# usam isto; o guard de produção em Settings recusa este valor fora daqui.
os.environ["NEXASALON_REFRESH_COOKIE_SECURE"] = "false"

# só agora é seguro importar qualquer coisa de nexasalon_api.
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from nexasalon_api.api.deps import get_current_actor  # noqa: E402
from nexasalon_api.core.db import SessionLocal  # noqa: E402
from nexasalon_api.core.dev_auth import ActorContext, DEV_ORGANIZATION_ID  # noqa: E402
from nexasalon_api.main import app  # noqa: E402
from nexasalon_api.models.enums import MembershipStatus  # noqa: E402
from nexasalon_api.models.identity import OrganizationMembership, User  # noqa: E402
from nexasalon_api.models.organization import Organization  # noqa: E402
from nexasalon_api.models.rbac import Role, RolePermission  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    _srv.cleanup()


def seed_organization(name: str, slug: str) -> ActorContext:
    """Cria uma organização de teste completa (org + role + user +
    membership) e devolve o ActorContext correspondente — usado pra
    simular uma segunda empresa nos testes de isolamento multi-tenant.

    O role "Owner" fabricado aqui recebe TODAS as permissions do catálogo
    (mesmo truque do "Dev Owner" em `core/dev_auth.py`) — desde que as
    rotas da Etapa 2C passaram a exigir `require_permission` (Etapa 2D),
    um `ActorContext` sem permissões apanharia 403 em tudo. Estes testes
    continuam validando regra de negócio/isolamento multi-tenant, não
    RBAC — RBAC tem cobertura própria em `test_auth.py`."""
    org_id, user_id, role_id, membership_id = (uuid.uuid4() for _ in range(4))
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, true)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name=name, slug=slug))
        session.flush()
        session.add(User(id=user_id, email=f"{slug}@nexasalon.local", name=f"Usuário {name}"))
        session.flush()
        session.add(Role(id=role_id, organization_id=org_id, name="Owner", is_system=False))
        session.flush()
        all_keys = list(session.scalars(text("SELECT key FROM permissions")).all())
        for key in all_keys:
            session.add(RolePermission(role_id=role_id, permission_key=key))
        session.flush()
        session.add(
            OrganizationMembership(
                id=membership_id,
                user_id=user_id,
                organization_id=org_id,
                role_id=role_id,
                status=MembershipStatus.ACTIVE,
            )
        )
        session.commit()
    return ActorContext(
        organization_id=org_id,
        user_id=user_id,
        membership_id=membership_id,
        role_id=role_id,
        role_name="Owner",
        permissions=frozenset(all_keys),
    )


@pytest.fixture()
def dev_client() -> TestClient:
    """Cliente usando o ator DEV ONLY real (sem override) — só para os
    testes que exercitam a própria dependency de dev (test_dev_auth.py).
    Os demais testes usam `client_as` com organizações isoladas."""
    app.dependency_overrides.pop(get_current_actor, None)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_actor, None)


@pytest.fixture()
def org_a_actor() -> ActorContext:
    return seed_organization("Org A de teste", f"org-a-{uuid.uuid4().hex[:8]}")


@pytest.fixture()
def org_b_actor() -> ActorContext:
    return seed_organization("Org B de teste", f"org-b-{uuid.uuid4().hex[:8]}")


@pytest.fixture()
def client_as():
    """Fábrica: `client_as(actor)` devolve um TestClient autenticado como
    aquele ator (via dependency_override). Cada chamada substitui o
    override no `app` compartilhado — não misture `client_as(a)` e
    `client_as(b)` intercalados sem reatribuir a variável a cada troca."""

    def _make(actor: ActorContext) -> TestClient:
        app.dependency_overrides[get_current_actor] = lambda: actor
        return TestClient(app)

    yield _make
    app.dependency_overrides.pop(get_current_actor, None)
