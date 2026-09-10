"""financial categories + cash_movements.financial_category_id
(Resultado disponivel - custos variaveis vs despesas fixas)

Adiciona:
  - `financial_categories`: categoria de lancamento financeiro manual,
    criada livremente por organizacao (mesmo raciocinio de
    `service_categories`/`appointment_custom_statuses`). Campo `nature`
    (novo enum `expense_nature`: fixed|variable) e o que alimenta as
    linhas "(-) Custos variaveis"/"(-) Despesas fixas" do painel
    "Resultado disponivel" — a classificacao pertence a CATEGORIA, nao a
    cada lancamento.
  - `cash_movements.financial_category_id`: FK NULLABLE, SEM backfill —
    todo `CashMovement` existente antes desta migration fica com
    `financial_category_id IS NULL` PARA SEMPRE (ledger append-only,
    nunca editado depois de criado — ver docstring do model). Nenhuma
    despesa antiga e classificada retroativamente; o Dashboard expoe o
    total de lancamentos sem categoria separadamente ("nao
    classificados"), nunca somando-os silenciosamente como fixo ou
    variavel.

RLS em `financial_categories` igual as demais tabelas diretas por
`organization_id`.

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-09
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
_EXPENSE_NATURE_VALUES = ["fixed", "variable"]


def upgrade() -> None:
    values_sql = ", ".join(f"'{v}'" for v in _EXPENSE_NATURE_VALUES)
    op.execute(f"CREATE TYPE expense_nature AS ENUM ({values_sql})")

    op.create_table(
        "financial_categories",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "nature",
            postgresql.ENUM(*_EXPENSE_NATURE_VALUES, name="expense_nature", create_type=False),
            nullable=False,
        ),
        sa.Column("display_order", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_financial_categories_organization_id_name")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_financial_categories_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_categories")),
    )
    op.execute("ALTER TABLE financial_categories ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE financial_categories FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON financial_categories USING (organization_id = {_ORG_ID})")

    op.add_column("cash_movements", sa.Column("financial_category_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_cash_movements_financial_category_id_financial_categories"),
        "cash_movements", "financial_categories", ["financial_category_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_cash_movements_financial_category_id_financial_categories"), "cash_movements", type_="foreignkey"
    )
    op.drop_column("cash_movements", "financial_category_id")

    op.execute("DROP POLICY tenant_isolation ON financial_categories")
    op.execute("ALTER TABLE financial_categories NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE financial_categories DISABLE ROW LEVEL SECURITY")
    op.drop_table("financial_categories")

    op.execute("DROP TYPE expense_nature")
