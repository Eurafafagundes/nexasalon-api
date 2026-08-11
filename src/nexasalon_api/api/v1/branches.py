import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.branch import BranchCreate, BranchRead, BranchUpdate
from nexasalon_api.services import branches as branches_service

router = APIRouter(prefix="/branches", tags=["branches"])

_view = require_permission("branches.view")
_manage = require_permission("branches.manage")


@router.get("", response_model=list[BranchRead], summary="Listar unidades")
def list_branches(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[BranchRead]:
    branches = branches_service.list_branches(session, actor.organization_id, include_inactive)
    return [BranchRead.model_validate(b) for b in branches]


@router.post("", response_model=BranchRead, status_code=status.HTTP_201_CREATED, summary="Criar unidade")
def create_branch(
    payload: BranchCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> BranchRead:
    branch = branches_service.create_branch(session, actor.organization_id, payload)
    return BranchRead.model_validate(branch)


@router.get("/{branch_id}", response_model=BranchRead, summary="Detalhar unidade")
def get_branch(
    branch_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> BranchRead:
    branch = branches_service.get_branch(session, actor.organization_id, branch_id)
    return BranchRead.model_validate(branch)


@router.put("/{branch_id}", response_model=BranchRead, summary="Editar unidade")
def update_branch(
    branch_id: uuid.UUID,
    payload: BranchUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> BranchRead:
    branch = branches_service.update_branch(session, actor.organization_id, branch_id, payload)
    return BranchRead.model_validate(branch)


@router.patch("/{branch_id}/activate", response_model=BranchRead, summary="Ativar unidade")
def activate_branch(
    branch_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> BranchRead:
    branch = branches_service.set_branch_active(session, actor.organization_id, branch_id, True)
    return BranchRead.model_validate(branch)


@router.patch("/{branch_id}/deactivate", response_model=BranchRead, summary="Desativar unidade")
def deactivate_branch(
    branch_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> BranchRead:
    branch = branches_service.set_branch_active(session, actor.organization_id, branch_id, False)
    return BranchRead.model_validate(branch)
