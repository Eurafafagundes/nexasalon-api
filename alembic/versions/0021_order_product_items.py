"""Estoque ↔ Comanda — linha de produto na comanda (order_product_items)

Etapa C do prompt mestre ("Integração Estoque ↔ Comanda + edição
auditada de valor/duração"), em cima da Etapa B já aprovada e commitada
(0020 — Produtos/Movimentações/Transferência/Inventário). Aditiva,
nenhuma migration anterior é alterada.

Uma tabela nova: `order_product_items` — linha de PRODUTO dentro de uma
comanda (`Order`), deliberadamente separada de `order_items` (linha de
SERVIÇO) — item explícito "separar claramente item de serviço e item
de produto". `unit_price` é snapshot de `Product.sale_price` no
momento em que o produto é adicionado à comanda (mesma filosofia de
`order_items.price`). `stock_movement_id` (nullable, FK pra
`stock_movements`, `SET NULL`) fica vazio enquanto a comanda está
aberta — a baixa de estoque só acontece no FECHAMENTO da comanda
(`services/orders.py::close_order` -> `services/stock.py::
record_sale_movement`) e este campo funciona como o marcador de
idempotência que impede uma segunda baixa pro mesmo item (a comanda em
si também é travada com `SELECT ... FOR UPDATE` no fechamento — ver
docstring de `close_order` — então isto é defesa em profundidade, não
o único mecanismo).

RLS: mesmo padrão ENABLE+FORCE+tenant_isolation de toda tabela nova
desta base (0014/0017/0019/0020).

Nenhuma permission nova: reaproveita `orders.manage`/`orders.edit_price`
já existentes (Comanda continua sendo uma fronteira de autorização só,
produto dentro dela não vira um domínio de permissão à parte) — ver
`services/orders.py`.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "order_product_items",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("product_name", sa.String(length=160), nullable=False),
        sa.Column("stock_movement_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_product_items_quantity_positive")),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_order_product_items_unit_price_not_negative")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_order_product_items_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_product_items_order_id_orders"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"],
            name=op.f("fk_order_product_items_product_id_products"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stock_movement_id"], ["stock_movements.id"],
            name=op.f("fk_order_product_items_stock_movement_id_stock_movements"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_product_items")),
    )
    op.create_index("ix_order_product_items_organization_id", "order_product_items", ["organization_id"], unique=False)
    op.create_index("ix_order_product_items_order_id", "order_product_items", ["order_id"], unique=False)
    op.create_index("ix_order_product_items_product_id", "order_product_items", ["product_id"], unique=False)

    op.execute("ALTER TABLE order_product_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE order_product_items FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON order_product_items USING (organization_id = {_ORG_ID})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON order_product_items")
    op.drop_index("ix_order_product_items_product_id", table_name="order_product_items")
    op.drop_index("ix_order_product_items_order_id", table_name="order_product_items")
    op.drop_index("ix_order_product_items_organization_id", table_name="order_product_items")
    op.drop_table("order_product_items")
