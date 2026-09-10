"""fixed expenses with immutable competence versions

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
_RECURRENCES = ["monthly", "quarterly", "semiannual", "annual"]


def upgrade() -> None:
    op.execute(
        "CREATE TYPE fixed_expense_recurrence AS ENUM ('monthly', 'quarterly', 'semiannual', 'annual')"
    )
    op.create_table(
        "fixed_expenses",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(255), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("fk_fixed_expenses_organization_id_organizations"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_fixed_expenses_created_by_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fixed_expenses")),
    )
    op.create_index(
        "ix_fixed_expenses_organization_id", "fixed_expenses", ["organization_id"]
    )
    op.create_table(
        "fixed_expense_versions",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("fixed_expense_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column("financial_category_id", sa.UUID(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("category_name_snapshot", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "recurrence",
            postgresql.ENUM(
                *_RECURRENCES, name="fixed_expense_recurrence", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("due_day", sa.SmallInteger(), nullable=False),
        sa.Column("start_month", sa.Date(), nullable=False),
        sa.Column("end_month", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount > 0", name="ck_fixed_expense_versions_amount_positive"
        ),
        sa.CheckConstraint(
            "due_day >= 1 AND due_day <= 31",
            name="ck_fixed_expense_versions_due_day_between_1_and_31",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_fixed_expense_versions_effective_range_valid",
        ),
        sa.CheckConstraint(
            "end_month IS NULL OR end_month >= start_month",
            name="ck_fixed_expense_versions_end_not_before_start",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("fk_fixed_expense_versions_organization_id_organizations"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["fixed_expense_id"], ["fixed_expenses.id"], name=op.f("fk_fixed_expense_versions_fixed_expense_id_fixed_expenses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"], name=op.f("fk_fixed_expense_versions_branch_id_branches"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["financial_category_id"], ["financial_categories.id"], name=op.f("fk_fixed_expense_versions_financial_category_id_financial_categories"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fixed_expense_versions")),
        sa.UniqueConstraint("fixed_expense_id", "effective_from", name=op.f("uq_fixed_expense_versions_fixed_expense_id_effective_from")),
    )
    op.create_index(
        "ix_fixed_expense_versions_organization_id",
        "fixed_expense_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_fixed_expense_versions_fixed_expense_id",
        "fixed_expense_versions",
        ["fixed_expense_id"],
    )
    op.create_index(
        "ix_fixed_expense_versions_branch_id", "fixed_expense_versions", ["branch_id"]
    )
    op.create_index(op.f("ix_fixed_expense_versions_financial_category_id"), "fixed_expense_versions", ["financial_category_id"])
    op.create_index(
        "ix_fixed_expense_versions_period",
        "fixed_expense_versions",
        ["organization_id", "branch_id", "effective_from", "effective_to"],
    )
    for table in ("fixed_expenses", "fixed_expense_versions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {_ORG})"
        )
    op.add_column("cash_movements", sa.Column("fixed_expense_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_cash_movements_fixed_expense_id_fixed_expenses"), "cash_movements", "fixed_expenses",
        ["fixed_expense_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(op.f("ix_cash_movements_fixed_expense_id"), "cash_movements", ["fixed_expense_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_cash_movements_fixed_expense_id"), table_name="cash_movements")
    op.drop_constraint(op.f("fk_cash_movements_fixed_expense_id_fixed_expenses"), "cash_movements", type_="foreignkey")
    op.drop_column("cash_movements", "fixed_expense_id")
    for table in ("fixed_expense_versions", "fixed_expenses"):
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("fixed_expense_versions")
    op.drop_table("fixed_expenses")
    op.execute("DROP TYPE fixed_expense_recurrence")
