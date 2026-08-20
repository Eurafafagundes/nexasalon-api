"""agenda access scope — controle granular de quais agendas cada membership pode ver/editar

Item explícito do pedido: "não quero apenas 'Pode ver Agenda: sim/não' —
quero controlar QUAIS agendas". Até aqui o único controle era
`agenda.view_own` (só a própria) vs `agenda.view_all` (todo mundo) —
grosseiro demais para "vê a própria agenda + a de um colega específico,
mas edita só a própria".

Duas peças novas, aditivas, sem alterar nenhuma das 18 migrations
anteriores:

1. Tipo `agenda_access_scope` (ALL/SELECTED) + duas colunas em
   `organization_memberships`: `agenda_view_scope`/`agenda_edit_scope`.
   Default ALL/ALL em AMBAS — toda membership já existente continua se
   comportando EXATAMENTE como antes (`agenda.view_own`/`agenda.view_all`
   continuam sendo o único portão relevante quando o escopo é ALL). Isto
   é uma restrição ADICIONAL opcional, nunca uma permissão nova.

   ALL é também, deliberadamente, a resposta ao item "aplicar acesso
   automaticamente a novas agendas": um profissional criado DEPOIS que
   uma membership foi configurada como ALL já está automaticamente
   coberto — não existe lista para manter atualizada. Só quem precisa de
   uma lista explícita (SELECTED) ganha linhas na tabela abaixo.

2. Tabela `membership_agenda_grants` — uma linha por (membership,
   professional) concedido explicitamente quando o escopo relevante é
   SELECTED, com `can_view`/`can_edit` independentes (mas `can_edit`
   exige `can_view=true`, via CHECK — "editar" é sempre um subconjunto
   de "visualizar", nunca o contrário). RLS por organização, mesmo
   padrão de `appointment_status_styles` (0017): ENABLE+FORCE +
   `tenant_isolation` via `app.current_org_id`.

Nenhuma permission nova é semeada aqui: a checagem continua vivendo em
cima de `agenda.view_own`/`agenda.view_all`/`agenda.edit` já existentes
(0007) — este é um filtro adicional de QUAIS profissionais, aplicado no
service layer (`services/agenda_access.py`), nunca um substituto do
catálogo de permissions.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_ORG_ID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("CREATE TYPE agenda_access_scope AS ENUM ('all', 'selected')")

    op.add_column(
        "organization_memberships",
        sa.Column(
            "agenda_view_scope",
            sa.Enum("all", "selected", name="agenda_access_scope", create_type=False),
            nullable=False,
            server_default="all",
        ),
    )
    op.add_column(
        "organization_memberships",
        sa.Column(
            "agenda_edit_scope",
            sa.Enum("all", "selected", name="agenda_access_scope", create_type=False),
            nullable=False,
            server_default="all",
        ),
    )

    op.create_table(
        "membership_agenda_grants",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("membership_id", sa.UUID(), nullable=False),
        sa.Column("professional_id", sa.UUID(), nullable=False),
        sa.Column("can_view", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_edit", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "can_view OR NOT can_edit", name=op.f("ck_membership_agenda_grants_edit_requires_view")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name=op.f("fk_membership_agenda_grants_organization_id_organizations"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"], ["organization_memberships.id"],
            name=op.f("fk_membership_agenda_grants_membership_id_organization_memberships"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"],
            name=op.f("fk_membership_agenda_grants_professional_id_professionals"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_membership_agenda_grants")),
        sa.UniqueConstraint(
            "membership_id", "professional_id", name=op.f("uq_membership_agenda_grants_membership_professional")
        ),
    )
    op.create_index(
        "ix_membership_agenda_grants_organization_id", "membership_agenda_grants", ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_membership_agenda_grants_membership_id", "membership_agenda_grants", ["membership_id"],
        unique=False,
    )

    op.execute("ALTER TABLE membership_agenda_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE membership_agenda_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON membership_agenda_grants USING (organization_id = {_ORG_ID})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON membership_agenda_grants")
    op.drop_index("ix_membership_agenda_grants_membership_id", table_name="membership_agenda_grants")
    op.drop_index("ix_membership_agenda_grants_organization_id", table_name="membership_agenda_grants")
    op.drop_table("membership_agenda_grants")
    op.drop_column("organization_memberships", "agenda_edit_scope")
    op.drop_column("organization_memberships", "agenda_view_scope")
    op.execute("DROP TYPE IF EXISTS agenda_access_scope")
