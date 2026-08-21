"""cash register config — Financeiro > Caixa > Configurações do Caixa

Etapa H (Caixa operacional). Tabela nova, aditiva, `cash_register_configs`
— uma linha por organização, SPARSE (mesmo padrão de
`appointment_status_styles`, 0017): nasce vazia, e "sem linha" =
"operando nos defaults de fábrica" (ver
`services/cash_register_config.py::get_effective_config`). Os defaults
de cada toggle são os pedidos pela Etapa H: tudo ON, exceto "exigir
caixa aberto para criar agendamento" (nasce OFF).

RLS: mesmo padrão de `cash_registers`/`cash_movements` (0014),
`orders` (0013) e `appointment_status_styles` (0017) —
`ENABLE`+`FORCE ROW LEVEL SECURITY` e uma política `tenant_isolation`
via `app.current_org_id`. `ON DELETE CASCADE` em `organization_id`
(diferente dos outros, que usam RESTRICT): esta tabela guarda só
preferências de configuração, não histórico financeiro — não há razão
pra bloquear a exclusão de uma organização por causa dela.

Nenhuma permission nova é criada: a leitura/escrita da própria tela de
configuração reaproveita `settings.manage` (0007, já concedida a
OWNER/ADMIN). As regras de negócio derivadas (exigir caixa aberto,
bloquear dia anterior etc.) continuam avaliadas por quem já tinha
acesso ao fluxo (`finance.*` pra abrir/fechar/pagar, `agenda.create`
pra agendar) — não precisam de checagem própria.

Grant faltante que a Etapa H pede explicitamente ("permitir Recepção
abrir/fechar caixa"): RECEPTIONIST nunca tinha `finance.view`/
`finance.manage` (0007 não incluía Financeiro no conjunto padrão da
Recepção) — sem isso, o toggle `allow_receptionist_open_close` não tem
efeito nenhum (a Recepção nem chegaria a passar pelo RBAC de rota).
ADMIN já tem os dois desde 0007 (herda "tudo exceto
organization.manage").

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

_RECEPTIONIST_GRANTS = ["finance.view", "finance.manage"]


def upgrade() -> None:
    op.create_table(
        "cash_register_configs",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("require_open_register_for_order", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "require_open_register_for_payment", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "require_open_register_for_appointment",
            sa.Boolean(), server_default=sa.text("false"), nullable=False,
        ),
        sa.Column("block_if_previous_day_open", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "require_close_previous_before_opening_today",
            sa.Boolean(), server_default=sa.text("true"), nullable=False,
        ),
        sa.Column("single_open_register_per_branch", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_admin_open_close", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("allow_receptionist_open_close", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_cash_register_configs_organization_id_organizations"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"],
            name=op.f("fk_cash_register_configs_updated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cash_register_configs")),
        sa.UniqueConstraint("organization_id", name=op.f("uq_cash_register_configs_organization_id")),
    )

    op.execute("ALTER TABLE cash_register_configs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cash_register_configs FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON cash_register_configs USING (organization_id = {_ORG_ID})")

    conn = op.get_bind()
    for key in _RECEPTIONIST_GRANTS:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = 'RECEPTIONIST' AND organization_id IS NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM role_permissions rp "
            "  JOIN roles r ON r.id = rp.role_id "
            "  WHERE r.name = 'RECEPTIONIST' AND r.organization_id IS NULL AND rp.permission_key = %s"
            ")",
            (key, key),
        )


def downgrade() -> None:
    conn = op.get_bind()
    for key in _RECEPTIONIST_GRANTS:
        conn.exec_driver_sql(
            "DELETE FROM role_permissions WHERE permission_key = %s AND role_id IN ("
            "  SELECT id FROM roles WHERE name = 'RECEPTIONIST' AND organization_id IS NULL"
            ")",
            (key,),
        )

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON cash_register_configs")
    op.drop_table("cash_register_configs")
