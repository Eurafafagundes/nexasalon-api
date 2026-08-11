import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.organization import Organization
from nexasalon_api.repositories import organization_repo


def get_current_organization(session: Session, organization_id: uuid.UUID) -> Organization:
    org = organization_repo.get(session, organization_id)
    if org is None:
        # só acontece se o ator DEV ONLY apontar pra uma org que não existe
        # mais — não deveria ocorrer em uso normal.
        raise NotFoundError("Organização do contexto atual não encontrada.")
    return org
