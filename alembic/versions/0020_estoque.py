"""Estoque — Produtos, Movimentações, Transferência entre unidades e Inventário

Etapa B do prompt mestre ("Estoque + Produtos + Movimentações +
Inventário"), implementada diretamente em cima da Etapa A já aprovada
(0019 — escopo granular de agenda). Nenhuma migration anterior é
alterada; tudo aqui é aditivo.

Seis tabelas novas:

  - `products`            — catálogo (nome/categoria/SKU/unidade/custo/
                             preço de venda/fornecedor/ativo/pra-venda-
                             ou-uso-interno). SEM campo de quantidade —
                             ver docstring de `models/product.py`.
  - `stock_levels`        — saldo por (produto, unidade/branch):
                             quantidade em mão + mínimo. Nunca um total
                             "global" por produto (item explícito do
                             pedido "nunca misturar global com
                             quantidade por filial").
  - `stock_movements`     — ledger append-only de TODA mudança de
                             quantidade (entrada/saída, motivo,
                             quantidade, custo unitário opcional,
                             responsável, observação, vínculo opcional
                             com Comanda/Transferência/Inventário).
                             Nenhum endpoint de update/delete — correção
                             é sempre uma nova movimentação.
  - `stock_transfers`     — origem/destino/produto/quantidade; gera o
                             par de movimentações (-N/+N) via
                             `stock_movements.transfer_id`. Nunca cria
                             lançamento financeiro.
  - `inventory_counts` e
    `inventory_count_items` — contagem de inventário por unidade:
                             contagem do sistema vs. contagem real,
                             gera movimentações `inventory_count` por
                             diferença ao fechar. Nunca sobrescreve
                             quantidade silenciosamente.

RLS em todas as seis (mesmo padrão `ENABLE`+`FORCE`+`tenant_isolation`
de 0014/0017/0019).

Três permissions novas no catálogo (padrão de 0011 — reaproveita o
catálogo existente, só acrescenta chaves):

  - `inventory.view`       — ver produtos/saldo/movimentações, SEM
                              custo (`cost_price`/`unit_cost` nunca
                              aparecem na resposta pra quem só tem
                              esta).
  - `inventory.view_cost`  — adicional: inclui custo e KPIs
                              financeiros (ex.: "valor em estoque").
                              Implementa o item explícito "Ver estoque
                              ≠ Ver custo dos produtos" — são duas
                              permissions independentes, não uma
                              hierarquia implícita (`services/stock.py`/
                              `schemas/product.py` checam as duas
                              separadamente).
  - `inventory.manage`     — criar/editar produto, registrar
                              movimentação, transferir, abrir/fechar
                              inventário.

Concedidas: OWNER e ADMIN recebem as três. RECEPTIONIST recebe só
`inventory.view` (precisa saber o que tem em estoque pra vender/avisar
o cliente, mas não gerencia nem vê custo). PROFESSIONAL não recebe
nenhuma nesta revisão (fora do escopo do que atende hoje) — mesmo
critério conservador de 0011 ("pode ser revisto depois").

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

ENUMS = {
    "product_unit": ["unit", "ml", "liter", "gram", "kg", "box", "pack", "meter", "dose", "pair"],
    "stock_movement_direction": ["in", "out"],
    "stock_movement_reason": [
        "purchase", "return", "adjustment", "inventory_count", "transfer_in",
        "sale", "internal_use", "damage", "transfer_out",
    ],
    "inventory_count_status": ["open", "closed"],
}

RLS_TABLES = [
    "products",
    "stock_levels",
    "stock_transfers",
    "inventory_counts",
    "stock_movements",
    "inventory_count_items",
]

PERMISSIONS = [
    ("inventory.view", "inventory", "Visualizar produtos, saldo de estoque e movimentações (sem custo)"),
    ("inventory.view_cost", "inventory", "Visualizar custo dos produtos e valor de estoque"),
    ("inventory.manage", "inventory", "Gerenciar produtos, movimentações, transferências e inventário"),
]

GRANTS = {
    "OWNER": ["inventory.view", "inventory.view_cost", "inventory.manage"],
    "ADMIN": ["inventory.view", "inventory.view_cost", "inventory.manage"],
    "RECEPTIONIST": ["inventory.view"],
}


def upgrade() -> None:
    for name, values in ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")

    op.create_table(
        "products",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("sku", sa.String(length=60), nullable=True),
        sa.Column(
            "unit", postgresql.ENUM(*ENUMS["product_unit"], name="product_unit", create_type=False),
            server_default="unit", nullable=False,
        ),
        sa.Column("cost_price", sa.Numeric(precision=10, scale=2), server_default="0", nullable=False),
        sa.Column("sale_price", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("supplier_name", sa.String(length=160), nullable=True),
        sa.Column("for_sale", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cost_price >= 0", name=op.f("ck_products_cost_price_not_negative")),
        sa.CheckConstraint("sale_price IS NULL OR sale_price >= 0", name=op.f("ck_products_sale_price_not_negative")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_products_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("organization_id", "sku", name=op.f("uq_products_organization_id_sku")),
    )
    op.create_index("ix_products_organization_id", "products", ["organization_id"], unique=False)
    op.create_index("ix_products_is_active", "products", ["is_active"], unique=False)

    op.create_table(
        "stock_levels",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column("quantity_on_hand", sa.Numeric(precision=12, scale=3), server_default="0", nullable=False),
        sa.Column("minimum_quantity", sa.Numeric(precision=12, scale=3), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity_on_hand >= 0", name=op.f("ck_stock_levels_quantity_not_negative")),
        sa.CheckConstraint("minimum_quantity >= 0", name=op.f("ck_stock_levels_minimum_not_negative")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_stock_levels_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_stock_levels_product_id_products"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f("fk_stock_levels_branch_id_branches"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_levels")),
        sa.UniqueConstraint("product_id", "branch_id", name=op.f("uq_stock_levels_product_id_branch_id")),
    )
    op.create_index("ix_stock_levels_organization_id", "stock_levels", ["organization_id"], unique=False)
    op.create_index("ix_stock_levels_branch_id", "stock_levels", ["branch_id"], unique=False)

    op.create_table(
        "stock_transfers",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("origin_branch_id", sa.UUID(), nullable=False),
        sa.Column("destination_branch_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("observation", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_by_name", sa.String(length=255), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_stock_transfers_quantity_positive")),
        sa.CheckConstraint(
            "origin_branch_id != destination_branch_id", name=op.f("ck_stock_transfers_distinct_branches")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_stock_transfers_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_stock_transfers_product_id_products"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["origin_branch_id"], ["branches.id"],
            name=op.f("fk_stock_transfers_origin_branch_id_branches"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_branch_id"], ["branches.id"],
            name=op.f("fk_stock_transfers_destination_branch_id_branches"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_stock_transfers_created_by_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_transfers")),
    )
    op.create_index("ix_stock_transfers_organization_id", "stock_transfers", ["organization_id"], unique=False)

    op.create_table(
        "inventory_counts",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*ENUMS["inventory_count_status"], name="inventory_count_status", create_type=False),
            server_default="open", nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_by_name", sa.String(length=255), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.UUID(), nullable=True),
        sa.Column("closed_by_name", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_inventory_counts_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f("fk_inventory_counts_branch_id_branches"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_inventory_counts_created_by_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_inventory_counts_closed_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_counts")),
    )
    op.create_index("ix_inventory_counts_organization_id", "inventory_counts", ["organization_id"], unique=False)
    op.create_index("ix_inventory_counts_status", "inventory_counts", ["status"], unique=False)

    op.create_table(
        "stock_movements",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column(
            "direction",
            postgresql.ENUM(*ENUMS["stock_movement_direction"], name="stock_movement_direction", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "reason",
            postgresql.ENUM(*ENUMS["stock_movement_reason"], name="stock_movement_reason", create_type=False),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("observation", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_by_name", sa.String(length=255), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("transfer_id", sa.UUID(), nullable=True),
        sa.Column("inventory_count_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_stock_movements_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_stock_movements_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_stock_movements_product_id_products"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f("fk_stock_movements_branch_id_branches"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_stock_movements_created_by_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_stock_movements_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"], ["stock_transfers.id"],
            name=op.f("fk_stock_movements_transfer_id_stock_transfers"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_count_id"], ["inventory_counts.id"],
            name=op.f("fk_stock_movements_inventory_count_id_inventory_counts"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_movements")),
    )
    op.create_index("ix_stock_movements_organization_id", "stock_movements", ["organization_id"], unique=False)
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id"], unique=False)
    op.create_index("ix_stock_movements_branch_id", "stock_movements", ["branch_id"], unique=False)
    op.create_index("ix_stock_movements_transfer_id", "stock_movements", ["transfer_id"], unique=False)
    op.create_index(
        "ix_stock_movements_inventory_count_id", "stock_movements", ["inventory_count_id"], unique=False
    )

    op.create_table(
        "inventory_count_items",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("inventory_count_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("system_quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("counted_quantity", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("system_quantity >= 0", name=op.f("ck_inventory_count_items_system_not_negative")),
        sa.CheckConstraint(
            "counted_quantity IS NULL OR counted_quantity >= 0",
            name=op.f("ck_inventory_count_items_counted_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_inventory_count_items_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_count_id"], ["inventory_counts.id"],
            name=op.f("fk_inventory_count_items_inventory_count_id_inventory_counts"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"],
            name=op.f("fk_inventory_count_items_product_id_products"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_count_items")),
        sa.UniqueConstraint(
            "inventory_count_id", "product_id", name=op.f("uq_inventory_count_items_inventory_count_id_product_id")
        ),
    )
    op.create_index(
        "ix_inventory_count_items_organization_id", "inventory_count_items", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_inventory_count_items_inventory_count_id", "inventory_count_items", ["inventory_count_id"], unique=False
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {_ORG_ID})")

    conn = op.get_bind()
    for key, module, description in PERMISSIONS:
        conn.exec_driver_sql(
            "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
            (key, module, description),
        )
    for role_name, keys in GRANTS.items():
        for key in keys:
            conn.exec_driver_sql(
                "INSERT INTO role_permissions (role_id, permission_key) "
                "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
                (key, role_name),
            )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key IN ('inventory.view', 'inventory.view_cost', 'inventory.manage')"
    )
    conn.exec_driver_sql(
        "DELETE FROM permissions WHERE key IN ('inventory.view', 'inventory.view_cost', 'inventory.manage')"
    )

    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_table("inventory_count_items")
    op.drop_table("stock_movements")
    op.drop_table("inventory_counts")
    op.drop_table("stock_transfers")
    op.drop_table("stock_levels")
    op.drop_table("products")

    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
