"""business hours and same-day online booking

Etapa M — "Agenda inteligente + Agendamento Online":

  - `business_hours`: horário de funcionamento do ESTABELECIMENTO
    (Configurações > Informações do Estabelecimento), camada SUPERIOR à
    jornada do profissional (`working_hours`) — ver docstring completa
    em `models/organization.py::BusinessHours`. Uma linha por dia da
    semana por organização; AUSÊNCIA de linhas pra uma organização =
    sem restrição nenhuma (comportamento idêntico ao de antes desta
    migration, "dados existentes precisam continuar funcionando") — a
    restrição só passa a valer depois que o proprietário salva a tela
    pela primeira vez.
  - `organizations.online_booking_same_day_enabled`: "Permitir
    agendamento para o mesmo dia" (Configurações > Agendamento Online >
    Regras de agendamento). Nasce `true` — comportamento IDÊNTICO ao de
    antes desta coluna (a antecedência mínima já existente continua
    governando sozinha até o proprietário desligar isto).

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-22
"""
import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "business_hours",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("is_open", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(is_open = false AND start_time IS NULL AND end_time IS NULL) OR "
            "(is_open = true AND start_time IS NOT NULL AND end_time IS NOT NULL AND start_time < end_time)",
            name=op.f("ck_business_hours_business_hours_open_consistency"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_business_hours_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_hours")),
        sa.UniqueConstraint("organization_id", "weekday", name=op.f("uq_business_hours_organization_id_weekday")),
    )

    # RLS: mesmo padrão das demais tabelas "diretas" com organization_id
    # (ver 0003/0008/0009 — guard contra GUC vazia já incluso em `_ORG_ID`).
    op.execute("ALTER TABLE business_hours ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE business_hours FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON business_hours USING (organization_id = {_ORG_ID})")

    op.add_column(
        "organizations",
        sa.Column("online_booking_same_day_enabled", sa.Boolean(), server_default="true", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("organizations", "online_booking_same_day_enabled")

    op.execute("DROP POLICY tenant_isolation ON business_hours")
    op.execute("ALTER TABLE business_hours NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE business_hours DISABLE ROW LEVEL SECURITY")
    op.drop_table("business_hours")
