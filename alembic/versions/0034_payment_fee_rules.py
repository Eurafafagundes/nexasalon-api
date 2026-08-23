"""payment fee rules and payment fee snapshot (Etapa N3 - Taxas de Pagamento)

Adiciona:
  - `payment_fee_rules`: taxa percentual configurada por organização,
    por (forma de pagamento, bandeira, parcelas) — só débito/crédito
    fazem sentido aqui (Pix/Dinheiro nunca têm incidência de taxa nesta
    etapa). RLS igual às demais tabelas diretas por `organization_id`.
  - `payments.payment_fee_rule_id`/`fee_percent_snapshot`/
    `fee_amount_snapshot`/`net_amount_snapshot`/`fee_status`: TODAS
    NULLABLE, sem backfill — pagamentos existentes antes desta migration
    continuam com os 5 campos `NULL` pra sempre (nenhuma taxa histórica
    é inventada). `fee_status` (novo enum `payment_fee_status`) é o
    diferenciador ESTRUTURADO entre "sem incidência de taxa" (Pix/
    Dinheiro), "taxa calculada" (cartão com regra encontrada) e "taxa
    não configurada" (cartão sem regra) — nunca depender só da nulidade
    dos snapshots pra essa distinção (ver docstring de
    `models/enums.py::PaymentFeeStatus`).

A taxa aplicada numa venda fica congelada no `Payment` (snapshot) —
editar `payment_fee_rules.fee_percent` depois NUNCA recalcula
pagamentos já criados.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-23
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

_PAYMENT_METHOD_VALUES = [
    "pix", "cash", "debit", "credit", "loyalty_card", "voucher", "barter", "transfer", "bank_slip",
]
_CARD_BRAND_VALUES = ["visa", "mastercard", "elo", "amex", "hipercard", "other"]
_PAYMENT_FEE_STATUS_VALUES = ["not_applicable", "calculated", "unconfigured"]


def upgrade() -> None:
    values_sql = ", ".join(f"'{v}'" for v in _PAYMENT_FEE_STATUS_VALUES)
    op.execute(f"CREATE TYPE payment_fee_status AS ENUM ({values_sql})")

    op.create_table(
        "payment_fee_rules",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "method",
            postgresql.ENUM(*_PAYMENT_METHOD_VALUES, name="payment_method", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "card_brand",
            postgresql.ENUM(*_CARD_BRAND_VALUES, name="card_brand", create_type=False),
            nullable=False,
        ),
        sa.Column("installments", sa.SmallInteger(), nullable=False),
        sa.Column("fee_percent", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("method = 'debit' OR method = 'credit'", name=op.f("ck_payment_fee_rules_method_is_card")),
        sa.CheckConstraint("installments >= 1", name=op.f("ck_payment_fee_rules_installments_positive")),
        sa.CheckConstraint("fee_percent >= 0", name=op.f("ck_payment_fee_rules_fee_percent_not_negative")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_payment_fee_rules_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_fee_rules")),
        sa.UniqueConstraint(
            "organization_id", "method", "card_brand", "installments",
            name=op.f("uq_payment_fee_rules_organization_id_method_card_brand_installments"),
        ),
    )

    # RLS: mesmo padrão das demais tabelas "diretas" com organization_id
    # (ver 0003/0008/0009/0013).
    op.execute("ALTER TABLE payment_fee_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE payment_fee_rules FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON payment_fee_rules USING (organization_id = {_ORG_ID})")

    op.add_column("payments", sa.Column("payment_fee_rule_id", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("fee_percent_snapshot", sa.Numeric(precision=5, scale=2), nullable=True))
    op.add_column("payments", sa.Column("fee_amount_snapshot", sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column("payments", sa.Column("net_amount_snapshot", sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column(
        "payments",
        sa.Column(
            "fee_status",
            postgresql.ENUM(*_PAYMENT_FEE_STATUS_VALUES, name="payment_fee_status", create_type=False),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        op.f("fk_payments_payment_fee_rule_id_payment_fee_rules"),
        "payments",
        "payment_fee_rules",
        ["payment_fee_rule_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_payments_payment_fee_rule_id_payment_fee_rules"), "payments", type_="foreignkey")
    op.drop_column("payments", "fee_status")
    op.drop_column("payments", "net_amount_snapshot")
    op.drop_column("payments", "fee_amount_snapshot")
    op.drop_column("payments", "fee_percent_snapshot")
    op.drop_column("payments", "payment_fee_rule_id")

    op.execute("DROP POLICY tenant_isolation ON payment_fee_rules")
    op.execute("ALTER TABLE payment_fee_rules NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE payment_fee_rules DISABLE ROW LEVEL SECURITY")
    op.drop_table("payment_fee_rules")

    op.execute("DROP TYPE payment_fee_status")
