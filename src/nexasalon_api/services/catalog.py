"""Camada de negócio do catálogo de serviços (entidade `Service` do
domínio — nome escolhido de propósito pra não colidir com "camada de
serviço/aplicação")."""
import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.service import Service
from nexasalon_api.repositories import service_category_repo, service_repo
from nexasalon_api.schemas.service import ServiceCreate, ServiceUpdate


def _assert_category_in_org(session: Session, organization_id: uuid.UUID, category_id: uuid.UUID | None) -> None:
    """`category_id` (se informado) deve pertencer à mesma organização —
    mesmo padrão de `_assert_branch_in_org` em `services/professionals.py`."""
    if category_id is None:
        return
    if service_category_repo.get(session, organization_id, category_id) is None:
        raise ValidationDomainError("category_id não pertence a esta organização (ou não existe).")


def list_services(session: Session, organization_id: uuid.UUID, include_inactive: bool = False) -> list[Service]:
    return service_repo.list_all(session, organization_id, include_inactive)


def get_service(session: Session, organization_id: uuid.UUID, service_id: uuid.UUID) -> Service:
    service = service_repo.get(session, organization_id, service_id)
    if service is None:
        raise NotFoundError("Serviço não encontrado.")
    return service


def create_service(session: Session, organization_id: uuid.UUID, data: ServiceCreate) -> Service:
    _assert_category_in_org(session, organization_id, data.category_id)
    return service_repo.create(session, organization_id, **data.model_dump())


def update_service(
    session: Session, organization_id: uuid.UUID, service_id: uuid.UUID, data: ServiceUpdate
) -> Service:
    service = get_service(session, organization_id, service_id)
    _assert_category_in_org(session, organization_id, data.category_id)
    for field, value in data.model_dump().items():
        setattr(service, field, value)
    return service_repo.save(session, service)


def set_service_active(
    session: Session, organization_id: uuid.UUID, service_id: uuid.UUID, is_active: bool
) -> Service:
    """Desativar não apaga o serviço nem os AppointmentItem/
    ProfessionalService históricos que o referenciam (FK é RESTRICT)."""
    service = get_service(session, organization_id, service_id)
    service.is_active = is_active
    return service_repo.save(session, service)
