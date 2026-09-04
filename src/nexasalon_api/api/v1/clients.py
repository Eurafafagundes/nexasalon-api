import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_any_permission, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.client_privacy import apply_client_contact_masking, client_can_view_contact
from nexasalon_api.schemas.appointment import AppointmentRead
from nexasalon_api.schemas.client import (
    ClientCreate,
    ClientHistory,
    ClientListRead,
    ClientLookupRead,
    ClientProfile,
    ClientRead,
    ClientUpdate,
)
from nexasalon_api.schemas.order import ClientOrderSummary, OrderRead
from nexasalon_api.services import clients as clients_service

router = APIRouter(prefix="/clients", tags=["clients"])

_view = require_permission("clients.view")
_manage = require_permission("clients.manage")
# Etapa L, Bloco 1 — "acesso ao módulo != uso operacional do dado":
# pesquisar/selecionar cliente num fluxo permitido (Agenda/Comanda) não
# deveria exigir a permissão AMPLA de Clientes (que abre Ficha 360° e a
# listagem completa). `_lookup`/`_create_operational` aceitam a permissão
# granular OU a ampla — quem já tinha `clients.view`/`clients.manage`
# continua funcionando exatamente igual.
_lookup = require_any_permission("clients.view", "clients.lookup")
_create_operational = require_any_permission("clients.manage", "clients.create")


@router.get(
    "", response_model=list[ClientListRead], summary="Listar/buscar clientes (nome, telefone ou CPF — campo único)"
)
def list_clients(
    search: str | None = None,
    include_inactive: bool = False,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> list[ClientListRead]:
    can_view_contact = client_can_view_contact(actor.permissions)
    entries = clients_service.list_clients_with_summary(session, actor.organization_id, include_inactive, search)
    result = []
    for entry in entries:
        base = ClientListRead.model_validate(entry.client)
        enriched = base.model_copy(
            update={
                "last_visit_at": entry.last_visit_at,
                "next_appointment_at": entry.next_appointment_at,
                "last_professional_name": entry.last_professional_name,
                "has_no_show": entry.has_no_show,
            }
        )
        result.append(apply_client_contact_masking(enriched, can_view_contact=can_view_contact))
    return result


@router.post("", response_model=ClientRead, status_code=status.HTTP_201_CREATED, summary="Criar cliente")
def create_client(
    payload: ClientCreate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_create_operational),
) -> ClientRead:
    # Cadastrar (mesmo com `clients.create`/`clients.manage`) NUNCA
    # concede, sozinho, a permissão de ver dados de contato — vale até
    # pra cliente que este mesmo ator acabou de cadastrar (item
    # explícito do pedido). A resposta já sai mascarada quando o ator
    # não tem `clients.view_contact_data`, mesmo tendo acabado de
    # digitar os valores reais no formulário.
    client = clients_service.create_client(session, actor.organization_id, payload)
    can_view_contact = client_can_view_contact(actor.permissions)
    return apply_client_contact_masking(ClientRead.model_validate(client), can_view_contact=can_view_contact)


@router.get(
    "/lookup",
    response_model=list[ClientLookupRead],
    summary="Pesquisar/selecionar cliente em fluxos operacionais (Agenda/Comanda) — Etapa L, Bloco 1",
)
def lookup_clients(
    search: str | None = None,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_lookup),
) -> list[ClientLookupRead]:
    # Reaproveita INTEGRALMENTE `clients_service.list_clients`/o mesmo
    # repositório de busca já usado por `GET /clients` — só troca o
    # schema de resposta pro enxuto (nunca CPF/endereço/histórico) e a
    # permissão exigida. Sempre `include_inactive=False`: um seletor
    # operacional nunca deveria oferecer um cliente desativado. A busca
    # em si aceita o termo digitado (nome, telefone OU CPF) normalmente
    # — é só a RESPOSTA que nunca revela telefone completo sem
    # `clients.view_contact_data`; sem isso, dava pra "adivinhar" um
    # telefone completo testando prefixos até um resultado bater.
    can_view_contact = client_can_view_contact(actor.permissions)
    clients = clients_service.list_clients(session, actor.organization_id, include_inactive=False, search=search)
    return [
        apply_client_contact_masking(ClientLookupRead.model_validate(c), can_view_contact=can_view_contact)
        for c in clients
    ]


