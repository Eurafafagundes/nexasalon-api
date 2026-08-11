import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.organization import Branch
from nexasalon_api.repositories import branch_repo
from nexasalon_api.schemas.branch import BranchCreate, BranchUpdate


def list_branches(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[Branch]:
    return branch_repo.list_all(session, organization_id, include_inactive)


def get_branch(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> Branch:
    branch = branch_repo.get(session, organization_id, branch_id)
    if branch is None:
        raise NotFoundError("Unidade não encontrada.")
    return branch


def create_branch(session: Session, organization_id: uuid.UUID, data: BranchCreate) -> Branch:
    return branch_repo.create(session, organization_id, **data.model_dump())


def update_branch(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID, data: BranchUpdate
) -> Branch:
    branch = get_branch(session, organization_id, branch_id)
    for field, value in data.model_dump().items():
        setattr(branch, field, value)
    return branch_repo.save(session, branch)


def set_branch_active(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID, is_active: bool
) -> Branch:
    """Ativa/desativa — nunca apaga a unidade nem o que está vinculado a
    ela (profissionais, agendamentos históricos etc. continuam intactos)."""
    branch = get_branch(session, organization_id, branch_id)
    branch.is_active = is_active
    return branch_repo.save(session, branch)
