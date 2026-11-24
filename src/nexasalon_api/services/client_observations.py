import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from nexasalon_api.models.client_observation import ClientObservation
from nexasalon_api.models.enums import AuditAction
from nexasalon_api.repositories import (
    appointment_repo,
    audit_log_repo,
    client_observation_repo,
    client_repo,
    order_repo,
    user_repo,
)

# Origens suportadas pela central de Observações (item "Origem: geral,
# agendamento ou comanda") — único vocabulário usado tanto no filtro de
# listagem quanto no histórico; nunca strings soltas espalhadas pelo
# módulo.
ORIGIN_GENERAL = "general"
ORIGIN_APPOINTMENT = "appointment"
ORIGIN_ORDER = "order"
_VALID_ORIGINS = frozenset({ORIGIN_GENERAL, ORIGIN_APPOINTMENT, ORIGIN_ORDER})

# `entity_type` gravado no AuditLog para cada origem — "appointment"/
# "order" já são os mesmos valores usados por `appointments.update_notes`/
# `orders.update_observation` (histórico pré-existente dessas duas
# entidades já aparece aqui de graça, sem nenhuma migração de dado).
_ENTITY_TYPE_BY_ORIGIN = {
    ORIGIN_GENERAL: "client_observation",
    ORIGIN_APPOINTMENT: "appointment",
    ORIGIN_ORDER: "order",
}
# `entity_type="appointment"`/`"order"` recebe AuditLog de QUALQUER
# alteração dessas entidades (reagendar, trocar status, fechar comanda
# etc.) — nunca só da observação. O histórico desta feature só pode
# mostrar eventos de observação (item "não inventar descrições de
# alterações que não correspondam aos dados"), por isso filtra por
# `change_type` aqui. `client_observation` é um `entity_type` CRIADO
# nesta rodada, de uso exclusivo desta feature — não precisa filtro.
_CHANGE_TYPE_FILTER_BY_ORIGIN: dict[str, frozenset[str] | None] = {
    ORIGIN_GENERAL: None,
    ORIGIN_APPOINTMENT: frozenset({"notes_edit"}),
    ORIGIN_ORDER: frozenset({"observation_edit"}),
}


@dataclass
class ObservationCardData:
    """Forma UNIFICADA de uma observação, qualquer que seja a origem —
    ver docstring de `schemas/client_observation.py::ObservationCard`
    para o porquê de cada campo. Calculado só na LEITURA (nunca
    persistido) a partir das 3 fontes canônicas: `ClientObservation`,
    `Appointment.notes`, `Order.observation`."""

    id: uuid.UUID
    origin: str
    content: str
    version: int
    created_at: datetime | None
    created_by_name: str | None
    updated_at: datetime | None
    updated_by_name: str | None
    is_legacy_import: bool
    can_edit: bool
    can_view_history: bool
    order_number: int | None = None
    appointment_starts_at: datetime | None = None
    appointment_id: uuid.UUID | None = None

    @property
    def _sort_at(self) -> datetime | None:
        return self.updated_at or self.created_at


