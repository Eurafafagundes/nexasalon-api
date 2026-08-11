"""row level security

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-11
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Tabelas com organization_id direto, NOT NULL: isolamento estrito por
# current_setting('app.current_org_id'). Inclui tabelas "filhas" como
# appointment_items/working_hours mesmo sendo alcançáveis via FK — RLS
# funciona melhor com o filtro na própria linha, e isso é uma segunda
# barreira caso um join da aplicação esteja errado.
DIRECT_ORG_TABLES = [
    "branches",
    "clients",
    "professionals",
    "services",
    "working_hours",
    "schedule_blocks",
    "recurrences",
    "appointments",
    "appointment_items",
    "tags",
    "organization_memberships",
]

# Tabelas de junção sem organization_id próprio: policy via subquery na
# tabela "pai" (que já tem sua própria RLS aplicada).
SUBQUERY_TABLES = {
    "professional_services": "professional_id IN (SELECT id FROM professionals)",
    "appointment_tags": "appointment_id IN (SELECT id FROM appointments)",
    "membership_permission_overrides": "membership_id IN (SELECT id FROM organization_memberships)",
}


def upgrade() -> None:
    # organizations: cada org só enxerga a própria linha.
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON organizations "
        "USING (id = current_setting('app.current_org_id', true)::uuid)"
    )

    for table in DIRECT_ORG_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (organization_id = current_setting('app.current_org_id', true)::uuid)"
        )

    for table, predicate in SUBQUERY_TABLES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({predicate})")

    # roles: organization_id NULL = role de sistema, template compartilhado
    # e visível a todas as orgs; preenchido = role customizada da própria org.
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON roles "
        "USING (organization_id IS NULL "
        "OR organization_id = current_setting('app.current_org_id', true)::uuid)"
    )

    # role_permissions: segue a mesma regra de roles (via subquery, pra não
    # duplicar organization_id numa tabela de junção pura).
    op.execute("ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE role_permissions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON role_permissions "
        "USING (role_id IN ("
        "  SELECT id FROM roles WHERE organization_id IS NULL "
        "  OR organization_id = current_setting('app.current_org_id', true)::uuid"
        "))"
    )

    # audit_logs: o INVERSO de roles — organization_id NULL aqui significa
    # ação de PLATAFORMA (não de tenant), e não deve aparecer pra nenhuma
    # organização comum, só pra um papel de superusuário/plataforma que
    # tipicamente já tem BYPASSRLS. Por isso a policy exige match exato,
    # sem aceitar NULL.
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON audit_logs "
        "USING (organization_id = current_setting('app.current_org_id', true)::uuid)"
    )

    # users e permissions ficam SEM RLS de propósito: são as únicas tabelas
    # verdadeiramente globais (identidade cross-tenant e catálogo de
    # código, respectivamente) — ver seção 7 do documento de modelagem.


def downgrade() -> None:
    all_tables = (
        ["organizations", "roles", "role_permissions", "audit_logs"]
        + DIRECT_ORG_TABLES
        + list(SUBQUERY_TABLES.keys())
    )
    for table in all_tables:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
