"""clientes/financeiro — evolução funcional (Clientes, Caixa por
unidade, Entradas/Saídas, número de comanda, snapshot de nome, Extrato)

Regra crítica desta rodada (item 32, explícita no pedido): o Neon de
staging JÁ TEM dado real (a 0013/0014 já foram aplicadas, já existe
comanda/pagamento de teste). NUNCA assumir tabela vazia. Cada mudança
abaixo segue o padrão seguro apropriado ao risco real de cada coluna:

  - `clients.*` (cpf, cep, state, city, neighborhood, address_line,
    complement): todas NULLABLE, sem backfill necessário — são campos
    novos e opcionais por definição (item "não torne CPF nem endereço
    obrigatórios"), então linhas existentes ficam corretamente com
    tudo NULL.

  - `cash_registers.branch_id`: NULLABLE. Não dá pra inferir com
    segurança qual unidade um caixa HISTÓRICO pertence (pode não haver
    nenhuma pista no dado legado), e forçar um valor arbitrário seria
    pior que deixar nulo — a API passa a EXIGIR `branch_id` em toda
    abertura NOVA (`CashRegisterOpen.branch_id`), mas caixas antigos
    continuam válidos e consultáveis com o campo vazio.

  - `cash_movements.category`: NULLABLE (opcional por definição).
    `cash_movements.method`: NOT NULL com `server_default='cash'` —
    isto é seguro adicionar em UM PASSO SÓ mesmo com linha existente
    (diferente de `payments.cash_register_id` na 0014, que não tinha
    nenhum valor universalmente correto pra inferir): toda
    sangria/suprimento já registrado ATÉ HOJE só existe em dinheiro
    (era a única forma suportada antes desta migration), então
    `cash`é o valor logicamente correto pra qualquer linha antiga, não
    só um placeholder.

  - `orders.order_number`: nullable -> backfill (numeração sequencial
    por organização, ordenada por `created_at`, via `ROW_NUMBER()`) ->
    validação -> NOT NULL + UNIQUE(organization_id, order_number).
    Mesmo caminho de 5 passos usado na 0014 pra `payments.cash_register_id`.

  - `order_items.service_name`/`professional_name`: nullable ->
    backfill via JOIN com `services`/`professionals` (nome ATUAL — é a
    melhor informação disponível pra uma linha que nunca teve
    snapshot; a partir desta migration todo item NOVO já nasce com o
    nome congelado no momento da venda, então isto só afeta o dado
    legado) -> validação -> NOT NULL.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_BRAZILIAN_STATES = [
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB",
    "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
]


def upgrade() -> None:
    conn = op.get_bind()

    # ---- Clientes (item 2) -------------------------------------------------
    states_sql = ", ".join(f"'{s}'" for s in _BRAZILIAN_STATES)
    op.execute(f"CREATE TYPE brazilian_state AS ENUM ({states_sql})")
    op.add_column("clients", sa.Column("cpf", sa.String(length=11), nullable=True))
    op.add_column(
        "clients",
        sa.Column("state", postgresql.ENUM(*_BRAZILIAN_STATES, name="brazilian_state", create_type=False), nullable=True),
    )
    op.add_column("clients", sa.Column("cep", sa.String(length=8), nullable=True))
    op.add_column("clients", sa.Column("city", sa.String(length=120), nullable=True))
    op.add_column("clients", sa.Column("neighborhood", sa.String(length=120), nullable=True))
    op.add_column("clients", sa.Column("address_line", sa.String(length=255), nullable=True))
    op.add_column("clients", sa.Column("complement", sa.String(length=120), nullable=True))
    op.create_index("ix_clients_org_cpf", "clients", ["organization_id", "cpf"], unique=False)

    # ---- Caixa por unidade (item 10) ---------------------------------------
    op.add_column("cash_registers", sa.Column("branch_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_cash_registers_branch_id_branches"), "cash_registers", "branches", ["branch_id"], ["id"],
        ondelete="RESTRICT",
    )

    # ---- Entradas/Saídas (item 21/22) --------------------------------------
    op.add_column("cash_movements", sa.Column("category", sa.String(length=120), nullable=True))
    op.add_column(
        "cash_movements",
        sa.Column(
            "method",
            postgresql.ENUM(
                "pix", "cash", "debit", "credit", "loyalty_card", "voucher", "barter", "transfer", "bank_slip",
                name="payment_method", create_type=False,
            ),
            nullable=False,
            server_default="cash",
        ),
    )

    # ---- Número de comanda (item 15/18) ------------------------------------
    op.add_column("orders", sa.Column("order_number", sa.BigInteger(), nullable=True))
    conn.execute(
        sa.text(
            """
            WITH numbered AS (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY organization_id ORDER BY created_at) AS rn
                FROM orders
            )
            UPDATE orders SET order_number = numbered.rn
            FROM numbered WHERE orders.id = numbered.id
            """
        )
    )
    remaining = conn.execute(sa.text("SELECT COUNT(*) FROM orders WHERE order_number IS NULL")).scalar_one()
    if remaining:
        raise RuntimeError(f"Migration 0015: sobraram {remaining} orders sem order_number após o backfill.")
    op.alter_column("orders", "order_number", nullable=False)
    op.create_unique_constraint("uq_orders_organization_order_number", "orders", ["organization_id", "order_number"])

    # ---- Snapshot de nome em OrderItem (item 16) ---------------------------
    op.add_column("order_items", sa.Column("service_name", sa.String(length=255), nullable=True))
    op.add_column("order_items", sa.Column("professional_name", sa.String(length=255), nullable=True))
    conn.execute(
        sa.text(
            """
            UPDATE order_items SET
                service_name = COALESCE(order_items.service_name, services.name),
                professional_name = COALESCE(order_items.professional_name, professionals.name)
            FROM services, professionals
            WHERE order_items.service_id = services.id AND order_items.professional_id = professionals.id
              AND (order_items.service_name IS NULL OR order_items.professional_name IS NULL)
            """
        )
    )
    remaining_items = conn.execute(
        sa.text("SELECT COUNT(*) FROM order_items WHERE service_name IS NULL OR professional_name IS NULL")
    ).scalar_one()
    if remaining_items:
        raise RuntimeError(
            f"Migration 0015: sobraram {remaining_items} order_items sem snapshot de nome após o backfill "
            "(serviço/profissional referenciado não encontrado — dado inconsistente, revisar antes de tentar de novo)."
        )
    op.alter_column("order_items", "service_name", nullable=False)
    op.alter_column("order_items", "professional_name", nullable=False)


def downgrade() -> None:
    op.alter_column("order_items", "professional_name", nullable=True)
    op.alter_column("order_items", "service_name", nullable=True)
    op.drop_column("order_items", "professional_name")
    op.drop_column("order_items", "service_name")

    op.drop_constraint("uq_orders_organization_order_number", "orders", type_="unique")
    op.drop_column("orders", "order_number")

    op.drop_column("cash_movements", "method")
    op.drop_column("cash_movements", "category")

    op.drop_constraint(op.f("fk_cash_registers_branch_id_branches"), "cash_registers", type_="foreignkey")
    op.drop_column("cash_registers", "branch_id")

    op.drop_index("ix_clients_org_cpf", table_name="clients")
    op.drop_column("clients", "complement")
    op.drop_column("clients", "address_line")
    op.drop_column("clients", "neighborhood")
    op.drop_column("clients", "city")
    op.drop_column("clients", "cep")
    op.drop_column("clients", "state")
    op.drop_column("clients", "cpf")
    op.execute("DROP TYPE IF EXISTS brazilian_state")
