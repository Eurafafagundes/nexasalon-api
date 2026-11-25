"""Migration 0062 — índice único parcial de ciclos ativos da Central de
Recuperação (`uq_client_recovery_cycles_org_client_active`) passa a
cobrir `scheduled`, além de `pending`/`in_progress` (auditoria pós-V3:
um retorno marcado mas ainda não realizado é tratado como ATIVO na
camada de aplicação desde a rodada anterior, mas o índice do banco
continuava só com o predicado antigo).

Banco descartável PRÓPRIO (mesmo padrão de `test_migration_0049.py`/
`test_migration_0014_backfill.py`) — nunca o banco compartilhado da
suíte principal (`conftest.py`), porque este teste precisa subir/
descer revisões específicas (0061 ↔ 0062) e inserir dados conflitantes
direto via SQL, coisas que deixariam o banco compartilhado parado numa
revisão errada pro resto da sessão se reaproveitado."""
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pgserver
import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_PGDATA = str(REPO_ROOT / ".pgdata_test_0062")
_DB_NAME = "nexasalon_migration_0062_test"


@pytest.fixture(scope="module")
def migration_db():
    shutil.rmtree(_PGDATA, ignore_errors=True)
    srv = pgserver.get_server(_PGDATA)
    try:
        srv.psql(f"CREATE DATABASE {_DB_NAME};")
    except Exception:
        pass

    u = urlsplit(srv.get_uri())
    admin_url = f"postgresql://postgres:@{u.hostname or '127.0.0.1'}:{u.port or 5432}/{_DB_NAME}"
    env = os.environ.copy()
    env["NEXASALON_DATABASE_URL"] = admin_url.replace("postgresql://", "postgresql+psycopg://")
    env.pop("NEXASALON_MIGRATIONS_DATABASE_URL", None)

    # Sobe TODAS as revisões de topo (0060 -> 0061 -> 0062 na branch
    # principal, + a branch local da Comissão Condicional) — mesma
    # lógica de `conftest.py` principal, isolada neste banco descartável.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao subir heads no banco isolado:\n{result.stdout}\n{result.stderr}"

    yield admin_url, env

    srv.cleanup()
    shutil.rmtree(_PGDATA, ignore_errors=True)


def _alembic(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True,
    )


def _index_predicate(conn: psycopg.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_expr(indpred, indrelid) FROM pg_index "
            "JOIN pg_class ON pg_class.oid = pg_index.indexrelid "
            "WHERE pg_class.relname = 'uq_client_recovery_cycles_org_client_active'"
        )
        row = cur.fetchone()
        return row[0] if row else None


def _seed_org_and_client(conn: psycopg.Connection, *, name: str = "Cliente Teste") -> tuple[uuid.UUID, uuid.UUID]:
    org_id, client_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
            (org_id, f"Org {org_id.hex[:8]}", f"org-0062-{org_id.hex[:8]}"),
        )
        cur.execute(
            "INSERT INTO clients (id, organization_id, name) VALUES (%s, %s, %s)",
            (client_id, org_id, name),
        )
    conn.commit()
    return org_id, client_id


def _insert_cycle(conn: psycopg.Connection, org_id: uuid.UUID, client_id: uuid.UUID, status: str) -> uuid.UUID:
    cycle_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO client_recovery_cycles (id, organization_id, client_id, status) VALUES (%s, %s, %s, %s)",
            (cycle_id, org_id, client_id, status),
        )
    conn.commit()
    return cycle_id


def test_upgrade_heads_chega_em_0062_com_indice_ampliado(migration_db):
    admin_url, env = migration_db
    assert "0062" in _alembic(env, "current").stdout + _alembic(env, "current").stderr
    with psycopg.connect(admin_url) as conn:
        predicate = _index_predicate(conn)
        assert predicate is not None
        assert "scheduled" in predicate
        assert "pending" in predicate
        assert "in_progress" in predicate


def test_downgrade_para_0061_restaura_predicado_original(migration_db):
    admin_url, env = migration_db
    result = _alembic(env, "downgrade", "0061")
    assert result.returncode == 0, result.stdout + result.stderr
    with psycopg.connect(admin_url) as conn:
        predicate = _index_predicate(conn)
        assert predicate is not None
        assert "scheduled" not in predicate
        assert "pending" in predicate
        assert "in_progress" in predicate
    # Religa pro resto do módulo continuar em 0062.
    result = _alembic(env, "upgrade", "0062")
    assert result.returncode == 0, result.stdout + result.stderr
    with psycopg.connect(admin_url) as conn:
        assert "scheduled" in _index_predicate(conn)


def test_impossibilidade_scheduled_e_pending_simultaneos(migration_db):
    admin_url, _env = migration_db
    with psycopg.connect(admin_url) as conn:
        org_id, client_id = _seed_org_and_client(conn, name="Scheduled+Pending")
        _insert_cycle(conn, org_id, client_id, "scheduled")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_cycle(conn, org_id, client_id, "pending")
        conn.rollback()


def test_impossibilidade_scheduled_e_in_progress_simultaneos(migration_db):
    admin_url, _env = migration_db
    with psycopg.connect(admin_url) as conn:
        org_id, client_id = _seed_org_and_client(conn, name="Scheduled+InProgress")
        _insert_cycle(conn, org_id, client_id, "scheduled")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_cycle(conn, org_id, client_id, "in_progress")
        conn.rollback()


