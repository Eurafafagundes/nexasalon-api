"""CPF global e opcional para usuários existentes

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-01
"""

import sqlalchemy as sa

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("cpf", sa.String(length=11), nullable=True))
    op.create_index(
        "uq_users_cpf_not_null",
        "users",
        ["cpf"],
        unique=True,
        postgresql_where=sa.text("cpf IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_cpf_not_null", table_name="users")
    op.drop_column("users", "cpf")
