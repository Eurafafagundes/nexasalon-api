"""order item commission snapshot (Etapa C2 - Comissao)

Adiciona em `order_items` o snapshot IMUTAVEL da comissao vigente no
momento do FECHAMENTO da comanda (`close_order`/
`close_orders_consolidated` -> `services/commissions.py::resolve_commission`),
espelhando o mesmo padrao ja usado pra taxa de pagamento na Etapa N3
(`payments.fee_*_snapshot`/`fee_status`):

  - `commission_type_snapshot` (reusa o enum `commission_type` ja
    existente desde a migration 0002, mesmo usado em
    `professional_services.commission_type`)
  - `commission_value_snapshot`
  - `commission_amount_snapshot`
  - `commission_status` (novo enum `commission_status`: `calculated` |
    `not_configured` - "sem regra configurada" NUNCA vira comissao
    zero, fica explicitamente `not_configured` com os 3 snapshots em
    NULL)

Todas NULLABLE, SEM backfill - `OrderItem` fechados antes desta
migration ficam com os 4 campos `NULL` pra sempre; nenhuma comissao
historica e inventada retroativamente. Editar
`professional_services.commission_type`/`commission_value` depois
NUNCA recalcula um `OrderItem` ja fechado.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-23
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_COMMISSION_STATUS_VALUES = ["calculated", "not_configured"]


def upgrade() -> None:
    values_sql = ", ".join(f"'{v}'" for v in _COMMISSION_STATUS_VALUES)
    op.execute(f"CREATE TYPE commission_status AS ENUM ({values_sql})")

    op.add_column(
        "order_items",
        sa.Column(
            "commission_type_snapshot",
            postgresql.ENUM("percentage", "fixed", name="commission_type", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("order_items", sa.Column("commission_value_snapshot", sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column("order_items", sa.Column("commission_amount_snapshot", sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column(
        "order_items",
        sa.Column(
            "commission_status",
            postgresql.ENUM(*_COMMISSION_STATUS_VALUES, name="commission_status", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("order_items", "commission_status")
    op.drop_column("order_items", "commission_amount_snapshot")
    op.drop_column("order_items", "commission_value_snapshot")
    op.drop_column("order_items", "commission_type_snapshot")

    op.execute("DROP TYPE commission_status")
