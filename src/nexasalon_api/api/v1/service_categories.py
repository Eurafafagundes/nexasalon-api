"""Rotas de categorias de serviço (`/api/v1/service-categories`).

Reaproveita as permissions `services.view`/`services.manage` já
existentes (não criamos `service_categories.view`/`.manage` novas) —
categoria é parte do catálogo de serviços, mesma fronteira de
autorização que o resto do catálogo já usa. Nenhuma mudança em RBAC/
seed de permissões foi necessária."""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.service_category import (
    ServiceCategoryCreate,
    ServiceCategoryRead,
    ServiceCategoryUpdate,
)
from nexasalon_api.services import service_categories as service_categories_service

router = APIRouter(prefix="/service-categories", tags=["service-categories"])

_view = require_permission("services.view")
_manage = require_permission("services.manage")


@router.get("", response_model=list[ServiceCategoryRead], summary="Listar categorias de serviço")
def list_service_categories(
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[ServiceCategoryRead]:
    categories = service_categories_service.list_categories(session, actor.organization_id, include_inactive)
    return [ServiceCategoryRead.model_validate(c) for c in categories]


@router.post(
    "", response_model=ServiceCategoryRead, status_code=status.HTTP_201_CREATED, summary="Criar categoria"
)
def create_service_category(
    payload: ServiceCategoryCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceCategoryRead:
    category = service_categories_service.create_category(session, actor.organization_id, payload)
    return ServiceCategoryRead.model_validate(category)


@router.get("/{category_id}", response_model=ServiceCategoryRead, summary="Detalhar categoria")
def get_service_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ServiceCategoryRead:
    category = service_categories_service.get_category(session, actor.organization_id, category_id)
    return ServiceCategoryRead.model_validate(category)


@router.put("/{category_id}", response_model=ServiceCategoryRead, summary="Editar categoria")
def update_service_category(
    category_id: uuid.UUID,
    payload: ServiceCategoryUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceCategoryRead:
    category = service_categories_service.update_category(session, actor.organization_id, category_id, payload)
    return ServiceCategoryRead.model_validate(category)


@router.patch("/{category_id}/activate", response_model=ServiceCategoryRead, summary="Ativar categoria")
def activate_service_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceCategoryRead:
    category = service_categories_service.set_category_active(session, actor.organization_id, category_id, True)
    return ServiceCategoryRead.model_validate(category)


@router.patch("/{category_id}/deactivate", response_model=ServiceCategoryRead, summary="Desativar categoria")
def deactivate_service_category(
    category_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ServiceCategoryRead:
    category = service_categories_service.set_category_active(session, actor.organization_id, category_id, False)
    return ServiceCategoryRead.model_validate(category)
