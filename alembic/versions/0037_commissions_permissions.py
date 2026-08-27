"""commissions permissions (Etapa C3 - Comissoes)

Tres chaves novas, mesmo padrao de granularidade view_own/view_all ja
usado em Agenda (`agenda.view_own`/`agenda.view_all`, migration 0007):

  - `commissions.view_all`  -> visualizar producao/comissao de TODOS os
                               profissionais da organizacao (visao
                               gerencial - Etapa C3).
  - `commissions.view_own`  -> visualizar apenas a PROPRIA comissao
                               (preparada estruturalmente nesta etapa;
                               a experiencia completa do funcionario
                               fica pra Etapa C5).
  - `commissions.manage`    -> reservada para fechamento/pagamento de
                               comissao (Etapa C4) - nao usada ainda.

Concessao aos 4 roles de sistema:
  - OWNER/ADMIN: view_all + manage (mesmo padrao conservador de
    dashboard.view/0026 e clients.lookup/0030 - dado sensivel, so quem
    ja gerencia a organizacao).
  - PROFESSIONAL: view_own (unico jeito de um profissional ver a
    propria comissao, quando a experiencia da C5 existir).
  - RECEPTIONIST: nenhuma - comissao e dado quase-folha de pagamento,
    fora do escopo operacional da recepcao por padrao (organizacao
    pode conceder via role customizado, mesmo raciocinio de 0030).

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-23
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

PERMISSIONS = [
    ("commissions.view_all", "commissions", "Visualizar produção e comissão de todos os profissionais"),
    ("commissions.view_own", "commissions", "Visualizar apenas a própria produção e comissão"),
    ("commissions.manage", "commissions", "Configurar fechamento e pagamento de comissão"),
]
GRANTED_TO = {
    "OWNER": ["commissions.view_all", "commissions.manage"],
    "ADMIN": ["commissions.view_all", "commissions.manage"],
    "PROFESSIONAL": ["commissions.view_own"],
}


def upgrade() -> None:
    conn = op.get_bind()
    for key, module, description in PERMISSIONS:
        conn.exec_driver_sql(
            "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
            (key, module, description),
        )
    for role_name, keys in GRANTED_TO.items():
        for key in keys:
            conn.exec_driver_sql(
                "INSERT INTO role_permissions (role_id, permission_key) "
                "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
                (key, role_name),
            )


def downgrade() -> None:
    conn = op.get_bind()
    keys = [p[0] for p in PERMISSIONS]
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key IN (" + ",".join(["%s"] * len(keys)) + ")",
        tuple(keys),
    )
    conn.exec_driver_sql(
        "DELETE FROM permissions WHERE key IN (" + ",".join(["%s"] * len(keys)) + ")",
        tuple(keys),
    )
