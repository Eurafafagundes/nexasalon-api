from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_current_actor, get_db
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.organization import OrganizationRead
from nexasalon_api.services import organizations as organizations_service

router = APIRouter(prefix="/organization", tags=["organization"])


@router.get("", response_model=OrganizationRead, summary="Consultar a organização atual")
def get_current_organization(
    session: Session = Depends(get_db), actor: ActorContext = Depends(get_current_actor)
) -> OrganizationRead:
    """De propósito, sem `require_permission`: dado básico da própria
    organização (nome, slug, fuso, contato) que QUALQUER membro
    autenticado precisa pra a UI funcionar (branding, título da página
    etc.) — não é um dado sensível como financeiro ou configurações.
    Escrita em Organization não existe nesta etapa; quando existir,
    ganha sua própria checagem de `organization.manage`."""
    org = organizations_service.get_current_organization(session, actor.organization_id)
    return OrganizationRead.model_validate(org)
