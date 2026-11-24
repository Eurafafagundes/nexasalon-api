import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.client_observation import ClientObservation


def get(session: Session, organization_id: uuid.UUID, observation_id: uuid.UUID) -> ClientObservation | None:
    stmt = select(ClientObservation).where(
        ClientObservation.id == observation_id, ClientObservation.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def get_for_update(
    session: Session, organization_id: uuid.UUID, observation_id: uuid.UUID
) -> ClientObservation | None:
    """Trava a linha (`SELECT ... FOR UPDATE`) durante a edição — mesmo
    padrão de `order_repo.get_for_update`: serializa duas requisições
    concorrentes na mesma observação, complementando (nunca substituindo)
    a checagem de `expected_version` em `services/client_observations.py`."""
    stmt = (
        select(ClientObservation)
        .where(ClientObservation.id == observation_id, ClientObservation.organization_id == organization_id)
        .with_for_update()
    )
    return session.scalars(stmt).first()


def list_for_client(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID
) -> list[ClientObservation]:
    stmt = select(ClientObservation).where(
        ClientObservation.organization_id == organization_id, ClientObservation.client_id == client_id
    )
    return list(session.scalars(stmt).all())


def create(session: Session, observation: ClientObservation) -> ClientObservation:
    session.add(observation)
    session.flush()
    return observation


def save(session: Session, observation: ClientObservation) -> ClientObservation:
    session.flush()
    return observation
