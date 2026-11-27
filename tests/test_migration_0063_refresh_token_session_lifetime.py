"""Migração de sessão de refresh token — 0062 -> 0063 (limite absoluto
de 7 dias + expiração por inatividade de 12h).

Mesmo padrão de `test_migration_0014_backfill.py`: Postgres descartável
PRÓPRIO (não o de `conftest.py`, que já sobe migrado até as `heads`
antes de qualquer teste rodar), subido só até `0062` (estado anterior a
esta correção), pra poder testar de verdade:

  1. Backfill: sessões já existentes (ACTIVAS, revogadas, ou já
     expiradas pela regra ANTIGA) recebem `session_started_at`/
     `last_used_at`, mas isso nunca "ressuscita" uma sessão que não
     devia mais funcionar.
  2. Downgrade reverte sem quebrar, e dá pra reaplicar depois.
  3. Compatibilidade com o backend ATUALMENTE PUBLICADO (código antigo,
     que não conhece `session_started_at`/`last_used_at`): a migration
     tem `server_default=now()` nas 2 colunas exatamente para que um
     INSERT feito pelo código antigo (`refresh_tokens` sem essas
     colunas) continue funcionando na janela entre a migration ser
     aplicada e o deploy do código novo — ver regra 4.1 do CLAUDE.md e
     o precedente das migrations 0058/0059.
"""
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pgserver
import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parent.parent
_PGDATA = str(REPO_ROOT / ".pgdata_migration_0063_test")


@pytest.fixture(scope="module")
def pre_0063_db():
    shutil.rmtree(_PGDATA, ignore_errors=True)
    srv = pgserver.get_server(_PGDATA)
    try:
        srv.psql("CREATE DATABASE nexasalon_migration_0063_test;")
    except Exception:
        pass  # já existe de uma execução anterior que não limpou

    admin_url = f"postgresql+psycopg://postgres:@/nexasalon_migration_0063_test?host={_PGDATA}"
    env = os.environ.copy()
    env["NEXASALON_DATABASE_URL"] = admin_url
    env.pop("NEXASALON_MIGRATIONS_DATABASE_URL", None)

    # Estado imediatamente ANTERIOR a esta correção — equivalente ao
    # backend atualmente publicado em produção.
    result = subprocess.run(
        ["alembic", "upgrade", "0062"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar até 0062:\n{result.stdout}\n{result.stderr}"

    yield admin_url, env

    srv.cleanup()
    shutil.rmtree(_PGDATA, ignore_errors=True)


def _insert_legacy_chain(admin_url: str) -> dict:
    """Monta a cadeia mínima de domínio (organização -> role -> usuário
    -> membership) via SQL cru, restrita ao schema da 0062 — nenhuma
    coluna de `refresh_tokens` posterior a esta revisão é usada aqui."""
    engine = create_engine(admin_url)
    with engine.begin() as conn:
        org_id = uuid.uuid4()
        conn.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        conn.execute(
            text(
                "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                "VALUES (:id, 'Org Legado 0063', :slug, now(), now())"
            ),
            {"id": org_id, "slug": f"org-legado-0063-{uuid.uuid4().hex[:8]}"},
        )
        role_id = conn.execute(
            text("SELECT id FROM roles WHERE name = 'OWNER' AND organization_id IS NULL")
        ).scalar_one()
        user_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO users (id, email, name, created_at, updated_at) "
                "VALUES (:id, :email, 'Usuário Legado 0063', now(), now())"
            ),
            {"id": user_id, "email": f"legado-0063-{uuid.uuid4().hex[:8]}@nexasalon.local"},
        )
        membership_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO organization_memberships (id, user_id, organization_id, role_id, status, "
                "created_at, updated_at) VALUES (:id, :uid, :org, :role, 'active', now(), now())"
            ),
            {"id": membership_id, "uid": user_id, "org": org_id, "role": role_id},
        )
    engine.dispose()
    return {"org_id": org_id, "user_id": user_id, "membership_id": membership_id}


