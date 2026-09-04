"""RECEPTIONIST ganha orders.edit_price — 3 níveis de acesso a Comandas

Decisão de produto: Comandas passa a ter 3 níveis explícitos na UI de
Equipe e acessos — "Visualizar comandas" (`orders.view`), "Criar e
editar comandas" (`orders.view` + `orders.manage` + `orders.edit_price`)
e "Finalizar comandas e pagamentos" (os anteriores + `payments.register`,
reaproveitada — ver `api/v1/orders.py::_register_payment`, nenhuma
permission nova pra fechar/pagar).

`orders.edit_price` (editar preço/quantidade de uma linha já existente
na comanda) foi concedida só a OWNER/ADMIN desde a migration 0013 — uma
decisão conservadora da época (edição de VALOR já cobrado é sensível).
O novo nível 2 ("Criar e editar comandas") inclui explicitamente
"alterar... quantidades", e hoje só existe UM endpoint pra isso
(`PATCH /orders/{id}/items|product-items/{item_id}`, gated por
`orders.edit_price` — não há como separar "editar quantidade" de
"editar preço" na API atual). RECEPTIONIST já tinha `orders.view`/
`orders.manage`/`payments.register` (0013) e `finance.view`/
`finance.manage` (0027) — faltava só esta pra completar os 3 níveis,
exatamente como o perfil padrão Recepcionista precisa ter.

Nenhuma permission nova (reaproveita a chave já existente desde 0013)
— só um grant a mais pro role de sistema RECEPTIONIST. Idempotente
(`NOT EXISTS`), mesmo padrão de grant da migration 0027.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-02
"""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

_KEY = "orders.edit_price"
_ROLE = "RECEPTIONIST"


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO role_permissions (role_id, permission_key) "
        "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM role_permissions rp "
        "  JOIN roles r ON r.id = rp.role_id "
        "  WHERE r.name = %s AND r.organization_id IS NULL AND rp.permission_key = %s"
        ")",
        (_KEY, _ROLE, _ROLE, _KEY),
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key = %s AND role_id IN ("
        "  SELECT id FROM roles WHERE name = %s AND organization_id IS NULL"
        ")",
        (_KEY, _ROLE),
    )
