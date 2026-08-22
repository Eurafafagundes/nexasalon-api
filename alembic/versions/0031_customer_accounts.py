"""customer accounts (conta externa da cliente) + vínculo com Client

Etapa L, Blocos 5-8 — identidade PRÓPRIA para a cliente final do
Agendamento Online, deliberadamente separada de `User`/
`OrganizationMembership` (item explícito do pedido: "não misturar
cliente com funcionário", "cliente final não deve usar
OrganizationMembership", "não inserir cliente no RBAC interno").

`customer_accounts` é GLOBAL (cross-tenant), sem `organization_id`
próprio e SEM RLS — mesmo raciocínio já aplicado a `users` na migration
0003 ("as únicas tabelas verdadeiramente globais são a identidade
cross-tenant e o catálogo de código"): uma CustomerAccount pode
futuramente ter vínculo com mais de uma Organization (item explícito do
pedido), então a identidade em si não pode ser escopada a uma única org.
`email` é único globalmente (já normalizado para minúsculas pela camada
de serviço antes de gravar — Postgres não tem unicidade case-insensitive
nativa sem extensão adicional, então a normalização acontece em
`services/customer_accounts.py`, não aqui). `google_subject` é único
quando presente (conta via Google). `password_hash` é nullable — uma
conta só-Google nunca tem senha própria.

`customer_account_links` é a tabela de vínculo Bloco 8 pede
explicitamente: "não criar outro Client a cada Agendamento Online" —
uma CustomerAccount se vincula a NO MÁXIMO um Client por Organization
(`uq_customer_account_links_account_org`), resolvido uma única vez (na
primeira reserva) e reaproveitado em todas as reservas seguintes daquela
mesma organização. Tem `organization_id` PRÓPRIO (mesmo padrão de
`DIRECT_ORG_TABLES` da migration 0003) e por isso leva RLS igual a
qualquer outra tabela de tenant — mesmo a "ponta" que não é tenant
(`customer_account_id`) não vaza entre organizações porque a linha do
VÍNCULO em si pertence à organização.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_accounts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("google_subject", sa.String(length=255), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_accounts")),
        sa.UniqueConstraint("email", name=op.f("uq_customer_accounts_email")),
        sa.UniqueConstraint("google_subject", name=op.f("uq_customer_accounts_google_subject")),
    )

    op.create_table(
        "customer_account_links",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_account_id"],
            ["customer_accounts.id"],
            name=op.f("fk_customer_account_links_customer_account_id_customer_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_customer_account_links_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_customer_account_links_client_id_clients"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_account_links")),
        sa.UniqueConstraint(
            "customer_account_id", "organization_id", name="uq_customer_account_links_account_org"
        ),
    )
    op.create_index(
        "ix_customer_account_links_org_client",
        "customer_account_links",
        ["organization_id", "client_id"],
    )

    # `customer_accounts` fica SEM RLS (global) — mesmo padrão de `users`
    # (migration 0003). `customer_account_links` tem `organization_id`
    # próprio -> mesma policy `tenant_isolation` de qualquer tabela de
    # tenant direta.
    op.execute("ALTER TABLE customer_account_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE customer_account_links FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON customer_account_links "
        "USING (organization_id = current_setting('app.current_org_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON customer_account_links")
    op.drop_index("ix_customer_account_links_org_client", table_name="customer_account_links")
    op.drop_table("customer_account_links")
    op.drop_table("customer_accounts")
