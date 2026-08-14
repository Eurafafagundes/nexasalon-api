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


def create(session: Session, organization_id: uuid.UUID, **fields) -> ScheduleBlock:
    block = ScheduleBlock(organization_id=organization_id, **fields)
    session.add(block)
    session.flush()
    return block


def get(session: Session, organization_id: uuid.UUID, block_id: uuid.UUID) -> ScheduleBlock | None:
    stmt = select(ScheduleBlock).where(
        ScheduleBlock.organization_id == organization_id, ScheduleBlock.id == block_id
    )
    return session.scalars(stmt).first()


def list_for_range(
    session: Session,
    organization_id: uuid.UUID,
    *,
    range_start: datetime,
    range_end: datetime,
    branch_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
) -> list[ScheduleBlock]:
    """Bloqueios para EXIBIÇÃO na Agenda num período — mais amplo que
    `list_overlapping` (que resolve conflito pra UM profissional/unidade
    específicos, sempre os dois IDs obrigatórios). Aqui os filtros são
    OPCIONAIS porque a Agenda principal mostra VÁRIOS profissionais ao
    mesmo tempo (uma coluna por profissional): quando `professional_id`
    não é informado, ainda assim precisamos de todo bloqueio de escopo
    PROFESSIONAL (de qualquer um deles) pra render cada coluna
    corretamente — o filtro por profissional específico só faz sentido
    quando alguém pede o bloqueio de UM profissional (ex.: uma tela
    futura de agenda individual). Mesma lógica pro escopo BRANCH.
    Bloqueios de escopo ORGANIZATION sempre entram, incondicionalmente."""
    stmt = select(ScheduleBlock).where(
        ScheduleBlock.organization_id == organization_id,
        ScheduleBlock.start_at < range_end,
        ScheduleBlock.end_at > range_start,
    )
    professional_filter = (
        and_(ScheduleBlock.scope == ScheduleBlockScope.PROFESSIONAL, ScheduleBlock.professional_id == professional_id)
        if professional_id is not None
        else ScheduleBlock.scope == ScheduleBlockScope.PROFESSIONAL
    )
    branch_filter = (
        and_(ScheduleBlock.scope == ScheduleBlockScope.BRANCH, ScheduleBlock.branch_id == branch_id)
        if branch_id is not None
        else ScheduleBlock.scope == ScheduleBlockScope.BRANCH
    )
    stmt = stmt.where(
        or_(ScheduleBlock.scope == ScheduleBlockScope.ORGANIZATION, professional_filter, branch_filter)
    )
    stmt = stmt.order_by(ScheduleBlock.start_at)
    return list(session.scalars(stmt).all())


def delete(session: Session, block: ScheduleBlock) -> None:
    session.delete(block)
    session.flush()
