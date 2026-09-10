"""organization tax rates (Resultado disponivel - imposto provisionado)

Adiciona `organization_tax_rates`: aliquota de imposto provisionada por
organizacao, versionada por COMPETENCIA MENSAL (`competence_month`,
sempre o primeiro dia do mes). Linhas ESPARSAS: uma linha so existe
quando a aliquota MUDA — resolver a aliquota vigente numa competencia
qualquer e "o ultimo valor conhecido com competence_month <= a
competencia pedida" (heranca/vigencia, ver docstring do model em
`models/finance.py::OrganizationTaxRate`).

Isto e uma PROVISAO GERENCIAL (faturamento aplicavel x aliquota da
competencia) — nunca gera lancamento de caixa/pagamento automatico.
Alterar a aliquota atual NUNCA recalcula uma competencia passada: cada
linha e imutavel quanto ao seu proprio `competence_month`, e o service
layer (`services/tax_rates.py`) exige confirmacao explicita para editar
uma competencia ja no passado.

RLS igual as demais tabelas diretas por `organization_id` (ver 0003/
0008/0009/0013/0034/0039).

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-09
"""
import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "organization_tax_rates",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("competence_month", sa.Date(), nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_by_name", sa.String(length=255), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 100", name=op.f("ck_organization_tax_rates_tax_rate_between_0_and_100")
        ),
        sa.UniqueConstraint(
            "organization_id", "competence_month",
            name=op.f("uq_organization_tax_rates_organization_id_competence_month"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_organization_tax_rates_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name=op.f("fk_organization_tax_rates_created_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_tax_rates")),
    )
    op.execute("ALTER TABLE organization_tax_rates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_tax_rates FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON organization_tax_rates USING (organization_id = {_ORG_ID})")


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON organization_tax_rates")
    op.execute("ALTER TABLE organization_tax_rates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_tax_rates DISABLE ROW LEVEL SECURITY")
    op.drop_table("organization_tax_rates")
