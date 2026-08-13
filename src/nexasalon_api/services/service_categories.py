"""Camada de negócio de `ServiceCategory` — cada organização cria e
mantém suas próprias categorias (nenhuma é fixa no código; ver
docstring do model)."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.service import ServiceCategory
from nexasalon_api.repositories import service_category_repo
from nexasalon_api.schemas.service_category import ServiceCategoryCreate, ServiceCategoryUpdate


def list_categories(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[ServiceCategory]:
    return service_category_repo.list_all(session, organization_id, include_inactive)


def get_category(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> ServiceCategory:
    category = service_category_repo.get(session, organization_id, category_id)
    if category is None:
        raise NotFoundError("Categoria de serviço não encontrada.")
    return category


def create_category(
    session: Session, organization_id: uuid.UUID, data: ServiceCategoryCreate
) -> ServiceCategory:
    return service_category_repo.create(session, organization_id, **data.model_dump())


def update_category(
    session: Session, organization_id: uuid.UUID, category_id: uuid.UUID, data: ServiceCategoryUpdate
) -> ServiceCategory:
    category = get_category(session, organization_id, category_id)
    for field, value in data.model_dump().items():
        setattr(category, field, value)
    return service_category_repo.save(session, category)


def set_category_active(
    session: Session, organization_id: uuid.UUID, category_id: uuid.UUID, is_active: bool
) -> ServiceCategory:
    """Desativar não apaga a categoria nem desvincula os `Service` que a
    referenciam (FK é SET NULL só quando a linha é de fato deletada)."""
    category = get_category(session, organization_id, category_id)
    category.is_active = is_active
    return service_category_repo.save(session, category)
