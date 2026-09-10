import os
import subprocess
import sys
from urllib.parse import urlsplit

import psycopg


def test_migration_0049_downgrade_upgrade_and_catalog():
    app_url = urlsplit(os.environ["NEXASALON_DATABASE_URL"].replace("postgresql+psycopg", "postgresql"))
    admin_url = f"postgresql://postgres:@{app_url.hostname}:{app_url.port}/nexasalon_test"
    env = os.environ.copy()
    env["NEXASALON_DATABASE_URL"] = admin_url.replace("postgresql://", "postgresql+psycopg://")

    def alembic(*args: str) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env,
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