@dataclass
class ObservationListResult:
    items: list[ObservationCardData]
    total: int
    page: int
    page_size: int


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def list_observations(
    session: Session,
    actor: ActorContext,
    client_id: uuid.UUID,
    *,
    origin: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> ObservationListResult:
    """Agrega as 3 fontes canônicas NUM SÓ cálculo em Python (mesmo
    padrão de `services/clients.py::get_client_profile`) — nunca uma
    tabela nova duplicando o conteúdo de `Appointment.notes`/
    `Order.observation` (item explícito "não duplicar observações em
    tabelas diferentes apenas para exibi-las no perfil").

    Escopo SEMPRE limitado a UM cliente (nunca a organização inteira) —
    cardinalidade naturalmente pequena (um cliente real tem dezenas a
    poucas centenas de agendamentos/comandas, nunca milhões), por isso
    filtrar/ordenar/paginar em Python depois de carregar as 3 listas é
    seguro aqui sem precisar de um UNION SQL — diferente de "carregar
    indiscriminadamente todo o histórico da ORGANIZAÇÃO" (isso sim seria
    perigoso e é exatamente o que este desenho evita).

    RBAC por card: comandas só entram na lista quando o ator tem
    `orders.view` (MESMO gate já usado por `get_client_profile.
    can_view_orders` — nunca uma permissão nova pra isso, nunca
    amplia o que já era visível). Observações gerais/de agendamento
    seguem o gate do próprio endpoint (`clients.view`), sem restrição
    adicional — mesmo comportamento já estabelecido da aba Histórico."""
    if origin is not None and origin not in _VALID_ORIGINS:
        raise ValueError(f"origin inválido: {origin!r}")

    organization_id = actor.organization_id
    client = client_repo.get(session, organization_id, client_id)
    if client is None:
        raise NotFoundError("Cliente não encontrado.")

    can_edit_general = "clients.manage" in actor.permissions
    can_edit_appointment = "agenda.edit" in actor.permissions
    can_edit_order = "orders.manage" in actor.permissions
    can_view_orders = "orders.view" in actor.permissions
    can_view_history = "clients.view_audit_log" in actor.permissions

    cards: list[ObservationCardData] = []

    if origin in (None, ORIGIN_GENERAL):
        for obs in client_observation_repo.list_for_client(session, organization_id, client_id):
            cards.append(
                ObservationCardData(
                    id=obs.id,
                    origin=ORIGIN_GENERAL,
                    content=obs.content,
                    version=obs.version,
                    created_at=obs.created_at,
                    created_by_name=obs.created_by_name,
                    updated_at=obs.updated_at,
                    updated_by_name=obs.updated_by_name,
                    is_legacy_import=obs.is_legacy_import,
                    can_edit=can_edit_general,
                    can_view_history=can_view_history,
                )
            )

    if origin in (None, ORIGIN_APPOINTMENT):
        for appt in appointment_repo.list_for_client(session, organization_id, client_id):
            if _blank(appt.notes):
                continue
            assert appt.notes is not None
            cards.append(
                ObservationCardData(
                    id=appt.id,
                    origin=ORIGIN_APPOINTMENT,
                    content=appt.notes,
                    version=appt.notes_version,
                    created_at=None,
                    created_by_name=None,
                    updated_at=appt.notes_updated_at,
                    updated_by_name=appt.notes_updated_by_name,
                    is_legacy_import=False,
                    can_edit=can_edit_appointment,
                    can_view_history=can_view_history,
                    appointment_starts_at=appt.starts_at,
                    appointment_id=appt.id,
                )
            )

    if origin in (None, ORIGIN_ORDER) and can_view_orders:
        for order in order_repo.list_for_org(session, organization_id, client_id=client_id):
            if _blank(order.observation):
                continue
            assert order.observation is not None
            cards.append(
                ObservationCardData(
                    id=order.id,
                    origin=ORIGIN_ORDER,
                    content=order.observation,
                    version=order.observation_version,
                    created_at=None,
                    created_by_name=None,
                    updated_at=order.observation_updated_at,
                    updated_by_name=order.observation_updated_by_name,
                    is_legacy_import=False,
                    can_edit=can_edit_order,
                    can_view_history=can_view_history,
                    order_number=order.order_number,
                    appointment_id=order.appointment_id,
                )
            )

    if search:
        needle = search.strip().lower()
        if needle:
            cards = [c for c in cards if needle in c.content.lower()]

    # Mais recentes primeiro (atualização conhecida mais recente), com
    # desempate estável: cartões sem NENHUMA data conhecida (ex. geral
    # importada do legado, nunca editada) vão por último, nunca
    # misturados "aleatoriamente" entre si nem antes de um cartão com
    # data real. Uma única chave composta (nunca `.sort()` encadeado,
    # que ficaria confuso de ler e fácil de quebrar numa edição futura).
    cards.sort(
        key=lambda c: (
            c._sort_at is None,
            -(c._sort_at.timestamp()) if c._sort_at is not None else 0.0,
            c.origin,
            str(c.id),
        )
    )

    total = len(cards)
    start = (page - 1) * page_size
    page_items = cards[start : start + page_size]
    return ObservationListResult(items=page_items, total=total, page=page, page_size=page_size)


def create_general_observation(
    session: Session, actor: ActorContext, client_id: uuid.UUID, content: str
) -> ClientObservation:
    """Sempre cria um registro NOVO e independente (item "cada nova
    observação geral deve ser um registro independente" — nunca
    sobrescreve/reaproveita uma observação geral anterior)."""
    client = client_repo.get(session, actor.organization_id, client_id)
    if client is None:
        raise NotFoundError("Cliente não encontrado.")

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None
    now = datetime.now(timezone.utc)

    observation = ClientObservation(
        organization_id=actor.organization_id,
        client_id=client_id,
        content=content,
        version=0,
        is_legacy_import=False,
        created_at=now,
        created_by=actor.user_id,
        created_by_name=actor_name,
    )
    client_observation_repo.create(session, observation)

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        user_name=actor_name,
        entity_type=_ENTITY_TYPE_BY_ORIGIN[ORIGIN_GENERAL],
        entity_id=observation.id,
        action=AuditAction.CREATE,
        old_values=None,
        new_values={"content": content, "change_type": "create"},
    )
    return observation


