"""comanda: observacao+auditoria, status personalizado de agendamento,
tipo de item de produto (venda vs consumo interno)

Rodada "Comanda, Auditoria, Status Personalizado e Estoque por Peso".
Três mudanças independentes, agrupadas numa única migration porque
nenhuma depende de rollback isolado da outra nesta etapa:

  - `orders.observation*` (4 colunas) + `observation_version`: observação
    operacional da comanda + auditoria DEDICADA (quem/quando editou por
    ÚLTIMO — nunca reaproveita `orders.updated_at`, que já muda por
    pagamento/produto/status). Mesmo padrão de snapshot já usado em
    `order_product_items.product_name` (`observation_updated_by_name`).
    Concorrência otimista via CONTADOR INTEIRO explícito
    (`observation_version`, nasce em `0` — "nunca editada" é um valor
    real e comparável, não `NULL`), nunca timestamp: um timestamp
    `NULL` inicial não distingue "nunca editada" de "sem controle de
    versão", o que deixava passar duas primeiras edições concorrentes
    sem 409 (auditoria "última correção pré-push", item 2). Cliente
    sempre manda `expected_observation_version` (obrigatório) — servidor
    recusa com 409 se não bater com o valor atual, incrementa em 1 a
    cada edição bem-sucedida.

  - `appointment_custom_statuses` (+ `appointments.custom_status_id`):
    etiqueta colorida OPCIONAL por organização, ortogonal ao enum
    `AppointmentStatus` (nunca substitui/afeta a máquina de estado
    operacional — mesmo isolamento estrutural já comprovado por
    `AppointmentStatusStyle`, ver docstring do model). CRUD completo
    (nome/cor/ativo/ordem), nunca hard-delete de uma linha em uso —
    `set_active(False)` só some da lista de seleção E passa a ser
    recusado como NOVA atribuição no service layer (auditoria "última
    correção pré-push", item 3) — um agendamento que já tinha a
    etiqueta continua exibindo ela normalmente mesmo desativada.

  - `order_product_items.item_type` (`sale` | `consumption`, default
    `sale`): distingue produto VENDIDO à cliente (comportamento
    inalterado) de produto CONSUMIDO internamente durante o serviço
    (ex.: cabelo usado numa progressiva) — item explícito "não misturar
    semântica financeira e de estoque". `unit_price` continua NOT NULL;
    consumo sem cobrança usa `0`, o total da comanda
    (`services/order_totals.py`) já soma `quantity * unit_price` sem
    nenhum branch novo. `server_default='sale'` — 100% retrocompatível,
    nenhuma linha existente muda de sentido.

  - `stock_movements.idempotency_key` (UUID, nullable, único quando
    preenchido): item "correção pós-fechamento" ganha proteção
    TRANSACIONAL contra duplo-submit/retry (auditoria "última correção
    pré-push", item 1) — o cliente gera uma chave por TENTATIVA de
    correção (não por clique) e o backend recusa criar uma segunda
    movimentação compensatória com a mesma chave, via UNIQUE INDEX
    parcial (nunca só um `disabled` de botão no frontend). Nullable e
    sem índice único incondicional: só a correção de consumo usa isto
    hoje — todo outro fluxo de movimentação continua sem chave, e
    Postgres trata múltiplos `NULL` como não-conflitantes num índice
    único parcial `WHERE idempotency_key IS NOT NULL`.

Amendment (auditoria "última correção pré-push"): migration NUNCA
executada em nenhum ambiente compartilhado (criada nesta mesma sessão,
sem acesso a Postgres local o tempo todo) — corrigida diretamente aqui
em vez de encadear uma 0040, sem risco de reescrever histórico já
aplicado em algum lugar.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-28
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
_ORDER_PRODUCT_ITEM_KIND_VALUES = ["sale", "consumption"]


def upgrade() -> None:
    # --- Observação da Comanda + Auditoria dedicada ---------------------
    op.add_column("orders", sa.Column("observation", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("observation_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("observation_updated_by", sa.UUID(), nullable=True))
    op.add_column("orders", sa.Column("observation_updated_by_name", sa.String(length=160), nullable=True))
    op.create_foreign_key(
        op.f("fk_orders_observation_updated_by_users"),
        "orders", "users", ["observation_updated_by"], ["id"], ondelete="SET NULL",
    )
    # Concorrência otimista — contador explícito, nasce em 0 ("nunca
    # editada" é um valor real, não NULL). Ver docstring do módulo.
    op.add_column("orders", sa.Column("observation_version", sa.Integer(), nullable=False, server_default="0"))

    # --- order_product_items.item_type (sale | consumption) -------------
    values_sql = ", ".join(f"'{v}'" for v in _ORDER_PRODUCT_ITEM_KIND_VALUES)
    op.execute(f"CREATE TYPE order_product_item_kind AS ENUM ({values_sql})")
    op.add_column(
        "order_product_items",
        sa.Column(
            "item_type",
            postgresql.ENUM(*_ORDER_PRODUCT_ITEM_KIND_VALUES, name="order_product_item_kind", create_type=False),
            nullable=False,
            server_default="sale",
        ),
    )

    # --- Status personalizado de agendamento -----------------------------
    op.create_table(
        "appointment_custom_statuses",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("color_hex", sa.String(length=7), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "color_hex ~ '^#[0-9A-Fa-f]{6}$'", name=op.f("ck_appointment_custom_statuses_color_hex_format")
        ),
        sa.UniqueConstraint(
            "organization_id", "name", name=op.f("uq_appointment_custom_statuses_organization_id_name")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_appointment_custom_statuses_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointment_custom_statuses")),
    )
    op.execute("ALTER TABLE appointment_custom_statuses ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointment_custom_statuses FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON appointment_custom_statuses USING (organization_id = {_ORG_ID})"
    )

    op.add_column("appointments", sa.Column("custom_status_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_appointments_custom_status_id_appointment_custom_statuses"),
        "appointments", "appointment_custom_statuses", ["custom_status_id"], ["id"], ondelete="SET NULL",
    )

    # --- stock_movements.idempotency_key (correção pós-fechamento) ------
    op.add_column("stock_movements", sa.Column("idempotency_key", sa.UUID(), nullable=True))
    op.create_index(
        "uq_stock_movements_idempotency_key",
        "stock_movements", ["idempotency_key"], unique=True,
        postgresql_where="idempotency_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index("uq_stock_movements_idempotency_key", table_name="stock_movements", postgresql_where="idempotency_key IS NOT NULL")
    op.drop_column("stock_movements", "idempotency_key")

    op.drop_constraint(
        op.f("fk_appointments_custom_status_id_appointment_custom_statuses"), "appointments", type_="foreignkey"
    )
    op.drop_column("appointments", "custom_status_id")

    op.execute("DROP POLICY tenant_isolation ON appointment_custom_statuses")
    op.execute("ALTER TABLE appointment_custom_statuses NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointment_custom_statuses DISABLE ROW LEVEL SECURITY")
    op.drop_table("appointment_custom_statuses")

    op.drop_column("order_product_items", "item_type")
    op.execute("DROP TYPE order_product_item_kind")

    op.drop_column("orders", "observation_version")
    op.drop_constraint(op.f("fk_orders_observation_updated_by_users"), "orders", type_="foreignkey")
    op.drop_column("orders", "observation_updated_by_name")
    op.drop_column("orders", "observation_updated_by")
    op.drop_column("orders", "observation_updated_at")
    op.drop_column("orders", "observation")
