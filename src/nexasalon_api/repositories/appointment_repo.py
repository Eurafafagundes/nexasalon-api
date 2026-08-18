import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nexasalon_api.models.appointment import Appointment


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
    created_by: uuid.UUID,
    fit_in: bool = False,
) -> Appointment:
    appointment = Appointment(
        organization_id=organization_id,
        branch_id=branch_id,
        client_id=client_id,
        notes=notes,
        created_by=created_by,
        updated_by=created_by,
        fit_in=fit_in,
    )
    session.add(appointment)
    session.flush()
    return appointment


def save(session: Session, appointment: Appointment) -> Appointment:
    session.flush()
    return appointment
