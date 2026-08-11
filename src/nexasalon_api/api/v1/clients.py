import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_current_actor, get_db
from nexasalon_api.core.dev_auth import ActorContext
from nexasalon_api.schemas.client import ClientCreate, ClientRead, ClientUpdate
from nexasalon_api.services import clients as clients_service

router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("", response_model=list[ClientRead], summary="Listar/buscar clientes (nome ou telefone)")
def list_clients(
    search: str | None = None,
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> list[ClientRead]:
    clients = clients_service.list_clients(session, actor.organization_id, include_inactive, search)
    return [ClientRead.model_validate(c) for c in clients]


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED, summary="Criar cliente")
def create_client(
    payload: ClientCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ClientRead:
    client = clients_service.create_client(session, actor.organization_id, payload)
    return ClientRead.model_validate(client)


@router.get("/{client_id}", response_model=ClientRead, summary="Detalhar cliente")
def get_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ClientRead:
    client = clients_service.get_client(session, actor.organization_id, client_id)
    return ClientRead.model_validate(client)


@router.put("/{client_id}", response_model=ClientRead, summary="Editar cliente")
def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ClientRead:
    client = clients_service.update_client(session, actor.organization_id, client_id, payload)
    return ClientRead.model_validate(client)


@router.patch("/{client_id}/activate", response_model=ClientRead, summary="Ativar cliente")
def activate_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ClientRead:
    client = clients_service.set_client_active(session, actor.organization_id, client_id, True)
    return ClientRead.model_validate(client)


@router.patch("/{client_id}/deactivate", response_model=ClientRead, summary="Desativar cliente")
def deactivate_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(get_current_actor),
) -> ClientRead:
    client = clients_service.set_client_active(session, actor.organization_id, client_id, False)
    return ClientRead.model_validate(client)
