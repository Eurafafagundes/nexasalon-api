"""appointments.fit_in — encaixe como característica do agendamento

Decisão desta rodada (evolução do Novo Agendamento — item 9 do pedido):
"Encaixe" NÃO é um status paralelo (continuamos com os 8 status oficiais
de `AppointmentStatus`) — é um booleano independente que convive com
qualquer status (`status=confirmed` + `fit_in=true` é um caso válido).

Nesta primeira versão `fit_in` é só DESCRITIVO: marcar um agendamento
como encaixe não pula nenhuma validação de disponibilidade (jornada,
`ScheduleBlock`, conflito com outro atendimento continuam bloqueando
exatamente como antes — ver `services/appointments.py`). Serve só pra
identificar/medir encaixes; "forçar" um encaixe sobre um conflito real
é um passo futuro deliberadamente fora de escopo aqui.

Seguro para banco com dados existentes (mesma regra da 0015 — nunca
assumir tabela vazia): coluna NOT NULL adicionada em UM PASSO SÓ com
`server_default='false'`, igual ao padrão já usado em
`cash_movements.method` na 0015 — não há ambiguidade nenhuma sobre o
valor correto para uma linha antiga (nenhum agendamento existente foi
criado com o conceito de encaixe, então `false` é logicamente correto
para todo o histórico, não um placeholder).

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("fit_in", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("appointments", "fit_in")
