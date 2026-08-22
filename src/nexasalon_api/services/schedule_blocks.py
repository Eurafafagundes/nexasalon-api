import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import ScheduleBlockScope
from nexasalon_api.models.professional import ScheduleBlock
from nexasalon_api.repositories import branch_repo, professional_repo, schedule_block_repo
from nexasalon_api.schemas.schedule_block import ScheduleBlockCreate

VIEW_ALL_PERMISSION = "agenda.view_all"
VIEW_OWN_PERMISSION = "agenda.view_own"


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
    actor: ActorContext,
    *,
    range_start: datetime,
    range_end: datetime,
    branch_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
) -> list[ScheduleBlock]:
    """Etapa L, Bloco 2 — escopo de VISIBILIDADE por ator, espelhando
    EXATAMENTE `services/agenda.py::list_agenda`/`filter_columns_by_scope`
    (mesma regra "escopo é sempre restrição adicional, nunca concessão").
    O bloqueio em si (linha no banco) é o MESMO pra todo mundo — o que
    muda por ator é só QUAIS profissionais ele pode enxergar; um bloqueio
    de escopo BRANCH/ORGANIZATION nunca é filtrado por profissional,
    então continua visível pra qualquer um que enxergue aquela
    unidade/organização, independente do escopo granular abaixo."""
    organization_id = actor.organization_id
    allowed_ids = actor.agenda_viewable_professional_ids

    if allowed_ids is None:
        can_view_all = VIEW_ALL_PERMISSION in actor.permissions
        if not can_view_all:
            # A rota já garante `agenda.view_own` OU `agenda.view_all`
            # via `require_any_permission` — chegar aqui sem nenhuma das
            # duas não deveria acontecer, mas o retorno vazio é seguro.
            if VIEW_OWN_PERMISSION not in actor.permissions or actor.professional_id is None:
                return []
            if professional_id is not None and professional_id != actor.professional_id:
                return []
            professional_id = actor.professional_id
        blocks = schedule_block_repo.list_for_range(
            session,
            organization_id,
            range_start=range_start,
            range_end=range_end,
            branch_id=branch_id,
            professional_id=professional_id,
        )
        return blocks

    # Escopo granular SELECTED (ver services/agenda_access.py): a lista
    # de profissionais visíveis é AUTORITATIVA, substitui a distinção
    # own/all. O repositório só filtra por UM professional_id de cada
    # vez — busca sem restrição de profissional e filtra em memória os
    # blocos de escopo PROFESSIONAL fora do conjunto permitido (blocos
    # BRANCH/ORGANIZATION nunca são cortados aqui).
    if professional_id is not None and professional_id not in allowed_ids:
        return []
    blocks = schedule_block_repo.list_for_range(
        session,
        organization_id,
        range_start=range_start,
        range_end=range_end,
        branch_id=branch_id,
        professional_id=professional_id,
    )
    return [
        b for b in blocks if b.scope != ScheduleBlockScope.PROFESSIONAL or b.professional_id in allowed_ids
    ]


def get_schedule_block(session: Session, organization_id: uuid.UUID, block_id: uuid.UUID) -> ScheduleBlock:
    block = schedule_block_repo.get(session, organization_id, block_id)
    if block is None:
        raise NotFoundError("Bloqueio de agenda não encontrado.")
    return block


def delete_schedule_block(session: Session, organization_id: uuid.UUID, block_id: uuid.UUID) -> None:
    block = get_schedule_block(session, organization_id, block_id)
    schedule_block_repo.delete(session, block)