def _insert_legacy_refresh_token(admin_url: str, chain: dict, *, revoked: bool, expired: bool) -> uuid.UUID:
    """Insere um refresh_token no formato PRÉ-0063 (sem
    `session_started_at`/`last_used_at`, que ainda não existem nesta
    revisão do schema) — reproduz fielmente como a linha existia em
    produção antes desta migration."""
    engine = create_engine(admin_url)
    token_id = uuid.uuid4()
    with engine.begin() as conn:
        issued_at = datetime.now(timezone.utc) - timedelta(days=40)
        # Regra ANTIGA: expires_at = issued_at + 30 dias. Se `expired`,
        # isso já passou; senão, ainda falta bastante (sessão antiga,
        # mas ainda válida pela regra antiga).
        expires_at = issued_at + timedelta(days=30) if expired else datetime.now(timezone.utc) + timedelta(days=5)
        conn.execute(
            text(
                "INSERT INTO refresh_tokens (id, user_id, organization_id, membership_id, token_hash, "
                "issued_at, expires_at, revoked_at) "
                "VALUES (:id, :uid, :org, :mid, :hash, :issued, :expires, :revoked)"
            ),
            {
                "id": token_id, "uid": chain["user_id"], "org": chain["org_id"], "mid": chain["membership_id"],
                "hash": uuid.uuid4().hex, "issued": issued_at, "expires": expires_at,
                "revoked": datetime.now(timezone.utc) - timedelta(days=1) if revoked else None,
            },
        )
    engine.dispose()
    return token_id


def test_migration_0063_backfill_concede_ciclo_novo_a_sessao_ativa(pre_0063_db):
    admin_url, env = pre_0063_db
    chain = _insert_legacy_chain(admin_url)
    token_id = _insert_legacy_refresh_token(admin_url, chain, revoked=False, expired=False)

    result = subprocess.run(
        ["alembic", "upgrade", "0063"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar para 0063:\n{result.stdout}\n{result.stderr}"

    engine = create_engine(admin_url)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT session_started_at, last_used_at FROM refresh_tokens WHERE id = :id"),
            {"id": token_id},
        ).one()
        now = datetime.now(timezone.utc)
        assert row.session_started_at is not None
        assert row.last_used_at is not None
        assert (now - row.session_started_at) < timedelta(minutes=5), (
            "backfill deveria conceder um ciclo novo a partir do deploy (session_started_at ~ now())"
        )
        assert (now - row.last_used_at) < timedelta(minutes=5)

        for column in ("session_started_at", "last_used_at"):
            is_nullable = conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'refresh_tokens' AND column_name = :col"
                ),
                {"col": column},
            ).scalar_one()
            assert is_nullable == "NO", f"{column} deveria ter virado NOT NULL depois do backfill"

        remaining_null = conn.execute(
            text(
                "SELECT COUNT(*) FROM refresh_tokens "
                "WHERE session_started_at IS NULL OR last_used_at IS NULL"
            )
        ).scalar_one()
        assert remaining_null == 0
    engine.dispose()


