"""dynamic catalog and agenda config

Revisão de domínio (pré Etapa 3B visual): garante que NENHUM
profissional/serviço/categoria/regra de agenda precise ser hardcoded
para o NexaSalon funcionar com qualquer tipo de negócio baseado em
atendimento/agendamento (salão, barbearia, estética, nail designer,
sobrancelhas, clínica de beleza, spa, studio de maquiagem etc.).

Adiciona:
  - `service_categories`: cada organização cria suas próprias categorias
    de serviço (nova tabela, RLS igual às demais tabelas diretas).
  - `services.category_id`: FK opcional para `service_categories` — o
    campo `category` (texto livre) já existente é MANTIDO por
    compatibilidade, os dois convivem.
  - `services.allow_online_booking`, `services.display_order`: mesma
    config que já existia em outras entidades (ativo/inativo, ordem).
  - `services.requires_deposit/deposit_type/deposit_value`,
    `services.buffer_before_minutes/buffer_after_minutes`: preparação de
    domínio para sinal/depósito e buffer de agenda — SEM implementar a
    lógica ainda (nenhuma rota/serviço lê estes campos por enquanto).
  - `professionals.has_schedule/show_on_main_schedule/
    allow_online_booking/display_order`: controlam exclusivamente como
    (e se) um profissional aparece na Agenda — um Professional pode
    existir sem possuir agenda própria (ex.: gerente/back-office).
    `agenda_color` já existente cobre "cor configurável" — não duplicada
    como `schedule_color`.
  - `organizations.business_type`: texto livre opcional, só metadado
    para onboarding/templates/linguagem de interface — nenhuma regra de
    negócio pode ficar condicionada a este valor.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "service_categories",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("display_order", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_service_categories_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_categories")),
        sa.UniqueConstraint("organization_id", "name", name=op.f("uq_service_categories_organization_id_name")),
    )
    op.create_index(
        "ix_service_categories_org_display_order",
        "service_categories",
        ["organization_id", "display_order"],
        unique=False,
    )

    # RLS: mesmo padrão (e mesmo guard contra GUC vazia, ver 0008) das
    # demais tabelas "diretas" com organization_id.
    op.execute("ALTER TABLE service_categories ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE service_categories FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON service_categories USING (organization_id = {_ORG_ID})")

    op.add_column("organizations", sa.Column("business_type", sa.String(length=50), nullable=True))

    op.add_column("professionals", sa.Column("has_schedule", sa.Boolean(), server_default="true", nullable=False))
    op.add_column(
        "professionals", sa.Column("show_on_main_schedule", sa.Boolean(), server_default="true", nullable=False)
    )
    op.add_column(
        "professionals", sa.Column("allow_online_booking", sa.Boolean(), server_default="true", nullable=False)
    )
    op.add_column("professionals", sa.Column("display_order", sa.SmallInteger(), server_default="0", nullable=False))
    op.create_index(
        "ix_professionals_org_display_order", "professionals", ["organization_id", "display_order"], unique=False
    )

    op.add_column("services", sa.Column("category_id", sa.UUID(), nullable=True))
    op.add_column("services", sa.Column("allow_online_booking", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("services", sa.Column("display_order", sa.SmallInteger(), server_default="0", nullable=False))
    op.add_column("services", sa.Column("requires_deposit", sa.Boolean(), server_default="false", nullable=False))
    op.add_column(
        "services",
        sa.Column(
            "deposit_type",
            # reaproveita o enum `commission_type` já criado na migration
            # 0001 — create_type=False é obrigatório aqui: sem isso o
            # Alembic tentaria `CREATE TYPE commission_type` de novo e
            # quebraria com "type already exists".
            postgresql.ENUM("percentage", "fixed", name="commission_type", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("services", sa.Column("deposit_value", sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column("services", sa.Column("buffer_before_minutes", sa.Integer(), server_default="0", nullable=False))
    op.add_column("services", sa.Column("buffer_after_minutes", sa.Integer(), server_default="0", nullable=False))
    op.create_foreign_key(
        op.f("fk_services_category_id_service_categories"),
        "services",
        "service_categories",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_services_org_display_order", "services", ["organization_id", "display_order"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_services_org_display_order", table_name="services")
    op.drop_constraint(op.f("fk_services_category_id_service_categories"), "services", type_="foreignkey")
    op.drop_column("services", "buffer_after_minutes")
    op.drop_column("services", "buffer_before_minutes")
    op.drop_column("services", "deposit_value")
    op.drop_column("services", "deposit_type")
    op.drop_column("services", "requires_deposit")
    op.drop_column("services", "display_order")
    op.drop_column("services", "allow_online_booking")
    op.drop_column("services", "category_id")

    op.drop_index("ix_professionals_org_display_order", table_name="professionals")
    op.drop_column("professionals", "display_order")
    op.drop_column("professionals", "allow_online_booking")
    op.drop_column("professionals", "show_on_main_schedule")
    op.drop_column("professionals", "has_schedule")

    op.drop_column("organizations", "business_type")

    op.execute("DROP POLICY tenant_isolation ON service_categories")
    op.drop_index("ix_service_categories_org_display_order", table_name="service_categories")
    op.drop_table("service_categories")
