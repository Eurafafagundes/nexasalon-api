"""add paid status to appointment_status enum

Etapa "Agenda + Profissionais v2" — padronização dos 8 status oficiais
da Agenda (Agendado, Confirmado, Aguardando, Em Atendimento,
Finalizado, Pago, Cancelado, Faltou). Adiciona o valor `paid` (Pago) ao
enum nativo do Postgres `appointment_status`, criado em 0002. NÃO
altera nenhuma automação — a definição automática desse status pela
Comanda/Caixa é uma etapa futura, fora do escopo desta migration. A
máquina de estados (`services/appointment_state_machine.py`) passa a
permitir a transição manual `FINISHED -> PAID` no mesmo commit.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-14
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

ENUM_NAME = "appointment_status"
NEW_VALUE = "paid"
OLD_VALUES = ("scheduled", "confirmed", "waiting", "in_progress", "finished", "cancelled", "no_show")


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE só não pode ser usado na MESMA transação
    # em que o valor novo é consumido (ex.: um INSERT com status='paid'
    # logo em seguida) — não é o caso aqui, esta migration só adiciona
    # o valor, sem tocar em dados.
    op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    # Postgres não tem "DROP VALUE" pra enum — a forma padrão de
    # reverter é recriar o tipo sem o valor novo. Isto falha (por
    # design, com o erro nativo do Postgres) se alguma linha ainda
    # estiver com status='paid', porque não existe um destino óbvio e
    # seguro pra migrar esses dados automaticamente num downgrade.
    conn = op.get_bind()
    values_sql = ", ".join(f"'{v}'" for v in OLD_VALUES)
    conn.exec_driver_sql(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    conn.exec_driver_sql(f"CREATE TYPE {ENUM_NAME} AS ENUM ({values_sql})")
    conn.exec_driver_sql(
        f"ALTER TABLE appointments ALTER COLUMN status TYPE {ENUM_NAME} USING status::text::{ENUM_NAME}"
    )
    conn.exec_driver_sql(
        f"ALTER TABLE appointment_items ALTER COLUMN status TYPE {ENUM_NAME} USING status::text::{ENUM_NAME}"
    )
    conn.exec_driver_sql(f"DROP TYPE {ENUM_NAME}_old")
