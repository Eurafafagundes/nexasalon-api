"""guard RLS policies against the empty-string GUC reset value

Etapa 2D — corrige um bug real descoberto ao escrever os testes de
autenticação (não fazia parte do plano original desta etapa, mas é
consequência direta do novo padrão de sessão introduzido por
`services/auth.py`, que abre transações setando SÓ `app.current_user_id`
e sem necessariamente também setar `app.current_org_id`).

O BUG: parâmetros de sessão (GUC) customizados do Postgres, quando
setados com `set_config(name, value, true)` (equivalente a `SET LOCAL`)
pela PRIMEIRA vez numa conexão, voltam para STRING VAZIA (`''`) — não
`NULL` — assim que a transação termina (commit ou rollback). Como
conexões são reaproveitadas por um pool, uma conexão que já teve
`app.current_org_id` setado em QUALQUER transação anterior passa a
carregar `''` depois disso, para sempre (até ser setado de novo).

Toda policy RLS deste projeto fazia `current_setting(nome, true)::uuid`
direto. Enquanto `current_setting` retorna `NULL`, o cast funciona
(`NULL::uuid` = `NULL`, a policy só nega a linha). Mas quando retorna
`''`, o cast EXPLODE com `invalid input syntax for type uuid: ""` — não
nega a linha, quebra a query inteira com erro 500.

Isso já era um risco latente desde a 0003 (Etapa 2B) — só nunca
apareceu porque `get_db()`/`_ensure_dev_seed()` sempre setam
`app.current_org_id` como a PRIMEIRA instrução de toda transação, antes
de qualquer SELECT. `services/auth.py` (Etapa 2D) quebrou essa premissa
ao abrir transações que só setam `app.current_user_id` (login, seleção
de organização) — e algumas dessas queries batem em tabelas cuja policy
também referencia `app.current_org_id`.

CORREÇÃO: troca `current_setting(nome, true)::uuid` por
`NULLIF(current_setting(nome, true), '')::uuid` em TODAS as policies —
`NULLIF('', '')` vira `NULL`, e o cast de `NULL` nunca explode. Mesmo
guard que a policy de auto-acesso de `organization_memberships` (0006)
já usava para `app.current_user_id` — esta migration só faltava aplicar
o mesmo guard ao lado de `app.current_org_id`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-11
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

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

SUBQUERY_TABLES = {
    "professional_services": "professional_id IN (SELECT id FROM professionals)",
    "appointment_tags": "appointment_id IN (SELECT id FROM appointments)",
    "membership_permission_overrides": "membership_id IN (SELECT id FROM organization_memberships)",
}

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
_USER_ID = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"

_OLD_ORG_ID = "current_setting('app.current_org_id', true)::uuid"


def upgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON organizations")
    op.execute(f"CREATE POLICY tenant_isolation ON organizations USING (id = {_ORG_ID})")

    for table in DIRECT_ORG_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        if table == "organization_memberships":
            # já tinha o guard em current_user_id desde a 0006; aqui só
            # completa o guard que faltava do lado de current_org_id.
            op.execute(
                "CREATE POLICY tenant_isolation ON organization_memberships "
                f"USING (organization_id = {_ORG_ID} OR user_id = {_USER_ID})"
            )
        else:
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {_ORG_ID})"
            )

    for table, predicate in SUBQUERY_TABLES.items():
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({predicate})")

    op.execute("DROP POLICY tenant_isolation ON roles")
    op.execute(
        "CREATE POLICY tenant_isolation ON roles "
        f"USING (organization_id IS NULL OR organization_id = {_ORG_ID})"
    )

    op.execute("DROP POLICY tenant_isolation ON role_permissions")
    op.execute(
        "CREATE POLICY tenant_isolation ON role_permissions "
        "USING (role_id IN ("
        f"  SELECT id FROM roles WHERE organization_id IS NULL OR organization_id = {_ORG_ID}"
        "))"
    )

    op.execute("DROP POLICY tenant_isolation ON audit_logs")
    op.execute(f"CREATE POLICY tenant_isolation ON audit_logs USING (organization_id = {_ORG_ID})")


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON organizations")
    op.execute(f"CREATE POLICY tenant_isolation ON organizations USING (id = {_OLD_ORG_ID})")

    for table in DIRECT_ORG_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        if table == "organization_memberships":
            op.execute(
                "CREATE POLICY tenant_isolation ON organization_memberships "
                f"USING (organization_id = {_OLD_ORG_ID} OR user_id = {_USER_ID})"
            )
        else:
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {_OLD_ORG_ID})"
            )

    for table, predicate in SUBQUERY_TABLES.items():
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({predicate})")

    op.execute("DROP POLICY tenant_isolation ON roles")
    op.execute(
        "CREATE POLICY tenant_isolation ON roles "
        f"USING (organization_id IS NULL OR organization_id = {_OLD_ORG_ID})"
    )

    op.execute("DROP POLICY tenant_isolation ON role_permissions")
    op.execute(
        "CREATE POLICY tenant_isolation ON role_permissions "
        "USING (role_id IN ("
        f"  SELECT id FROM roles WHERE organization_id IS NULL OR organization_id = {_OLD_ORG_ID}"
        "))"
    )

    op.execute("DROP POLICY tenant_isolation ON audit_logs")
    op.execute(f"CREATE POLICY tenant_isolation ON audit_logs USING (organization_id = {_OLD_ORG_ID})")
