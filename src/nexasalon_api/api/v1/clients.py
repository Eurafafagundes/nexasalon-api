import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.schemas.client import ClientCreate, ClientHistory, ClientRead, ClientUpdate
from nexasalon_api.schemas.order import OrderRead
from nexasalon_api.services import clients as clients_service

router = APIRouter(prefix="/clients", tags=["clients"])

_view = require_permission("clients.view")
_manage = require_permission("clients.manage")


@router.get("", response_model=list[ClientRead], summary="Listar/buscar clientes (nome ou telefone)")
def list_clients(
    search: str | None = None,
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[ClientRead]:
    clients = clients_service.list_clients(session, actor.organization_id, include_inactive, search)
    return [ClientRead.model_validate(c) for c in clients]


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED, summary="Criar cliente")
def create_client(
    payload: ClientCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    client = clients_service.create_client(session, actor.organization_id, payload)
    return ClientRead.model_validate(client)


@router.get("/{client_id}", response_model=ClientRead, summary="Detalhar cliente")
def get_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ClientRead:
    client = clients_service.get_client(session, actor.organization_id, client_id)
    return ClientRead.model_validate(client)


@router.get(
    "/{client_id}/history",
    response_model=ClientHistory,
    summary="Resumo + histórico de comandas do cliente (tudo derivado de Order)",
)
def get_client_history(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ClientHistory:
    summary = clients_service.get_client_history(session, actor.organization_id, client_id)
    return ClientHistory(
        client_since=summary.client_since,
        visits_count=summary.visits_count,
        total_spent=summary.total_spent,
        orders=[OrderRead.from_order(o) for o in summary.orders],
    )


@router.put("/{client_id}", response_model=ClientRead, summary="Editar cliente")
def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    client = clients_service.update_client(session, actor.organization_id, client_id, payload)
    return ClientRead.model_validate(client)


@router.patch("/{client_id}/activate", response_model=ClientRead, summary="Ativar cliente")
def activate_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    client = clients_service.set_client_active(session, actor.organization_id, client_id, True)
    return ClientRead.model_validate(client)


@router.patch("/{client_id}/deactivate", response_model=ClientRead, summary="Desativar cliente")
def deactivate_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    client = clients_service.set_client_active(session, actor.organization_id, client_id, False)
    return ClientRead.model_validate(client)
