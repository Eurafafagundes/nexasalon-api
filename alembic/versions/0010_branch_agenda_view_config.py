"""branch agenda view config

Etapa 3B (Agenda visual) — segunda rodada: a grade principal tinha uma
janela de horas (07:00–21:00) e granularidade (15/30 min) FIXAS no
frontend. Isso violava o mesmo princípio de dinamismo já aplicado ao
resto do domínio (ver migration 0009): apresentação da Agenda também
precisa ser configurável por unidade, não hardcoded.

Adiciona à `Branch`:
  - `agenda_view_start` / `agenda_view_end`: janela de horas desenhada
    na grade principal desta unidade.
  - `agenda_slot_minutes`: granularidade das linhas da grade (15 ou 30
    minutos — CHECK constraint garante isso no banco, não só na API).

IMPORTANTE — o que isto NÃO é: não é `WorkingHours` (que continua
sendo a única fonte de disponibilidade real de cada profissional, via
`services/availability.py`) e não afeta duração de serviço nem buffer.
É só a "moldura" visual da grade.

Defaults (`07:00`/`21:00`/`30`) são aplicados VIA `server_default`
somente para compatibilidade das unidades já cadastradas antes desta
migration — não representam uma regra de negócio, cada unidade pode
mudar livremente depois.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "branches", sa.Column("agenda_view_start", sa.Time(), server_default="07:00:00", nullable=False)
    )
    op.add_column(
        "branches", sa.Column("agenda_view_end", sa.Time(), server_default="21:00:00", nullable=False)
    )
    op.add_column(
        "branches", sa.Column("agenda_slot_minutes", sa.SmallInteger(), server_default="30", nullable=False)
    )
    op.create_check_constraint(
        "agenda_view_start_before_end", "branches", "agenda_view_start < agenda_view_end"
    )
    op.create_check_constraint(
        "agenda_slot_minutes_allowed_values", "branches", "agenda_slot_minutes IN (15, 30)"
    )


def downgrade() -> None:
    op.drop_constraint("agenda_slot_minutes_allowed_values", "branches", type_="check")
    op.drop_constraint("agenda_view_start_before_end", "branches", type_="check")
    op.drop_column("branches", "agenda_slot_minutes")
    op.drop_column("branches", "agenda_view_end")
    op.drop_column("branches", "agenda_view_start")
