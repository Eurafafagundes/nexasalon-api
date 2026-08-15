"""orders and payments — primeira versão funcional da Comanda

Rodada "Agenda visual/status, jornada por profissional e Comanda"
(item 3). Domínio novo, verificado antes de implementar que não havia
nenhum model/API de Comanda/Order/Payment existente (só o enum
`AppointmentStatus.PAID`, já preparado desde a rodada anterior, mas sem
nada que o disparasse automaticamente).

Fluxo: Agendamento -> Atendimento (Appointment.status=finished) ->
Comanda (`Order`, itens copiados dos `AppointmentItem`, preço editável
por linha) -> Pagamento (`Payment`, um ou mais lançamentos) -> Pago
(fechar a comanda promove o Appointment pra `paid` automaticamente,
via `services/orders.py` reaproveitando `appointment_state_machine`).

Escopo deliberadamente pequeno — ver `models/order.py` para a
justificativa das 3 camadas de preço. NÃO é o Financeiro completo: sem
contas a pagar/receber, DRE, fluxo de caixa, conciliação, TEF,
comissão, fiscal/NF. `Payment` já é uma lista (preparado pra pagamento
misto, ex. Pix + Crédito), mas a versão inicial da UI só registra um
lançamento por fechamento.

Novas permissions (catálogo incremental, mesmo padrão da 0011):
  orders.view        - ver comanda de um agendamento
  orders.manage       - abrir/criar comanda a partir de um agendamento
  orders.edit_price    - editar o preço de uma linha da comanda (ação
                          sensível, ver docstring de GRANTED_EDIT_PRICE)
  payments.register   - registrar pagamento(s) e fechar a comanda

Concedidas a OWNER/ADMIN/RECEPTIONIST (mesmo padrão de quem já
gerencia a Agenda operacional — RECEPTIONIST costuma ser quem fecha o
caixa/comanda no balcão). `orders.edit_price` fica só com OWNER/ADMIN:
alterar um valor cobrado é mais sensível que só ver/abrir/registrar
pagamento, e cada edição já fica auditada (`AuditLog`, ver
`services/orders.py`). PROFESSIONAL fica de fora de tudo nesta
revisão — mesma decisão já tomada pra `agenda.manage_blocks` (0011).

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

ENUMS = {
    "order_status": ["open", "closed"],
    # Cartão Fidelidade/Voucher/Permuta adicionados antes da migration
    # ser aplicada/commitada (ajuste direto, sem nova revision) —
    # bandeira/parcelas continuam exclusivos de debit/credit (ver
    # `schemas/order.py::_CARD_METHODS`), os três novos não usam nenhum
    # dos dois.
    "payment_method": ["pix", "cash", "debit", "credit", "loyalty_card", "voucher", "barter"],
    "card_brand": ["visa", "mastercard", "elo", "amex", "hipercard", "other"],
}

DIRECT_ORG_TABLES = ["orders", "order_items", "payments"]

PERMISSIONS = [
    ("orders.view", "orders", "Visualizar a comanda de um agendamento"),
    ("orders.manage", "orders", "Abrir/criar a comanda de um agendamento"),
    ("orders.edit_price", "orders", "Editar o preço de uma linha da comanda"),
    ("payments.register", "payments", "Registrar pagamento(s) e fechar a comanda"),
]

GRANTED_DEFAULT = ["OWNER", "ADMIN", "RECEPTIONIST"]
GRANTED_EDIT_PRICE = ["OWNER", "ADMIN"]


def upgrade() -> None:
    for name, values in ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")

    op.create_table(
        "orders",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("appointment_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("open", "closed", name="order_status", create_type=False),
            server_default="open",
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("closed_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)",
            name=op.f("ck_orders_closed_at_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("fk_orders_organization_id_organizations"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["appointments.id"], name=op.f("fk_orders_appointment_id_appointments"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name=op.f("fk_orders_branch_id_branches"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_orders_client_id_clients"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_orders_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_orders_closed_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("appointment_id", name=op.f("uq_orders_appointment_id")),
    )
    op.create_index("ix_orders_client_id", "orders", ["client_id"], unique=False)
    op.create_index("ix_orders_status", "orders", ["status"], unique=False)

    op.create_table(
        "order_items",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("appointment_item_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("price >= 0", name=op.f("ck_order_items_price_not_negative")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("fk_order_items_organization_id_organizations"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["appointment_item_id"], ["appointment_items.id"], name=op.f("fk_order_items_appointment_item_id_appointment_items"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_order_items_service_id_services"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"], name=op.f("fk_order_items_professional_id_professionals"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"], unique=False)

    op.create_table(
        "payments",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column(
            "method",
            postgresql.ENUM(
                "pix", "cash", "debit", "credit", "loyalty_card", "voucher", "barter",
                name="payment_method", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "card_brand",
            postgresql.ENUM(
                "visa", "mastercard", "elo", "amex", "hipercard", "other", name="card_brand", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("installments", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name=op.f("fk_payments_organization_id_organizations"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_payments_order_id_orders"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_payments_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"], unique=False)

    for table in DIRECT_ORG_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {_ORG_ID})")

    conn = op.get_bind()
    for key, module, description in PERMISSIONS:
        conn.exec_driver_sql(
            "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
            (key, module, description),
        )

    for role_name in GRANTED_DEFAULT:
        for key in ("orders.view", "orders.manage", "payments.register"):
            conn.exec_driver_sql(
                "INSERT INTO role_permissions (role_id, permission_key) "
                "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
                (key, role_name),
            )
    for role_name in GRANTED_EDIT_PRICE:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
            ("orders.edit_price", role_name),
        )


def downgrade() -> None:
    conn = op.get_bind()
    permission_keys = [p[0] for p in PERMISSIONS]
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key = ANY(%s)", (permission_keys,)
    )
    conn.exec_driver_sql("DELETE FROM permissions WHERE key = ANY(%s)", (permission_keys,))

    for table in reversed(DIRECT_ORG_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_table("payments")
    op.drop_table("order_items")
    op.drop_table("orders")

    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
