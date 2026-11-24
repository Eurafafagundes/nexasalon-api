"""Migration 0049 — Despesas Fixas (`fixed_expenses`/`organization_tax_rates`).

Isolamento (correção pós-NexaLink): este teste fazia `alembic
downgrade 0048` + `alembic upgrade 0049` diretamente no banco
COMPARTILHADO da suíte (`NEXASALON_DATABASE_URL`, o mesmo que
`conftest.py` já sobe migrado até `heads` pra TODOS os outros testes).
Isso deixava o banco compartilhado PARADO na revisão 0049 pro resto da
sessão — qualquer teste de outro arquivo que rodasse depois, na mesma
sessão pytest, perdia todo o schema de 0050 em diante (ex.: as tabelas
novas do NexaLink, `organization_public_pages`/`organization_public_
links`/`public_page_events`, ou colunas como `audit_logs.user_name`),
causando `UndefinedTable`/`UndefinedColumn` em massa só quando a suíte
COMPLETA rodava numa única sessão — nunca quando um arquivo rodava
isolado ou em combinações menores (por isso o problema só apareceu ao
rodar `pytest` sem filtro).

Corrigido pelo MESMO padrão já usado em `test_migration_0014_backfill.py`:
Postgres descartável PRÓPRIO (`pgserver`, diretório `.pgdata_test_0049`
dedicado, nunca reaproveitando `.pgdata_test`/`.pgdata_migration_test`
de outro arquivo), destruído no fim do módulo. O banco compartilhado da
suíte principal nunca é tocado por este teste."""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pgserver
import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_PGDATA = str(REPO_ROOT / ".pgdata_test_0049")


@pytest.fixture(scope="module")
def migration_db():
    shutil.rmtree(_PGDATA, ignore_errors=True)
    srv = pgserver.get_server(_PGDATA)
    try:
        srv.psql("CREATE DATABASE nexasalon_migration_0049_test;")
    except Exception:
        pass

    u = urlsplit(srv.get_uri())
    admin_url = (
        f"postgresql://postgres:@{u.hostname or '127.0.0.1'}:{u.port or 5432}/nexasalon_migration_0049_test"
    )
    env = os.environ.copy()
    env["NEXASALON_DATABASE_URL"] = admin_url.replace("postgresql://", "postgresql+psycopg://")
    env.pop("NEXASALON_MIGRATIONS_DATABASE_URL", None)

    # Sobe o schema completo (mesma lógica de `heads` do conftest.py
    # principal) ANTES do downgrade — precisa de um estado real de
    # onde descer até 0048, isolado do banco compartilhado.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"falha ao subir heads no banco isolado:\n{result.stdout}\n{result.stderr}"

    yield admin_url, env

    srv.cleanup()
    shutil.rmtree(_PGDATA, ignore_errors=True)


def test_migration_0049_downgrade_upgrade_and_catalog(migration_db):
    admin_url, env = migration_db

    def alembic(*args: str) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, check=True,
        )
        return result.stdout + result.stderr

    alembic("downgrade", "0048")
    assert "0048" in alembic("current")
    with psycopg.connect(admin_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.fixed_expenses')")
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='cash_movements' AND column_name='fixed_expense_id'"
        )
        assert cursor.fetchone() is None
    alembic("upgrade", "0049")
    assert "0049" in alembic("current")

    with psycopg.connect(admin_url) as connection, connection.cursor() as cursor:
        for table in (
            "organization_tax_rates", "financial_categories", "fixed_expenses",
            "fixed_expense_versions",
        ):
            cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            assert cursor.fetchone()[0] == table
        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='cash_movements' AND column_name='fixed_expense_id'"
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            "SELECT typname FROM pg_type WHERE typname IN "
            "('fixed_expense_recurrence', 'expense_nature') ORDER BY typname"
        )
        assert [row[0] for row in cursor.fetchall()] == ["expense_nature", "fixed_expense_recurrence"]
        cursor.execute(
            "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_type.oid=pg_enum.enumtypid "
            "WHERE pg_type.typname='fixed_expense_recurrence'"
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "monthly", "quarterly", "semiannual", "annual",
        }
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid IN "
            "('fixed_expenses'::regclass, 'fixed_expense_versions'::regclass, 'cash_movements'::regclass)"
        )
        constraints = {row[0] for row in cursor.fetchall()}
        assert {
            "fk_fixed_expenses_organization_id_organizations",
            "fk_fixed_expenses_created_by_users",
            "fk_fixed_expense_versions_organization_id_organizations",
            "fk_fixed_expense_versions_fixed_expense_id_fixed_expenses",
            "fk_fixed_expense_versions_branch_id_branches",
            "fk_cash_movements_fixed_expense_id_fixed_expenses",
            "uq_fixed_expense_versions_fixed_expense_id_effective_from",
        } <= constraints
        cursor.execute(
            "SELECT confrelid::regclass::text FROM pg_constraint "
            "WHERE conrelid='fixed_expense_versions'::regclass AND contype='f'"
        )
        assert "financial_categories" in {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename IN "
            "('fixed_expenses', 'fixed_expense_versions', 'cash_movements')"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert {
            "ix_fixed_expenses_organization_id",
            "ix_fixed_expense_versions_organization_id",
            "ix_fixed_expense_versions_fixed_expense_id",
            "ix_fixed_expense_versions_branch_id",
            "ix_fixed_expense_versions_financial_category_id",
            "ix_fixed_expense_versions_period",
            "ix_cash_movements_fixed_expense_id",
        } <= indexes
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('fixed_expenses', 'fixed_expense_versions') ORDER BY relname"
        )
        assert cursor.fetchall() == [
            ("fixed_expense_versions", True, True), ("fixed_expenses", True, True)
        ]
        cursor.execute(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE tablename IN ('fixed_expenses', 'fixed_expense_versions') ORDER BY tablename"
        )
        assert cursor.fetchall() == [
            ("fixed_expense_versions", "tenant_isolation"),
            ("fixed_expenses", "tenant_isolation"),
        ]