@router.get("/{client_id}", response_model=ClientRead, summary="Detalhar cliente")
def get_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ClientRead:
    client = clients_service.get_client(session, actor.organization_id, client_id)
    can_view_contact = client_can_view_contact(actor.permissions)
    return apply_client_contact_masking(ClientRead.model_validate(client), can_view_contact=can_view_contact)


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


@router.get(
    "/{client_id}/profile",
    response_model=ClientProfile,
    summary="Ficha 360° do cliente — Resumo + Histórico + Comandas (Etapa J)",
)
def get_client_profile(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> ClientProfile:
    profile = clients_service.get_client_profile(session, actor, client_id)
    can_view_contact = client_can_view_contact(actor.permissions)
    return ClientProfile(
        client=apply_client_contact_masking(ClientRead.model_validate(profile.client), can_view_contact=can_view_contact),
        client_since=profile.client_since,
        visits_count=profile.visits_count,
        total_spent=profile.total_spent if profile.can_view_finance else None,
        last_visit_at=profile.last_visit_at,
        next_appointment=AppointmentRead.model_validate(profile.next_appointment)
        if profile.next_appointment
        else None,
        no_show_count=profile.no_show_count,
        cancelled_count=profile.cancelled_count,
        timeline=[AppointmentRead.model_validate(a) for a in profile.timeline],
        orders=[ClientOrderSummary.from_order(o, can_view_finance=profile.can_view_finance) for o in profile.orders],
        can_view_finance=profile.can_view_finance,
        can_view_orders=profile.can_view_orders,
        has_online_account=profile.has_online_account,
    )


@router.put("/{client_id}", response_model=ClientRead, summary="Editar cliente")
def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    # `clients.manage` (editar) é uma permission DIFERENTE de
    # `clients.view_contact_data` — um ator com `clients.manage` mas
    # sem a permission de contato não consegue ver o telefone/CPF/
    # e-mail/endereço reais (ver GET acima), então o formulário de
    # edição no frontend também não teria como PRÉ-preencher esses
    # campos com o valor real: mostraria os placeholders mascarados
    # ("(**) *****-1234" etc.). Sem proteção aqui, salvar esse
    # formulário sem tocar nesses campos gravaria o PLACEHOLDER
    # mascarado como se fosse o dado real, corrompendo o cadastro.
    # `update_client` (services/clients.py) por isso ignora
    # completamente os campos de contato do payload quando o ator não
    # tem a permission — só nome/nascimento/gênero/observações mudam;
    # quem PRECISA corrigir um telefone precisa primeiro poder VÊ-LO.
    can_view_contact = client_can_view_contact(actor.permissions)
    client = clients_service.update_client(
        session, actor.organization_id, client_id, payload, can_view_contact=can_view_contact
    )
    return apply_client_contact_masking(ClientRead.model_validate(client), can_view_contact=can_view_contact)


@router.patch("/{client_id}/activate", response_model=ClientRead, summary="Ativar cliente")
def activate_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    client = clients_service.set_client_active(session, actor.organization_id, client_id, True)
    can_view_contact = client_can_view_contact(actor.permissions)
    return apply_client_contact_masking(ClientRead.model_validate(client), can_view_contact=can_view_contact)


@router.patch("/{client_id}/deactivate", response_model=ClientRead, summary="Desativar cliente")
def deactivate_client(
    client_id: uuid.UUID,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> ClientRead:
    client = clients_service.set_client_active(session, actor.organization_id, client_id, False)
    can_view_contact = client_can_view_contact(actor.permissions)
    return apply_client_contact_masking(ClientRead.model_validate(client), can_view_contact=can_view_contact)