def update_general_observation(
    session: Session,
    actor: ActorContext,
    client_id: uuid.UUID,
    observation_id: uuid.UUID,
    content: str,
    expected_version: int,
) -> ClientObservation:
    """Concorrência otimista — mesmo padrão de `orders.update_observation`:
    recusa com 409 ANTES de qualquer outra decisão (inclusive antes do
    atalho "nada mudou"), mesmo quando o texto bate por coincidência com
    o valor atual. Só gera uma revisão de auditoria quando o conteúdo
    de fato muda (item "alteração sem mudança não gera revisão")."""
    observation = client_observation_repo.get_for_update(session, actor.organization_id, observation_id)
    if observation is None or observation.client_id != client_id:
        raise NotFoundError("Observação não encontrada.")

    if expected_version != observation.version:
        raise ConflictError(
            "Esta observação foi alterada por outro usuário. Atualize o conteúdo antes de salvar novamente."
        )

    old_content = observation.content
    if content == old_content:
        return observation

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None
    now = datetime.now(timezone.utc)

    observation.content = content
    observation.version = observation.version + 1
    observation.updated_at = now
    observation.updated_by = actor.user_id
    observation.updated_by_name = actor_name
    client_observation_repo.save(session, observation)

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        user_name=actor_name,
        entity_type=_ENTITY_TYPE_BY_ORIGIN[ORIGIN_GENERAL],
        entity_id=observation.id,
        action=AuditAction.UPDATE,
        old_values={"content": old_content},
        new_values={"content": content, "change_type": "edit"},
    )
    return observation


def get_observation_history(
    session: Session, actor: ActorContext, client_id: uuid.UUID, origin: str, origin_id: uuid.UUID
):
    """Histórico completo (mais recente primeiro) de UMA observação —
    nunca aceita `origin_id` sem antes confirmar que a entidade de
    origem pertence mesmo a `client_id` (evita vazamento entre clientes/
    organizações via id adivinhado; o filtro por `organization_id` do
    próprio `AuditLog`/RLS cobre o isolamento entre organizações, este
    cobre especificamente o isolamento ENTRE CLIENTES da mesma org)."""
    if origin not in _VALID_ORIGINS:
        raise ValueError(f"origin inválido: {origin!r}")
    if "clients.view_audit_log" not in actor.permissions:
        raise ForbiddenError("Permissão 'clients.view_audit_log' é necessária para ver o histórico.")

    organization_id = actor.organization_id
    if origin == ORIGIN_GENERAL:
        observation = client_observation_repo.get(session, organization_id, origin_id)
        if observation is None or observation.client_id != client_id:
            raise NotFoundError("Observação não encontrada.")
    elif origin == ORIGIN_APPOINTMENT:
        appointment = appointment_repo.get(session, organization_id, origin_id)
        if appointment is None or appointment.client_id != client_id:
            raise NotFoundError("Agendamento não encontrado.")
    else:
        order = order_repo.get(session, organization_id, origin_id)
        if order is None or order.client_id != client_id:
            raise NotFoundError("Comanda não encontrada.")

    # `list_for_entity` devolve do mais antigo pro mais recente (ordem
    # cronológica natural do repositório, reaproveitada por outros
    # consumidores — ver docstring de `audit_log_repo.list_for_entity`).
    # O painel de Histórico quer o INVERSO ("mais recente primeiro",
    # item explícito do pedido) — inverte só aqui, nunca no repositório
    # compartilhado.
    entries = list(reversed(audit_log_repo.list_for_entity(session, organization_id, _ENTITY_TYPE_BY_ORIGIN[origin], origin_id)))
    change_types = _CHANGE_TYPE_FILTER_BY_ORIGIN[origin]
    if change_types is None:
        return entries
    return [e for e in entries if (e.new_values or {}).get("change_type") in change_types]
