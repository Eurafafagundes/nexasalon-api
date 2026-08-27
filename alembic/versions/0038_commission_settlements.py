"""commission settlements and adjustments (Etapa C4 - Fechamento/Pagamento de Comissao)

Adiciona:
  - `commission_settlements`: registro IMUTAVEL de "esta comissao foi
    paga" (o ato de criar JA E a confirmacao de pagamento - "Registrar
    pagamento"). RLS igual as demais tabelas diretas por
    `organization_id`. `production_total`/`commission_total` sao somas
    CONGELADAS no momento da criacao, nunca recalculadas depois (ver
    docstring de `models/commission.py::CommissionSettlement`).
  - `commission_adjustments`: correcao manual auditavel (positiva ou
    negativa), nunca altera o snapshot original de `OrderItem` nem um
    settlement ja criado. `order_item_id`/`commission_settlement_id`
    nullable (ajuste pode nascer solto/"pendente").
  - `order_items.commission_settlement_id`: FK nullable, RESTRICT.
    `NULL` = "A pagar"; preenchido = "Pago". CHECK
    `commission_settlement_id_requires_calculated` garante no proprio
    banco que so item com `commission_status=calculated` pode ser
    linkado (nunca `not_configured` nem historico).

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-27
"""
import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "commission_settlements",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("production_total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("commission_total", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=255), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "production_total >= 0", name=op.f("ck_commission_settlements_production_total_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_commission_settlements_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"],
            name=op.f("fk_commission_settlements_professional_id_professionals"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name=op.f("fk_commission_settlements_created_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commission_settlements")),
    )
    op.execute("ALTER TABLE commission_settlements ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE commission_settlements FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON commission_settlements USING (organization_id = {_ORG_ID})")

    op.create_table(
        "commission_adjustments",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("order_item_id", sa.UUID(), nullable=True),
        sa.Column("commission_settlement_id", sa.UUID(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_commission_adjustments_amount_not_zero")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_commission_adjustments_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"],
            name=op.f("fk_commission_adjustments_professional_id_professionals"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["order_items.id"],
            name=op.f("fk_commission_adjustments_order_item_id_order_items"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["commission_settlement_id"], ["commission_settlements.id"],
            name=op.f("fk_commission_adjustments_commission_settlement_id_commission_settlements"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name=op.f("fk_commission_adjustments_created_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commission_adjustments")),
    )
    op.execute("ALTER TABLE commission_adjustments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE commission_adjustments FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON commission_adjustments USING (organization_id = {_ORG_ID})")

    op.add_column("order_items", sa.Column("commission_settlement_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_order_items_commission_settlement_id_commission_settlements"),
        "order_items", "commission_settlements", ["commission_settlement_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_order_items_commission_settlement_id_requires_calculated"),
        "order_items",
        "commission_settlement_id IS NULL OR commission_status = 'calculated'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_order_items_commission_settlement_id_requires_calculated"), "order_items", type_="check"
    )
    op.drop_constraint(
        op.f("fk_order_items_commission_settlement_id_commission_settlements"), "order_items", type_="foreignkey"
    )
    op.drop_column("order_items", "commission_settlement_id")

    op.execute("DROP POLICY tenant_isolation ON commission_adjustments")
    op.execute("ALTER TABLE commission_adjustments NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE commission_adjustments DISABLE ROW LEVEL SECURITY")
    op.drop_table("commission_adjustments")

    op.execute("DROP POLICY tenant_isolation ON commission_settlements")
    op.execute("ALTER TABLE commission_settlements NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE commission_settlements DISABLE ROW LEVEL SECURITY")
    op.drop_table("commission_settlements")
