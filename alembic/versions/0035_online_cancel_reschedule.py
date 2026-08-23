"""online cancel and reschedule by customer

Etapa N5 — Cancelamento e Reagendamento pelo Cliente (área pública
"Meus Agendamentos"). Três colunas novas em `organizations`:

  - `online_cancel_enabled` (bool, nasce `false`)
  - `online_reschedule_enabled` (bool, nasce `false`)
  - `online_change_min_hours` (int, nasce `24`)

Ambas as flags nascem DESLIGADAS pra organizações existentes — é uma
capacidade nova, nenhuma organização ganha autoatendimento sem o
proprietário ligar explicitamente (mesmo raciocínio de
`online_booking_enabled`, migration 0030). `online_change_min_hours`
nasce em 24h, mas só passa a valer depois que uma das flags acima for
ligada.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-23
"""
import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("online_cancel_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("online_reschedule_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("online_change_min_hours", sa.Integer(), server_default="24", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("organizations", "online_change_min_hours")
    op.drop_column("organizations", "online_reschedule_enabled")
    op.drop_column("organizations", "online_cancel_enabled")
