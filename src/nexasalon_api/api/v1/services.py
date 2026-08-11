"""Rotas do catálogo de serviços (`/api/v1/services`). Não confundir com
o pacote `nexasalon_api.services`, que é a camada de negócio — o nome
do recurso REST e o nome do pacote de aplicação coincidem por acaso do
domínio (um salão presta "serviços")."""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.repositories import professional_service_repo
from nexasalon_api.schemas.professional import ProfessionalServiceRead
from nexasalon_api.schemas.service import ServiceCreate, ServiceRead, ServiceUpdate
from nexasalon_api.services import catalog

router = APIRouter(prefix="/services", tags=["services"])

_view = require_permission("services.view")
_manage = require_permission("services.manage")


@router.get("", response_model=list[ServiceRead], summary="Listar serviços")
def list_services(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[ServiceRead]:
    services = catalog.list_services(session, actor.organization_id, include_inactive)
    return [ServiceRead.model_validate(s) for s in services]


@router.post("", response_model=ServiceRead, status_code=status.HTTP_201_CREATED, summary="Criar serviço")
def create_service(
    payload: ServiceCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceRead:
    service = catalog.create_service(session, actor.organization_id, payload)
    return ServiceRead.model_validate(service)


@router.get("/{service_id}", response_model=ServiceRead, summary="Detalhar serviço")
def get_service(
    service_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ServiceRead:
    service = catalog.get_service(session, actor.organization_id, service_id)
    return ServiceRead.model_validate(service)


@router.put("/{service_id}", response_model=ServiceRead, summary="Editar serviço")
def update_service(
    service_id: uuid.UUID,
    payload: ServiceUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceRead:
    service = catalog.update_service(session, actor.organization_id, service_id, payload)
    return ServiceRead.model_validate(service)


@router.patch("/{service_id}/activate", response_model=ServiceRead, summary="Ativar serviço")
def activate_service(
    service_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceRead:
    service = catalog.set_service_active(session, actor.organization_id, service_id, True)
    return ServiceRead.model_validate(service)


@router.patch("/{service_id}/deactivate", response_model=ServiceRead, summary="Desativar serviço")
def deactivate_service(
    service_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceRead:
    service = catalog.set_service_active(session, actor.organization_id, service_id, False)
    return ServiceRead.model_validate(service)


@router.get(
    "/{service_id}/professionals",
    response_model=list[ProfessionalServiceRead],
    summary="Listar profissionais que executam este serviço",
)
def list_service_professionals(
    service_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[ProfessionalServiceRead]:
    # confirma que o serviço é da org atual antes de listar (404 se não for/existir)
    catalog.get_service(session, actor.organization_id, service_id)
    rows = professional_service_repo.list_for_service(session, actor.organization_id, service_id)
    return [ProfessionalServiceRead.model_validate(r) for r in rows]
