import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.professional import ScheduleBlock
from nexasalon_api.repositories import branch_repo, professional_repo, schedule_block_repo
from nexasalon_api.schemas.schedule_block import ScheduleBlockCreate


def _assert_professional_in_org(session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID) -> None:
    if professional_repo.get(session, organization_id, professional_id) is None:
        raise ValidationDomainError("professional_id não pertence a esta organização (ou não existe).")


def _assert_branch_in_org(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> None:
    if not branch_repo.exists(session, organization_id, branch_id):
        raise ValidationDomainError("branch_id não pertence a esta organização.")


def create_schedule_block(
    session: Session, organization_id: uuid.UUID, data: ScheduleBlockCreate
) -> ScheduleBlock:
    if data.professional_id is not None:
        _assert_professional_in_org(session, organization_id, data.professional_id)
    if data.branch_id is not None:
        _assert_branch_in_org(session, organization_id, data.branch_id)
    return schedule_block_repo.create(
        session,
        organization_id,
        scope=data.scope,
        professional_id=data.professional_id,
        branch_id=data.branch_id,
        block_type=data.block_type,
        title=data.title,
        start_at=data.start_at,
        end_at=data.end_at,
    )


def list_schedule_blocks(
    session: Session,
    organization_id: uuid.UUID,
    *,
    range_start: datetime,
    range_end: datetime,
    branch_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
) -> list[ScheduleBlock]:
    return schedule_block_repo.list_for_range(
        session,
        organization_id,
        range_start=range_start,
        range_end=range_end,
        branch_id=branch_id,
        professional_id=professional_id,
    )


def get_schedule_block(session: Session, organization_id: uuid.UUID, block_id: uuid.UUID) -> ScheduleBlock:
    block = schedule_block_repo.get(session, organization_id, block_id)
    if block is None:
        raise NotFoundError("Bloqueio de agenda não encontrado.")
    return block


def delete_schedule_block(session: Session, organization_id: uuid.UUID, block_id: uuid.UUID) -> None:
    block = get_schedule_block(session, organization_id, block_id)
    schedule_block_repo.delete(session, block)
