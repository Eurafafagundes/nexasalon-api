"""clients.view_audit_log permission — historico de auditoria das observacoes

Rodada "Centralização de observações do cliente e auditoria completa".
Nenhuma permission existente cobre "ver o histórico de revisões de uma
observação" — `clients.view`/`clients.manage` já governam ver/editar o
CONTEÚDO atual (reaproveitados sem mudança nesta rodada), mas o painel
de Histórico (linha do tempo de edições, `GET /clients/{id}/observations/
history`) expõe METADADO adicional (quem editou quando, conteúdo
anterior) que nem todo mundo com `clients.view` precisa enxergar.

`clients.view_audit_log` é NOVA, ortogonal a `clients.view`/
`clients.manage` (mesmo espírito de `clients.view_contact_data`, 0043):
só libera o painel de Histórico das observações — nunca a visualização/
edição do conteúdo atual, que continua exigindo `clients.view`/
`clients.manage` normalmente.

Concessão (mesmo precedente de `clients.view_contact_data`, 0043 —
RECEPTIONIST já lida operacionalmente com observações de cliente no
dia a dia e legitimamente precisa investigar "quem mudou isso e
quando"; PROFESSIONAL fica de fora por padrão, mas pode ser concedida
por organização via ManageAccessDrawer como qualquer outra permission):
  OWNER        -> tem (todas as permissions do catálogo, ver 0007)
  ADMIN        -> tem
  RECEPTIONIST -> tem
  PROFESSIONAL -> NÃO tem por padrão

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-24
"""
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

_KEY = "clients.view_audit_log"
_MODULE = "clients"
_DESCRIPTION = "Ver histórico de auditoria das observações do cliente"
_GRANTED_TO = ["OWNER", "ADMIN", "RECEPTIONIST"]


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
