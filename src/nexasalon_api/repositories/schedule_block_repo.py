import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import ScheduleBlockScope
from nexasalon_api.models.professional import ScheduleBlock


def list_overlapping(
    session: Session,
    organization_id: uuid.UUID,
    *,
    professional_id: uuid.UUID,
    branch_id: uuid.UUID,
    range_start: datetime,
    range_end: datetime,
) -> list[ScheduleBlock]:
    """Bloqueios que afetam o profissional NESTA janela — de qualquer um
    dos três escopos possíveis: dele especificamente, da unidade onde o
    atendimento seria, ou da organização inteira (ex.: feriado)."""
    stmt = select(ScheduleBlock).where(
        ScheduleBlock.organization_id == organization_id,
        ScheduleBlock.start_at < range_end,
        ScheduleBlock.end_at > range_start,
        or_(
            and_(ScheduleBlock.scope == ScheduleBlockScope.PROFESSIONAL, ScheduleBlock.professional_id == professional_id),
            and_(ScheduleBlock.scope == ScheduleBlockScope.BRANCH, ScheduleBlock.branch_id == branch_id),
            ScheduleBlock.scope == ScheduleBlockScope.ORGANIZATION,
        ),
    )
    return list(session.scalars(stmt).all())
