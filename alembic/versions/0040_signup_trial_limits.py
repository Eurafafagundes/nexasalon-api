"""signup público: datas do trial e limite de profissionais

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-01
"""

import sqlalchemy as sa

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organizations", sa.Column("professional_limit", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "ck_organizations_professional_limit_positive",
        "organizations",
        "professional_limit IS NULL OR professional_limit > 0",
    )
    op.create_index(
        "ix_professionals_organization_id",
        "professionals",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_professionals_organization_id", table_name="professionals")
    op.drop_constraint(
        "ck_organizations_professional_limit_positive", "organizations", type_="check"
    )
    op.drop_column("organizations", "professional_limit")
    op.drop_column("organizations", "trial_ends_at")
    op.drop_column("organizations", "trial_started_at")
