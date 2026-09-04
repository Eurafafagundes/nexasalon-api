"""orders.reopen permission — reabertura de comanda finalizada

Rodada "Reabertura e Cancelamento de Comandas". Permissão NOVA,
ortogonal a `orders.cancel` (já existente, migration 0025) — o pedido
explícito foi separar as duas: cancelar uma comanda ABERTA continua
`orders.cancel` (sem mudança nenhuma aqui); reabrir uma comanda
FECHADA — desfazer pagamento/estoque/comissão já registrados — é uma
operação administrativa mais sensível, então ganha permission própria.

Concessão (mesmo raciocínio conservador já usado nas migrations 0026/
0030 — "OWNER/ADMIN só", nunca RECEPTIONIST/PROFESSIONAL de fábrica):
  OWNER        -> tem (todas as permissions do catálogo, migration 0007)
  ADMIN        -> tem (mesmo padrão de "tudo exceto organization.manage")
  RECEPTIONIST -> NÃO (reabrir uma venda já recebida é decisão
                  administrativa, mesmo tendo `orders.cancel` e
                  `payments.register` de fábrica)
  PROFESSIONAL -> NÃO (nunca teve nenhuma permission de escrita/.manage)

Organizações podem conceder a um perfil customizado via
ManageAccessDrawer (override por membership), como qualquer outra
permission.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-04
"""
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_KEY = "orders.reopen"
_MODULE = "orders"
_DESCRIPTION = "Reabrir uma comanda finalizada (desfaz pagamento e efeitos financeiros)"
_GRANTED_TO = ["OWNER", "ADMIN"]


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s) "
        "ON CONFLICT (key) DO NOTHING",
        (_KEY, _MODULE, _DESCRIPTION),
    )
    for role_name in _GRANTED_TO:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM role_permissions rp "
            "  JOIN roles r ON r.id = rp.role_id "
            "  WHERE r.name = %s AND r.organization_id IS NULL AND rp.permission_key = %s"
            ")",
            (_KEY, role_name, role_name, _KEY),
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DELETE FROM role_permissions WHERE permission_key = %s", (_KEY,))
    conn.exec_driver_sql("DELETE FROM permissions WHERE key = %s", (_KEY,))
