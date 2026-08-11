"""seed permission catalog and system roles

Etapa 2D - Autenticacao/RBAC.

Popula o catalogo fixo de permissions e os 4 roles de sistema
(OWNER, ADMIN, RECEPTIONIST, PROFESSIONAL), todos com organization_id NULL
e is_system=true (visiveis/reutilizaveis por qualquer organizacao, conforme
a policy de roles definida na 0003). A arquitetura permanece preparada para
roles customizadas por organizacao no futuro (organization_id preenchido),
mas essa migration cria apenas os templates de sistema.

Catalogo ampliado nesta revisao (ainda dentro da Etapa 2D, antes do
commit): as rotas REST de Branches/Professionals/Services so exigiam
autenticacao, sem RBAC de verdade. Para aplicar `require_permission` nelas
com granularidade view/manage (como clients.view/clients.manage, que ja
existia), faltavam 4 chaves: professionals.view, services.view,
branches.view, branches.manage. `organization.manage` continua cobrindo
o resto dos dados da organizacao (nao ha rota de escrita em Organization
nesta etapa).

Matriz de permissoes por role:
  OWNER        -> todas as permissions do catalogo
  ADMIN        -> todas, exceto organization.manage
  RECEPTIONIST -> clients.view, clients.manage, agenda.view_all,
                  agenda.create, agenda.edit, agenda.cancel,
                  professionals.view, services.view
                  (precisa ver profissionais/servicos pra montar agenda,
                  mas nao gerencia nenhum dos dois)
  PROFESSIONAL -> agenda.view_own, agenda.edit, clients.view,
                  professionals.view, services.view
                  (mesma logica: enxerga o catalogo pra saber o que presta,
                  sem poder editar)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-11
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# (key, module, description)
PERMISSIONS = [
    ("organization.manage", "organization", "Gerenciar dados e configurações da organização"),
    ("users.manage", "users", "Gerenciar usuários, memberships e permissões"),
    ("branches.view", "branches", "Visualizar unidades/filiais"),
    ("branches.manage", "branches", "Criar, editar e desativar unidades/filiais"),
    ("professionals.view", "professionals", "Visualizar profissionais"),
    ("professionals.manage", "professionals", "Gerenciar profissionais e horários"),
    ("services.view", "services", "Visualizar catálogo de serviços"),
    ("services.manage", "services", "Gerenciar catálogo de serviços"),
    ("clients.view", "clients", "Visualizar clientes"),
    ("clients.manage", "clients", "Criar, editar e remover clientes"),
    ("agenda.view_own", "agenda", "Visualizar apenas a própria agenda"),
    ("agenda.view_all", "agenda", "Visualizar a agenda de todos os profissionais"),
    ("agenda.create", "agenda", "Criar agendamentos"),
    ("agenda.edit", "agenda", "Editar agendamentos"),
    ("agenda.cancel", "agenda", "Cancelar agendamentos"),
    ("agenda.force_overlap", "agenda", "Forçar agendamento sobreposto (exceção)"),
    ("finance.view", "finance", "Visualizar dados financeiros"),
    ("finance.manage", "finance", "Gerenciar lançamentos financeiros"),
    ("reports.view", "reports", "Visualizar relatórios"),
    ("settings.manage", "settings", "Gerenciar configurações gerais"),
]

ALL_KEYS = [p[0] for p in PERMISSIONS]

ROLES = {
    "OWNER": (
        "Acesso total à organização, incluindo configurações e faturamento",
        ALL_KEYS,
    ),
    "ADMIN": (
        "Gerencia a operação do dia a dia, exceto dados da organização em si",
        [k for k in ALL_KEYS if k != "organization.manage"],
    ),
    "RECEPTIONIST": (
        "Atendimento e agenda no balcão",
        [
            "clients.view",
            "clients.manage",
            "agenda.view_all",
            "agenda.create",
            "agenda.edit",
            "agenda.cancel",
            "professionals.view",
            "services.view",
        ],
    ),
    "PROFESSIONAL": (
        "Profissional que atende clientes e gerencia a própria agenda",
        ["agenda.view_own", "agenda.edit", "clients.view", "professionals.view", "services.view"],
    ),
}


def upgrade() -> None:
    conn = op.get_bind()

    for key, module, description in PERMISSIONS:
        conn.exec_driver_sql(
            "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
            (key, module, description),
        )

    for name, (description, _keys) in ROLES.items():
        conn.exec_driver_sql(
            "INSERT INTO roles (id, organization_id, name, description, is_system) "
            "VALUES (gen_random_uuid(), NULL, %s, %s, true)",
            (name, description),
        )

    for name, (_description, keys) in ROLES.items():
        for key in keys:
            conn.exec_driver_sql(
                "INSERT INTO role_permissions (role_id, permission_key) "
                "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
                (key, name),
            )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE role_id IN ("
        "  SELECT id FROM roles WHERE organization_id IS NULL "
        "  AND name IN ('OWNER', 'ADMIN', 'RECEPTIONIST', 'PROFESSIONAL')"
        ")"
    )
    conn.exec_driver_sql(
        "DELETE FROM roles WHERE organization_id IS NULL "
        "AND name IN ('OWNER', 'ADMIN', 'RECEPTIONIST', 'PROFESSIONAL')"
    )
    conn.exec_driver_sql(
        "DELETE FROM permissions WHERE key IN ("
        + ",".join(["%s"] * len(ALL_KEYS))
        + ")",
        tuple(ALL_KEYS),
    )
