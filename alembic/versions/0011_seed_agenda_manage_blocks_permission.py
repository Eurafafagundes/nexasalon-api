"""seed agenda.manage_blocks permission

Etapa "Agenda + Profissionais v2". `ScheduleBlock` já existia como model
(migration original de Etapa 2A) mas nunca teve API — esta migration
acompanha a introdução das rotas `/api/v1/schedule-blocks` (criar,
listar, remover bloqueios de agenda: almoço, folga, reunião, feriado
etc.).

Reaproveita o catálogo de permissions existente (padrão de 0007): não
criamos uma permission separada de "view" porque ver bloqueios já é
parte natural de visualizar a Agenda (a rota de listagem usa
`agenda.view_all`, que RECEPTIONIST/ADMIN/OWNER já possuem). Só a
ESCRITA (criar/remover bloqueio) precisa de uma permission nova, porque
nem todo mundo que vê a agenda deve poder bloquear horários — em
especial PROFESSIONAL (que só edita a própria agenda) não deveria
poder bloquear a agenda de outros profissionais.

Concedida a OWNER, ADMIN e RECEPTIONIST (mesmo padrão de quem já
gerencia agenda: agenda.create/edit/cancel). PROFESSIONAL fica de fora
nesta revisão — pode ser revisto depois se o produto quiser que cada
profissional bloqueie a própria agenda.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-13
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

PERMISSION_KEY = "agenda.manage_blocks"
PERMISSION_MODULE = "agenda"
PERMISSION_DESCRIPTION = "Criar e remover bloqueios de agenda (folga, reunião, feriado etc.)"

GRANTED_TO = ["OWNER", "ADMIN", "RECEPTIONIST"]


def upgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql(
        "INSERT INTO permissions (key, module, description) VALUES (%s, %s, %s)",
        (PERMISSION_KEY, PERMISSION_MODULE, PERMISSION_DESCRIPTION),
    )

    for role_name in GRANTED_TO:
        conn.exec_driver_sql(
            "INSERT INTO role_permissions (role_id, permission_key) "
            "SELECT id, %s FROM roles WHERE name = %s AND organization_id IS NULL",
            (PERMISSION_KEY, role_name),
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "DELETE FROM role_permissions WHERE permission_key = %s",
        (PERMISSION_KEY,),
    )
    conn.exec_driver_sql(
        "DELETE FROM permissions WHERE key = %s",
        (PERMISSION_KEY,),
    )