def test_impossibilidade_dois_scheduled_simultaneos(migration_db):
    admin_url, _env = migration_db
    with psycopg.connect(admin_url) as conn:
        org_id, client_id = _seed_org_and_client(conn, name="Dois Scheduled")
        _insert_cycle(conn, org_id, client_id, "scheduled")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_cycle(conn, org_id, client_id, "scheduled")
        conn.rollback()


def test_multiplos_ciclos_historicos_encerrados_sao_permitidos(migration_db):
    admin_url, _env = migration_db
    with psycopg.connect(admin_url) as conn:
        org_id, client_id = _seed_org_and_client(conn, name="Historico Multiplo")
        _insert_cycle(conn, org_id, client_id, "recovered")
        _insert_cycle(conn, org_id, client_id, "lost")
        _insert_cycle(conn, org_id, client_id, "recovered")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM client_recovery_cycles WHERE organization_id = %s AND client_id = %s",
                (org_id, client_id),
            )
            assert cur.fetchone()[0] == 3  # nenhum dos 3 encerrados colide


def test_novo_ciclo_apos_recuperacao_e_nova_inatividade_e_permitido(migration_db):
    """Ciclo `recovered` (terminal) nunca bloqueia um ciclo ATIVO novo
    pro mesmo cliente — "reset completo após recuperação" depende disso."""
    admin_url, _env = migration_db
    with psycopg.connect(admin_url) as conn:
        org_id, client_id = _seed_org_and_client(conn, name="Recuperado de Novo")
        _insert_cycle(conn, org_id, client_id, "recovered")
        new_cycle_id = _insert_cycle(conn, org_id, client_id, "pending")  # não deve levantar
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM client_recovery_cycles WHERE id = %s", (new_cycle_id,))
            assert cur.fetchone()[0] == "pending"


def test_isolamento_entre_organizacoes_nunca_colide(migration_db):
    """O índice é por (organization_id, client_id) — dois CLIENTES
    DIFERENTES (ainda que em organizações diferentes OU na mesma
    organização) nunca colidem entre si."""
    admin_url, _env = migration_db
    with psycopg.connect(admin_url) as conn:
        org_a, client_a = _seed_org_and_client(conn, name="Cliente Org A")
        org_b, client_b = _seed_org_and_client(conn, name="Cliente Org B")
        _insert_cycle(conn, org_a, client_a, "scheduled")
        _insert_cycle(conn, org_b, client_b, "scheduled")  # cliente DIFERENTE, nunca colide
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM client_recovery_cycles WHERE status = 'scheduled' AND client_id IN (%s, %s)",
                (client_a, client_b),
            )
            assert cur.fetchone()[0] == 2


def test_pre_check_interrompe_upgrade_quando_ja_existe_duplicidade():
    """Simula o cenário que o pré-check da migration precisa pegar:
    sobe só até 0061 (índice ainda sem `scheduled`), insere via SQL um
    cliente com `scheduled` + `pending` simultâneos (permitido pelo
    índice ANTIGO), e confirma que `alembic upgrade 0062` recusa e
    NUNCA altera/apaga os dados. Banco PRÓPRIO e isolado (não usa o
    `migration_db` do módulo, que já está em 0062) — precisa nascer
    parado em 0061."""
    pgdata = str(REPO_ROOT / ".pgdata_test_0062_precheck")
    shutil.rmtree(pgdata, ignore_errors=True)
    srv = pgserver.get_server(pgdata)
    try:
        try:
            srv.psql("CREATE DATABASE nexasalon_migration_0062_precheck;")
        except Exception:
            pass
        u = urlsplit(srv.get_uri())
        admin_url = f"postgresql://postgres:@{u.hostname or '127.0.0.1'}:{u.port or 5432}/nexasalon_migration_0062_precheck"
        env = os.environ.copy()
        env["NEXASALON_DATABASE_URL"] = admin_url.replace("postgresql://", "postgresql+psycopg://")
        env.pop("NEXASALON_MIGRATIONS_DATABASE_URL", None)

        result = _alembic(env, "upgrade", "0061")
        assert result.returncode == 0, result.stdout + result.stderr

        with psycopg.connect(admin_url) as conn:
            org_id, client_id = _seed_org_and_client(conn, name="Duplicidade Preexistente")
            # Permitido pelo índice ANTIGO (0061): `scheduled` não é
            # coberto ainda, então coexiste com `pending` sem erro.
            _insert_cycle(conn, org_id, client_id, "scheduled")
            _insert_cycle(conn, org_id, client_id, "pending")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM client_recovery_cycles WHERE organization_id = %s AND client_id = %s",
                    (org_id, client_id),
                )
                count_before = cur.fetchone()[0]
            assert count_before == 2

        result = _alembic(env, "upgrade", "0062")
        assert result.returncode != 0
        assert "0062" in (result.stdout + result.stderr)
        assert "múltiplos ciclos" in (result.stdout + result.stderr) or "mais de 1 ciclo" in (result.stdout + result.stderr)

        # Nunca apagou/alterou nada, e nunca avançou a revisão.
        current = _alembic(env, "current")
        assert "0062" not in (current.stdout + current.stderr)
        assert "0061" in (current.stdout + current.stderr)
        with psycopg.connect(admin_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM client_recovery_cycles WHERE organization_id = %s AND client_id = %s",
                    (org_id, client_id),
                )
                assert cur.fetchone()[0] == 2  # intocado
            predicate = _index_predicate(conn)
            assert "scheduled" not in predicate  # índice nunca foi trocado
    finally:
        srv.cleanup()
        shutil.rmtree(pgdata, ignore_errors=True)
