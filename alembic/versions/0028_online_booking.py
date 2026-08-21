"""Agendamento Online público — Etapa K

Cada organização pode ativar uma página pública (`/agendar/<slug>`) para
clientes reservarem horário sem login. `slug` JÁ EXISTIA (migration
0002, único, NOT NULL) — não duplicado aqui, só passa a ser editável via
`PUT /organization`. Quatro colunas novas, todas com DEFAULT seguro (a
página nasce DESATIVADA — nenhuma organização existente passa a expor
uma página pública sem opt-in explícito):

  - online_booking_enabled (false)
  - online_booking_auto_confirm (true — confirma sozinho quando ligada;
    não tem efeito nenhum enquanto `online_booking_enabled=false`)
  - online_booking_min_lead_minutes (60)
  - online_booking_max_lead_days (60)

RLS — o problema do "ovo e da galinha": a policy `tenant_isolation` de
`organizations` (migration 0003) é `FORCE ROW LEVEL SECURITY`, então uma
busca por slug ANTES de conhecer `organization_id` sempre devolve zero
linhas (não há como já ter setado `app.current_org_id` pra uma
organização que ainda não foi resolvida). Em vez de qualquer bypass
genérico (BYPASSRLS derrotaria a RLS como segunda barreira em TODO o
resto do sistema, não só nesta busca), a solução é uma SEGUNDA policy,
estritamente `FOR SELECT` e PERMISSIVA (policies permissivas se
combinam com OR — Postgres combina múltiplas policies permissivas do
mesmo comando com OR), habilitada só quando um flag de sessão explícito
está ligado:

  CREATE POLICY public_booking_lookup ON organizations
    FOR SELECT USING (current_setting('app.public_booking_lookup', true) = 'true')

O flag só é setado por `api/deps.py::get_public_context` (rota pública,
sem actor autenticado) e por `services/organizations.py` (checagem de
unicidade de slug entre organizações, ao trocar o próprio slug) — em
AMBOS os casos, DESLIGADO de novo logo em seguida, dentro da mesma
transação, nunca deixado ligado pro resto da request. Por ser `FOR
SELECT` apenas, nunca afeta INSERT/UPDATE/DELETE em `organizations`
(que continuam só sob `tenant_isolation`) — e por ser específica desta
tabela, não afeta a RLS de NENHUMA outra tabela: assim que a
organização é resolvida pelo slug, `app.current_org_id` é setado
normalmente e o resto da request (serviços, profissionais,
disponibilidade, criação do agendamento) enxerga exatamente a mesma RLS
de tenant isolation de sempre.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-21
"""
import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("online_booking_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("online_booking_auto_confirm", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("online_booking_min_lead_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("online_booking_max_lead_days", sa.Integer(), server_default=sa.text("60"), nullable=False),
    )

    op.execute(
        "CREATE POLICY public_booking_lookup ON organizations "
        "FOR SELECT USING (current_setting('app.public_booking_lookup', true) = 'true')"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS public_booking_lookup ON organizations")

    op.drop_column("organizations", "online_booking_max_lead_days")
    op.drop_column("organizations", "online_booking_min_lead_minutes")
    op.drop_column("organizations", "online_booking_auto_confirm")
    op.drop_column("organizations", "online_booking_enabled")
