"""Consulta da agenda (lista de AppointmentItem num período) — Etapa 3A.

Separado de `services/appointments.py` (que lida com UM Appointment de
cada vez) porque a regra de escopo aqui é sobre uma COLEÇÃO: um ator
com só `agenda.view_own` nunca deve ver itens de outro profissional,
mesmo que peça explicitamente por `professional_id` de outra pessoa —
nesse caso a resposta é uma lista vazia (o equivalente, pra uma
listagem, do padrão 404-em-vez-de-403 usado no resto da API: não
confirma nem nega a existência de agendamentos alheios)."""
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.appointment import AppointmentItem
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.models.professional import Professional
from nexasalon_api.repositories import appointment_item_repo, professional_repo

VIEW_ALL_PERMISSION = "agenda.view_all"
VIEW_OWN_PERMISSION = "agenda.view_own"


def list_agenda(
    session: Session,
    actor: ActorContext,
    *,
    date_from: datetime,
    date_to: datetime,
    branch_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    status: AppointmentStatus | None = None,
) -> list[AppointmentItem]:
    can_view_all = VIEW_ALL_PERMISSION in actor.permissions
    effective_professional_id = professional_id

    if not can_view_all:
        # a rota já garante `view_own` OU `view_all` via
        # `require_any_permission` — chegar aqui sem nenhuma das duas
        # não deveria acontecer, mas o retorno vazio é seguro de qualquer forma.
        if VIEW_OWN_PERMISSION not in actor.permissions or actor.professional_id is None:
            return []
        if professional_id is not None and professional_id != actor.professional_id:
            return []
        effective_professional_id = actor.professional_id

    return appointment_item_repo.list_agenda(
        session,
        actor.organization_id,
        date_from=date_from,
        date_to=date_to,
        branch_id=branch_id,
        professional_id=effective_professional_id,
        service_id=service_id,
        status=status,
    )


def list_schedule_columns(
    session: Session, actor: ActorContext, *, branch_id: uuid.UUID | None = None
) -> list[Professional]:
    """As colunas da Agenda PRINCIPAL — profissionais ativos com agenda
    habilitada e visível na grade principal (ver
    `professional_repo.list_schedule_columns`). Um ator só com
    `agenda.view_own` recebe, no máximo, a própria coluna (nunca as dos
    colegas) — mesma lógica anti-leak de `list_agenda`."""
    columns = professional_repo.list_schedule_columns(session, actor.organization_id, branch_id)
    if VIEW_ALL_PERMISSION in actor.permissions:
        return columns
    if VIEW_OWN_PERMISSION not in actor.permissions or actor.professional_id is None:
        return []
    return [p for p in columns if p.id == actor.professional_id]
