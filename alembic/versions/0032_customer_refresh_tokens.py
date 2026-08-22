"""customer refresh tokens (persistência segura da sessão da cliente)

Ajuste pós-Etapa L, aprovado com uma ressalva: a sessão da
`CustomerAccount` (Agendamento Online público) vivia só como um
`access_token` de 30 dias em memória no frontend — perdido a cada
reload, sem forma de revogar no backend. Este ajuste troca isso por uma
arquitetura access-token curto + refresh-token opaco rotacionado,
espelhando 1:1 a técnica já usada para FUNCIONÁRIO (`refresh_tokens`,
migration 0005) — mesmo hash SHA-256, mesma rotação a cada uso, mesma
detecção de reuso — mas numa tabela TOTALMENTE separada: nenhuma linha,
FK ou cookie é compartilhado com a sessão de staff (Bloco 7 da Etapa L:
"não misturar cliente com funcionário" continua valendo aqui).

Sem `organization_id`/`membership_id` (`CustomerAccount` é global, sem
organização própria — mesmo raciocínio de `customer_accounts`,
migration 0031). Sem Row Level Security, mesmo raciocínio de
`refresh_tokens`: segurança vem da posse do token bruto de alta entropia
(nunca armazenado, só o hash), não de isolamento por tenant — e o fluxo
de refresh precisa funcionar antes de qualquer `app.current_org_id`
estar setado.
"""
import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_refresh_tokens",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_account_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_account_id"],
            ["customer_accounts.id"],
            name=op.f("fk_customer_refresh_tokens_customer_account_id_customer_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["customer_refresh_tokens.id"],
            name=op.f("fk_customer_refresh_tokens_replaced_by_id_customer_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_refresh_tokens")),
    )
    op.create_index(
        op.f("ix_customer_refresh_tokens_token_hash"), "customer_refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        op.f("ix_customer_refresh_tokens_customer_account_id"),
        "customer_refresh_tokens",
        ["customer_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_customer_refresh_tokens_customer_account_id"), table_name="customer_refresh_tokens")
    op.drop_index(op.f("ix_customer_refresh_tokens_token_hash"), table_name="customer_refresh_tokens")
    op.drop_table("customer_refresh_tokens")
