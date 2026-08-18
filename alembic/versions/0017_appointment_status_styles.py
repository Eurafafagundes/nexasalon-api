"""appointment status styles — personalização de nome/cor dos status oficiais

Item "Configurações > Status da Agenda". Tabela nova, aditiva,
`appointment_status_styles` — SEM nenhuma alteração nas 16 migrations
anteriores. Guarda só APRESENTAÇÃO (nome exibido + cor) dos 8 status
oficiais de `Appointment`, por organização; `status_code` reaproveita o
tipo Postgres já existente `appointment_status` (criado na migration
original / evoluído em 0012), não cria um enum paralelo nem uma coluna
livre — CHECK garante que só um dos 8 valores válidos entra ali (via o
próprio tipo enum do Postgres, que já rejeita qualquer outro valor).

Deliberadamente NÃO pré-semeia as 8 combinações por organização
existente: tabela nasce vazia (SPARSE), e vazio = "usa o padrão de
fábrica" tanto pro backend (nenhuma linha pra devolver) quanto pro
frontend (cai pro `APPOINTMENT_STATUS_CONFIG` local). Isso evita
escrever milhares de linhas redundantes em produção só pra representar
"não personalizado".

RLS: mesmo padrão de `cash_registers`/`cash_movements` (0014) e
`orders` (0013) — `ENABLE`+`FORCE ROW LEVEL SECURITY` e uma política
`tenant_isolation` via `app.current_org_id`. `ON DELETE RESTRICT` em
`organization_id` (não CASCADE) segue a mesma cautela dos demais
registros financeiros/administrativos do projeto — apagar uma
organização com personalização de status configurada deve falhar alto,
não apagar silenciosamente.

Nenhuma permission nova precisa ser semeada: a escrita (`PUT`/`DELETE`
das rotas) é gated por `settings.manage`, que já existe desde a
migration 0007 e já é concedida a OWNER (tudo) e ADMIN ("tudo exceto
organization.manage") — RECEPTIONIST/PROFESSIONAL continuam de fora. A
leitura (`GET`) não exige nenhuma permission especial, mesmo padrão de
`GET /organization` — é dado de UI, não sensível.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "appointment_status_styles",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "status_code",
            postgresql.ENUM(
                "scheduled", "confirmed", "waiting", "in_progress", "finished", "paid", "cancelled", "no_show",
                name="appointment_status", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=40), nullable=True),
        sa.Column("color_hex", sa.String(length=7), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "color_hex IS NULL OR color_hex ~ '^#[0-9A-Fa-f]{6}$'",
            name=op.f("ck_appointment_status_styles_color_hex_format"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_appointment_status_styles_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"],
            name=op.f("fk_appointment_status_styles_updated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointment_status_styles")),
        sa.UniqueConstraint(
            "organization_id", "status_code", name=op.f("uq_appointment_status_styles_org_status")
        ),
    )
    op.create_index(
        "ix_appointment_status_styles_organization_id", "appointment_status_styles", ["organization_id"],
        unique=False,
    )

    op.execute("ALTER TABLE appointment_status_styles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointment_status_styles FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON appointment_status_styles USING (organization_id = {_ORG_ID})")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON appointment_status_styles")
    op.drop_index("ix_appointment_status_styles_organization_id", table_name="appointment_status_styles")
    op.drop_table("appointment_status_styles")
