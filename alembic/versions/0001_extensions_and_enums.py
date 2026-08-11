"""extensions and enum types

Revision ID: 0001
Revises:
Create Date: 2026-08-11
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ENUMS = {
    "organization_status": ["trial", "active", "suspended", "cancelled"],
    "membership_status": ["invited", "active", "suspended", "removed"],
    "commission_type": ["percentage", "fixed"],
    "schedule_block_scope": ["professional", "branch", "organization"],
    "schedule_block_type": [
        "lunch", "meeting", "day_off", "vacation", "unavailable", "maintenance", "other",
    ],
    "appointment_status": [
        "scheduled", "confirmed", "waiting", "in_progress", "finished", "cancelled", "no_show",
    ],
    "appointment_source": ["internal", "public_booking"],
    "recurrence_frequency": ["daily", "weekly", "biweekly", "monthly", "custom"],
    "recurrence_status": ["active", "paused", "cancelled"],
    "audit_action": ["create", "update", "delete"],
    "permission_effect": ["grant", "deny"],
}


def upgrade() -> None:
    # gen_random_uuid() é nativo do Postgres desde a versão 13 (não
    # depende mais da extensão pgcrypto) — é o que as PKs uuid usam
    # como server_default. Sem extensão para instalar/gerenciar.
    for name, values in ENUMS.items():
        values_sql = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")


def downgrade() -> None:
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
