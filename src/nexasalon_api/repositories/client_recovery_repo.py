import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.client_recovery import (
    ClientRecoveryCycle,
    ClientRecoveryEvent,
)
from nexasalon_api.models.enums import RecoveryCycleStatus

_ACTIVE_STATUSES = frozenset({RecoveryCycleStatus.PENDING, RecoveryCycleStatus.IN_PROGRESS})


def get(session: Session, organization_id: uuid.UUID, cycle_id: uuid.UUID) -> ClientRecoveryCycle | None:
    stmt = select(ClientRecoveryCycle).where(
        ClientRecoveryCycle.id == cycle_id, ClientRecoveryCycle.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def get_for_update(
    session: Session, organization_id: uuid.UUID, cycle_id: uuid.UUID
) -> ClientRecoveryCycle | None:
    """Trava a linha (`SELECT ... FOR UPDATE`) — mesmo padrão de
    `client_observation_repo.get_for_update`: serializa duas requisições
    concorrentes no MESMO ciclo, complementando (nunca substituindo) a
    checagem de `expected_version` em `services/client_recovery.py`."""
    stmt = (
        select(ClientRecoveryCycle)
        .where(ClientRecoveryCycle.id == cycle_id, ClientRecoveryCycle.organization_id == organization_id)
        .with_for_update()
    )
    return session.scalars(stmt).first()


def get_active_for_client(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID
) -> ClientRecoveryCycle | None:
    """O ciclo ATIVO (`pending`/`in_progress`) do cliente, se existir —
    nunca mais de um (índice único parcial garante isso no banco)."""
    stmt = select(ClientRecoveryCycle).where(
        ClientRecoveryCycle.organization_id == organization_id,
        ClientRecoveryCycle.client_id == client_id,
        ClientRecoveryCycle.status.in_(_ACTIVE_STATUSES),
    )
    return session.scalars(stmt).first()


def get_active_for_client_for_update(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID
) -> ClientRecoveryCycle | None:
    stmt = (
        select(ClientRecoveryCycle)
        .where(
            ClientRecoveryCycle.organization_id == organization_id,
            ClientRecoveryCycle.client_id == client_id,
            ClientRecoveryCycle.status.in_(_ACTIVE_STATUSES),
        )
        .with_for_update()
    )
    return session.scalars(stmt).first()


def get_scheduled_by_linked_appointment(
    session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID
) -> ClientRecoveryCycle | None:
    """Ciclo em `scheduled` vinculado a ESTE agendamento de retorno —
    usado pelo hook de reentrada automática (cancelamento/falta do
    agendamento vinculado, ver `services/client_recovery.py::
    reopen_cycle_if_linked_appointment_cancelled`)."""
    stmt = select(ClientRecoveryCycle).where(
        ClientRecoveryCycle.organization_id == organization_id,
        ClientRecoveryCycle.linked_appointment_id == appointment_id,
        ClientRecoveryCycle.status == RecoveryCycleStatus.SCHEDULED,
    )
    return session.scalars(stmt).first()


def list_for_clients(
    session: Session, organization_id: uuid.UUID, client_ids: list[uuid.UUID]
) -> list[ClientRecoveryCycle]:
    """Ciclos ATIVOS de vários clientes de uma vez — usado pela listagem
    em lote da Central (evita 1 query por linha)."""
    if not client_ids:
        return []
    stmt = select(ClientRecoveryCycle).where(
        ClientRecoveryCycle.organization_id == organization_id,
        ClientRecoveryCycle.client_id.in_(client_ids),
        ClientRecoveryCycle.status.in_(_ACTIVE_STATUSES),
    )
    return list(session.scalars(stmt).all())


def list_all_for_clients(
    session: Session, organization_id: uuid.UUID, client_ids: list[uuid.UUID]
) -> list[ClientRecoveryCycle]:
    """TODOS os ciclos (qualquer status) de vários clientes de uma vez —
    usado pela lógica de reentrada (`services/client_recovery.py`) pra
    decidir, por cliente, se existe um ciclo ATIVO, ou se o ciclo mais
    recente encerrado ainda bloqueia reentrada (`suppressed_until`/
    `opted_out`). Em lote, nunca 1 query por cliente."""
    if not client_ids:
        return []
    stmt = select(ClientRecoveryCycle).where(
        ClientRecoveryCycle.organization_id == organization_id,
        ClientRecoveryCycle.client_id.in_(client_ids),
    )
    return list(session.scalars(stmt).all())


def list_by_org(
    session: Session,
    organization_id: uuid.UUID,
    *,
    statuses: frozenset[RecoveryCycleStatus] | None = None,
    assigned_user_id: uuid.UUID | None = None,
) -> list[ClientRecoveryCycle]:
    """Todos os ciclos da organização (qualquer status), opcionalmente
    filtrados — usado pelas subabas Acompanhamento/Concluídos e pelo
    indicadores/summary. Sem paginação aqui (a paginação/ordenação final
    acontece em `services/client_recovery.py`, depois de combinar com
    dado de elegibilidade de clientes sem ciclo ainda)."""
    stmt = select(ClientRecoveryCycle).where(ClientRecoveryCycle.organization_id == organization_id)
    if statuses is not None:
        stmt = stmt.where(ClientRecoveryCycle.status.in_(statuses))
    if assigned_user_id is not None:
        stmt = stmt.where(ClientRecoveryCycle.assigned_user_id == assigned_user_id)
    return list(session.scalars(stmt).all())


def create(session: Session, cycle: ClientRecoveryCycle) -> ClientRecoveryCycle:
    session.add(cycle)
    session.flush()
    return cycle


def save(session: Session, cycle: ClientRecoveryCycle) -> ClientRecoveryCycle:
    session.flush()
    return cycle


def create_event(session: Session, event: ClientRecoveryEvent) -> ClientRecoveryEvent:
    session.add(event)
    session.flush()
    return event


def list_events_for_cycle(
    session: Session, organization_id: uuid.UUID, recovery_cycle_id: uuid.UUID
) -> list[ClientRecoveryEvent]:
    """Mais antigo primeiro (ordem cronológica natural) — quem quiser
    "mais recente primeiro" (painel de Histórico) inverte na camada de
    serviço/apresentação, mesmo padrão de `audit_log_repo.list_for_entity`."""
    stmt = (
        select(ClientRecoveryEvent)
        .where(
            ClientRecoveryEvent.organization_id == organization_id,
            ClientRecoveryEvent.recovery_cycle_id == recovery_cycle_id,
        )
        .order_by(ClientRecoveryEvent.created_at, ClientRecoveryEvent.id)
    )
    return list(session.scalars(stmt).all())


def list_events_for_client(
    session: Session, organization_id: uuid.UUID, client_id: uuid.UUID
) -> list[ClientRecoveryEvent]:
    """Timeline completa (todos os ciclos) de UM cliente — aba
    "Recuperação" da Ficha do Cliente. Mais antigo primeiro, mesma
    convenção de `list_events_for_cycle`."""
    stmt = (
        select(ClientRecoveryEvent)
        .where(
            ClientRecoveryEvent.organization_id == organization_id,
            ClientRecoveryEvent.client_id == client_id,
        )
        .order_by(ClientRecoveryEvent.created_at, ClientRecoveryEvent.id)
    )
    return list(session.scalars(stmt).all())


def count_by_status_in_period(
    session: Session, organization_id: uuid.UUID, start: datetime, end: datetime
) -> dict[RecoveryCycleStatus, int]:
    """Usado pelo "Recuperados"/"Sem interesse" (indicadores HISTÓRICOS,
    com período explícito, item 3 do pedido) — conta ciclos ENCERRADOS
    (`closed_at` dentro do período) por status, nunca o estoque atual.
    Implementado como `list_by_org` + contagem em Python (volume por
    organização/período é pequeno — dezenas a poucas centenas de ciclos
    encerrados por mês, nunca milhares)."""
    stmt = select(ClientRecoveryCycle.status).where(
        ClientRecoveryCycle.organization_id == organization_id,
        ClientRecoveryCycle.closed_at.is_not(None),
        ClientRecoveryCycle.closed_at >= start,
        ClientRecoveryCycle.closed_at <= end,
    )
    counts = Counter(session.scalars(stmt).all())
    return dict(counts)
