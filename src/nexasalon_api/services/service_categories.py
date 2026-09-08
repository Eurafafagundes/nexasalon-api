"""Camada de negócio de `ServiceCategory` — cada organização cria e
mantém suas próprias categorias (nenhuma é fixa no código; ver
docstring do model)."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.service import ServiceCategory
from nexasalon_api.repositories import service_category_repo, service_repo
from nexasalon_api.schemas.service_category import (
    ServiceCategoryCreate,
    ServiceCategoryUpdate,
)


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


def delete_category(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID) -> None:
    """Exclusão de verdade (hard delete) — só quando a categoria NÃO tem
    nenhum serviço apontando pra ela (ativo ou inativo). A FK de
    `Service.category_id` é `ondelete="SET NULL"` — o Postgres deixaria
    apagar mesmo com serviços vinculados, só desvinculando —, mas essa
    UX (recategorizar em massa silenciosamente) nunca foi pedida; a
    regra de produto é bloquear e pedir pra mover/remover os serviços
    primeiro, então a checagem é feita aqui, na camada de negócio, não
    deixada pro comportamento do banco."""
    category = get_category(session, organization_id, category_id)
    services_count = service_repo.count_by_category(session, organization_id, category_id)
    if services_count > 0:
        raise ConflictError(
            f"Esta categoria possui {services_count} "
            f"{'serviço' if services_count == 1 else 'serviços'}. "
            "Mova ou remova os serviços antes de excluir a categoria."
        )
    service_category_repo.delete(session, category)
