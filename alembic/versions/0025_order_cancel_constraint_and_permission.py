"""order cancel constraint/index + orders.cancel permission

Etapa F — segundo passo (ver docstring de 0024, que só adiciona o valor
`cancelled` ao enum `order_status`; esta migration é quem de fato o
USA, num CHECK constraint e num índice — precisa ser uma transação
separada, já com o valor do enum commitado).

Duas alterações estruturais:

  1. `ck_orders_closed_at_matches_status` passa a aceitar também
     `(status = 'cancelled' AND closed_at IS NULL)`, ao lado das duas
     combinações que já existiam (`open`/`closed`).
  2. `uq_orders_appointment_id` (UNIQUE simples em `appointment_id`)
     vira um índice único PARCIAL que ignora comandas canceladas — sem
     isso, cancelar uma comanda criada por engano deixaria o
     Appointment permanentemente sem poder abrir uma comanda nova (a
     constraint simples contaria a linha cancelada como ocupando a
     unicidade pra sempre). `services/orders.py::get_by_appointment`
     filtra `status != 'cancelled'` no mesmo espírito.

Permissão nova `orders.cancel` (nenhuma equivalente semântica já
existia — `orders.manage` é abrir/gerenciar, `orders.edit_price` é só
preço) — concedida a OWNER/ADMIN/RECEPTIONIST, mesmo conjunto de
`orders.manage` (quem pode abrir uma comanda por engano deve poder
desfazer o próprio engano).

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-21
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

OLD_CHECK_SQL = "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL)"
NEW_CHECK_SQL = (
    "(status = 'open' AND closed_at IS NULL) OR (status = 'closed' AND closed_at IS NOT NULL) "
    "OR (status = 'cancelled' AND closed_at IS NULL)"
)

PERMISSION_KEY = "orders.cancel"
PERMISSION_MODULE = "orders"
PERMISSION_DESCRIPTION = "Cancelar uma comanda aberta (sem pagamento/baixa de estoque)"
GRANTED_TO = ["OWNER", "ADMIN", "RECEPTIONIST"]


def upgrade() -> None:
    op.execute("ALTER TABLE orders DROP CONSTRAINT ck_orders_closed_at_matches_status")
    op.execute(f"ALTER TABLE orders ADD CONSTRAINT ck_orders_closed_at_matches_status CHECK ({NEW_CHECK_SQL})")

    op.execute("ALTER TABLE orders DROP CONSTRAINT uq_orders_appointment_id")
    op.execute(
        "CREATE UNIQUE INDEX uq_orders_appointment_id_active ON orders (appointment_id) "
        "WHERE status <> 'cancelled'"
    )

    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
        (PERMISSION_KEY, PERMISSION_MODULE, PERMISSION_DESCRIPTION),
    )
    for role_name in GRANTED_TO:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
            (PERMISSION_KEY, role_name),
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DELETE FROM role_permissions WHERE permission_key = %s", (PERMISSION_KEY,))
    conn.exec_driver_sql("DELETE FROM permissions WHERE key = %s", (PERMISSION_KEY,))

    # Volta pro UNIQUE simples — falha (por design) se alguma comanda
    # cancelada ainda compartilhar appointment_id com outra ativa,
    # porque não há um destino óbvio pra reconciliar isso num downgrade.
    op.execute("DROP INDEX uq_orders_appointment_id_active")
    op.execute("ALTER TABLE orders ADD CONSTRAINT uq_orders_appointment_id UNIQUE (appointment_id)")

    op.execute("ALTER TABLE orders DROP CONSTRAINT ck_orders_closed_at_matches_status")
    op.execute(f"ALTER TABLE orders ADD CONSTRAINT ck_orders_closed_at_matches_status CHECK ({OLD_CHECK_SQL})")
