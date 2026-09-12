"""order item benefit (Cartao Fidelidade / Cortesia)

Rodada "Benefício por Item": hoje, pra dar um servico de graca via
Cartao Fidelidade, o atendente editava manualmente `OrderItem.price`
para R$0 -- isso zerava junto o Faturamento (`price` e a unica base
de Faturamento) e a comissao percentual (`resolve_commission(price=
item.price, ...)` nunca consulta o catalogo, so recebe o valor do
item). Esta migration adiciona os dois campos que separam "valor
economico do servico" (permanece em `price`, intocado) de "valor
efetivamente cobrado do cliente" (derivado: `price - benefit_amount`,
ver `services/order_totals.py::item_charged_amount`).

  - `benefit_type` (novo enum `benefit_type`: `loyalty` | `courtesy`)
  - `benefit_amount` (NUMERIC(10,2))

Os dois vem juntos (NULL+NULL = sem beneficio, ou ambos preenchidos
com `0 <= benefit_amount <= price`) -- reforcado tambem por
CheckConstraint no banco, nao so na service layer.

Todas NULLABLE, SEM backfill -- `OrderItem`s existentes ficam com os
dois campos NULL pra sempre (== comportamento identico ao atual,
sem beneficio). Nunca cria Payment nem CashMovement pro valor do
beneficio -- a diferenca simplesmente nao precisa de nenhum
lancamento financeiro.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-11
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

_BENEFIT_TYPE_VALUES = ["loyalty", "courtesy"]

CHECK_NAME = "benefit_requires_type_and_amount_within_price"
CHECK_SQL = (
    "(benefit_type IS NULL AND benefit_amount IS NULL) OR "
    "(benefit_type IS NOT NULL AND benefit_amount IS NOT NULL "
    "AND benefit_amount >= 0 AND benefit_amount <= price)"
)


def upgrade() -> None:
    values_sql = ", ".join(f"'{v}'" for v in _BENEFIT_TYPE_VALUES)
    op.execute(f"CREATE TYPE benefit_type AS ENUM ({values_sql})")

    op.add_column(
        "order_items",
        sa.Column(
            "benefit_type",
            postgresql.ENUM(*_BENEFIT_TYPE_VALUES, name="benefit_type", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("order_items", sa.Column("benefit_amount", sa.Numeric(precision=10, scale=2), nullable=True))
    op.create_check_constraint(CHECK_NAME, "order_items", CHECK_SQL)


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, "order_items", type_="check")
    op.drop_column("order_items", "benefit_amount")
    op.drop_column("order_items", "benefit_type")

    op.execute("DROP TYPE benefit_type")