def test_migration_0063_backfill_nao_reativa_token_revogado(pre_0063_db):
    """O backfill grava `session_started_at`/`last_used_at` "frescos"
    mesmo em tokens já revogados (não há como saber retroativamente, e
    não faz diferença) — mas isso nunca reativa a sessão:
    `services/auth.py::refresh` sempre checa `revoked_at` ANTES de
    qualquer checagem de inatividade/limite absoluto, então um token
    revogado continua rejeitado independente do que o backfill
    escreveu nessas 2 colunas novas."""
    admin_url, env = pre_0063_db
    chain = _insert_legacy_chain(admin_url)
    token_id = _insert_legacy_refresh_token(admin_url, chain, revoked=True, expired=False)

    result = subprocess.run(
        ["alembic", "upgrade", "0063"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar para 0063:\n{result.stdout}\n{result.stderr}"

    engine = create_engine(admin_url)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT revoked_at, session_started_at FROM refresh_tokens WHERE id = :id"),
            {"id": token_id},
        ).one()
        assert row.revoked_at is not None, "backfill nunca deveria limpar revoked_at"
        assert row.session_started_at is not None
    engine.dispose()


def test_migration_0063_backfill_nao_reativa_token_ja_expirado_pela_regra_antiga(pre_0063_db):
    """Token cujo `expires_at` (regra ANTIGA: issued_at + 30 dias) já
    tinha passado antes da migration continua expirado depois dela — o
    backfill só toca `session_started_at`/`last_used_at`, NUNCA
    `expires_at`; a checagem `expires_at <= now` em `refresh()` continua
    rejeitando esse token normalmente até sua próxima rotação (que
    nunca vai acontecer, porque ele é rejeitado antes de chegar lá)."""
    admin_url, env = pre_0063_db
    chain = _insert_legacy_chain(admin_url)
    token_id = _insert_legacy_refresh_token(admin_url, chain, revoked=False, expired=True)

    result = subprocess.run(
        ["alembic", "upgrade", "0063"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar para 0063:\n{result.stdout}\n{result.stderr}"

    engine = create_engine(admin_url)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT expires_at FROM refresh_tokens WHERE id = :id"),
            {"id": token_id},
        ).one()
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        assert expires_at < datetime.now(timezone.utc), (
            "expires_at (regra antiga) não deveria ser alterado pelo backfill"
        )
    engine.dispose()


def test_migration_0063_compatibilidade_com_backend_atualmente_publicado(pre_0063_db):
    """Simula o código ANTIGO (ainda não sabe que `session_started_at`/
    `last_used_at` existem) inserindo um refresh_token DEPOIS que a
    migration 0063 já rodou — reproduz exatamente a janela entre
    "migration aplicada" e "deploy do código novo" (ordem segura padrão
    pra migration aditiva, regra 4.1 do CLAUDE.md). Sem o
    `server_default=now()` nas 2 colunas, este INSERT quebraria com
    violação de NOT NULL."""
    admin_url, env = pre_0063_db
    chain = _insert_legacy_chain(admin_url)

    result = subprocess.run(
        ["alembic", "upgrade", "0063"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao migrar para 0063:\n{result.stdout}\n{result.stderr}"

    engine = create_engine(admin_url)
    token_id = uuid.uuid4()
    with engine.begin() as conn:
        # INSERT deliberadamente SEM session_started_at/last_used_at —
        # exatamente o que o código antigo (`refresh_token_repo.create`
        # pré-Task B) ainda faria nesta janela.
        conn.execute(
            text(
                "INSERT INTO refresh_tokens (id, user_id, organization_id, membership_id, token_hash, "
                "issued_at, expires_at) "
                "VALUES (:id, :uid, :org, :mid, :hash, now(), now() + interval '30 days')"
            ),
            {
                "id": token_id, "uid": chain["user_id"], "org": chain["org_id"], "mid": chain["membership_id"],
                "hash": uuid.uuid4().hex,
            },
        )

        row = conn.execute(
            text("SELECT session_started_at, last_used_at FROM refresh_tokens WHERE id = :id"),
            {"id": token_id},
        ).one()
        assert row.session_started_at is not None, "server_default deveria ter preenchido session_started_at"
        assert row.last_used_at is not None, "server_default deveria ter preenchido last_used_at"
    engine.dispose()


def test_migration_0063_downgrade_reverte_sem_quebrar(pre_0063_db):
    """Independente dos testes anteriores de propósito (não assume qual
    já rodou nem em que ordem — só garante que está em 0063 antes de
    começar). O que importa validar é que o downgrade em si roda sem
    erro, remove as 2 colunas, e dá pra reaplicar a 0063 depois."""
    admin_url, env = pre_0063_db

    result = subprocess.run(
        ["alembic", "upgrade", "0063"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao garantir 0063 antes do downgrade:\n{result.stdout}\n{result.stderr}"

    result = subprocess.run(
        ["alembic", "downgrade", "0062"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao reverter a 0063:\n{result.stdout}\n{result.stderr}"

    engine = create_engine(admin_url)
    with engine.begin() as conn:
        columns = set(
            conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'refresh_tokens'")
            ).scalars().all()
        )
    engine.dispose()
    assert "session_started_at" not in columns
    assert "last_used_at" not in columns

    result = subprocess.run(
        ["alembic", "upgrade", "0063"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao reaplicar a 0063 depois do downgrade:\n{result.stdout}\n{result.stderr}"
