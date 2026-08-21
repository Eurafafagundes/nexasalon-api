import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.appointment import Appointment
from nexasalon_api.models.enums import AppointmentSource, AppointmentStatus


def get(session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID) -> Appointment | None:
    # `populate_existing=True` é necessário porque este repo é chamado de
    # novo logo depois de apagar+recriar os itens de um Appointment já
    # carregado na identity map (PUT/replace) — sem isto, a coleção
    # `.items` em memória continuaria com os objetos antigos (já
    # deletados no banco), mesmo com uma query nova.
    stmt = (
        select(Appointment)
        .options(selectinload(Appointment.items))
        .where(Appointment.id == appointment_id, Appointment.organization_id == organization_id)
        .execution_options(populate_existing=True)
    )
    return session.scalars(stmt).first()


def create(
    session: Session,
    organization_id: uuid.UUID,
    *,
    branch_id: uuid.UUID,
    client_id: uuid.UUID,
    notes: str | None,
    created_by: uuid.UUID | None,
    fit_in: bool = False,
    source: AppointmentSource = AppointmentSource.INTERNAL,
) -> Appointment:
    # `created_by=None` (Etapa K — Agendamento Online público): o
    # agendamento não tem um usuário autenticado por trás, e
    # `Appointment.created_by`/`updated_by` já são nullable (FK
    # `ondelete=SET NULL`) — nenhuma mudança de schema precisou disso.
    # `source` default preserva TODOS os call sites internos existentes
    # (nenhum precisou mudar).
    appointment = Appointment(
        organization_id=organization_id,
        branch_id=branch_id,
        client_id=client_id,
        notes=notes,
        created_by=created_by,
        updated_by=created_by,
        fit_in=fit_in,
        source=source,
    )
    session.add(appointment)
    session.flush()
    return appointment


def save(session: Session, appointment: Appointment) -> Appointment:
    session.flush()
    return appointment


def list_active_for_client(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[Appointment]:
    """Agendamentos NÃO cancelados da cliente, qualquer unidade/data —
    usada por `services/appointments.py::get_related_appointments`
    (Etapa I, "Alteração de status") pra filtrar o dia operacional em
    memória. `CANCELLED` fica de fora: não faz sentido oferecer mudança
    de status em lote pra um agendamento já cancelado."""
    stmt = (
        select(Appointment)
        .options(selectinload(Appointment.items))
        .where(
            Appointment.organization_id == organization_id,
            Appointment.client_id == client_id,
            Appointment.status != AppointmentStatus.CANCELLED,
        )
    )
    return list(session.scalars(stmt).all())


def list_for_client(session: Session, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[Appointment]:
    """TODOS os agendamentos da cliente (qualquer status, inclusive
    CANCELLED/NO_SHOW) — Etapa J (Ficha 360°), aba "Histórico": timeline
    cronológica completa, diferente de `list_active_for_client` (que
    existe pra outro propósito, filtrar o dia operacional da Etapa I).
    Mais recente primeiro."""
    stmt = (
        select(Appointment)
        .options(selectinload(Appointment.items))
        .where(Appointment.organization_id == organization_id, Appointment.client_id == client_id)
        .order_by(Appointment.starts_at.desc())
    )
    return list(session.scalars(stmt).all())


def list_for_clients(session: Session, organization_id: uuid.UUID, client_ids: list[uuid.UUID]) -> list[Appointment]:
    """Mesma ideia de `list_for_client`, mas em LOTE pra vários clientes
    de uma vez — usada por `services/clients.py::list_clients_with_summary`
    (Etapa J, "Lista de clientes") pra calcular "próximo agendamento" e
    "faltas" sem 1 query por linha da lista (evita N+1)."""
    if not client_ids:
        return []
    stmt = select(Appointment).where(
        Appointment.organization_id == organization_id, Appointment.client_id.in_(client_ids)
    )
    return list(session.scalars(stmt).all())
