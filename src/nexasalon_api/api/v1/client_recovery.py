import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.client_privacy import (
    client_can_view_contact,
    mask_phone_for_display,
)
from nexasalon_api.models.client_recovery import ClientRecoveryCycle
from nexasalon_api.models.enums import RecoveryCycleStatus
from nexasalon_api.schemas.client_recovery import (
    AssignmentUpdate,
    ContactLogCreate,
    LinkAppointmentCreate,
    RecoveryCycleRead,
    RecoveryEventRead,
    RecoveryQueueResponse,
    RecoveryQueueRowRead,
    RecoverySettingsRead,
    RecoverySettingsUpdate,
    RecoverySort,
    RecoverySubtab,
    RecoverySummaryRead,
    RecoveryTimelineResponse,
)
from nexasalon_api.services import client_recovery as client_recovery_service

# Router SEM prefixo fixo — path operations escritos por extenso. Motivo
# (item explícito "evitar colisões entre rotas estáticas e parametrizadas
# de clientes"): `GET /clients/{client_id}` já existe em `clients.router`
# e casaria com `GET /clients/recovery` se este router fosse incluído
# DEPOIS dele em `api/v1/router.py` (Starlette tenta as rotas na ordem de
# inclusão; `{client_id}` aceitaria "recovery" como valor literal). A
# correção real está na ORDEM de inclusão em `router.py` (este router
# entra ANTES de `clients.router`) — nunca confiar só na ordem dentro
# deste arquivo.
router = APIRouter(tags=["client-recovery"])

_view = require_permission("clients.recovery.view")
_manage = require_permission("clients.recovery.manage")
_finance_view = "finance.view"


def _can_view_finance(actor: ActorContext) -> bool:
    return _finance_view in actor.permissions


def _to_cycle_read(cycle: ClientRecoveryCycle) -> RecoveryCycleRead:
    return RecoveryCycleRead(
        id=cycle.id,
        status=cycle.status,
        assigned_user_id=cycle.assigned_user_id,
        assigned_user_name=cycle.assigned_user_name,
        next_contact_at=cycle.next_contact_at,
        linked_appointment_id=cycle.linked_appointment_id,
        version=cycle.version,
    )


def _to_cycle_read_optional(cycle: ClientRecoveryCycle | None) -> RecoveryCycleRead | None:
    return _to_cycle_read(cycle) if cycle is not None else None


def _to_row_read(
    session: Session, actor: ActorContext, row, *, can_view_contact: bool, can_view_finance: bool
) -> RecoveryQueueRowRead:
    phone = row.client.whatsapp or row.client.phone
    return RecoveryQueueRowRead(
        client_id=row.client.id,
        client_name=row.client.name,
        client_since=row.client.created_at,
        phone=phone if can_view_contact else mask_phone_for_display(phone),
        whatsapp_href=(
            client_recovery_service.build_whatsapp_href_for_client(session, actor, row.client)
            if can_view_contact
            else None
        ),
        last_visit_at=row.last_visit_at,
        days_since_last_visit=row.days_since_last_visit,
        last_professional_names=row.last_professional_names,
        last_service_names=row.last_service_names,
        next_appointment_at=row.next_appointment_at,
        total_spent=row.total_spent if can_view_finance else None,
        is_overdue=row.is_overdue,
        cycle=_to_cycle_read_optional(row.cycle),
    )


@router.get("/clients/recovery", response_model=RecoveryQueueResponse, summary="Central de Recuperação de Clientes")
def list_recovery_queue(
    subtab: RecoverySubtab = "pending",
    min_days: int | None = Query(default=None, ge=0),
    max_days: int | None = Query(default=None, ge=0),
    search: str | None = None,
    assigned_user_id: uuid.UUID | None = None,
    has_phone: bool | None = None,
    overdue_only: bool = False,
    concluded_status: RecoveryCycleStatus | None = None,
    last_procedure_service_id: uuid.UUID | None = None,
    last_professional_id: uuid.UUID | None = None,
    sort: RecoverySort = "days_desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> RecoveryQueueResponse:
    result = client_recovery_service.list_recovery_queue(
        session, actor, subtab=subtab, min_days=min_days, max_days=max_days, search=search,
        assigned_user_id=assigned_user_id, has_phone=has_phone, overdue_only=overdue_only,
        concluded_statuses=(frozenset({concluded_status}) if concluded_status else None),
        last_procedure_service_id=last_procedure_service_id, last_professional_id=last_professional_id,
        sort=sort, page=page, page_size=page_size,
    )
    can_view_contact = client_can_view_contact(actor.permissions)
    can_view_finance = _can_view_finance(actor)
    return RecoveryQueueResponse(
        items=[
            _to_row_read(session, actor, r, can_view_contact=can_view_contact, can_view_finance=can_view_finance)
            for r in result.items
        ],
        total=result.total, page=result.page, page_size=result.page_size,
    )


@router.get("/clients/recovery/summary", response_model=RecoverySummaryRead, summary="Indicadores da Central de Recuperação")
def get_recovery_summary(
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_view),
) -> RecoverySummaryRead:
    summary = client_recovery_service.get_recovery_summary(session, actor, date_from=date_from, date_to=date_to)
    return RecoverySummaryRead(
        pending_count=summary.pending_count, in_progress_count=summary.in_progress_count,
        recovered_count=summary.recovered_count, not_interested_count=summary.not_interested_count,
        concluded_count=summary.concluded_count,
        date_from=summary.date_from, date_to=summary.date_to,
    )


@router.get(
    "/clients/{client_id}/recovery", response_model=RecoveryTimelineResponse,
    summary="Histórico de recuperação do cliente (aba Recuperação da Ficha)",
)
def get_client_recovery_timeline(
    client_id: uuid.UUID, session: Session = Depends(get_db), actor: ActorContext = Depends(_view)
) -> RecoveryTimelineResponse:
    events = client_recovery_service.get_client_recovery_timeline(session, actor, client_id)
    return RecoveryTimelineResponse(
        items=[
            RecoveryEventRead(
                id=e.id, event_type=e.event_type.value, actor_user_name=e.actor_user_name,
                contact_channel=e.contact_channel, contact_result=e.contact_result, notes=e.notes,
                previous_status=e.previous_status, new_status=e.new_status,
                next_contact_at=e.next_contact_at, linked_appointment_id=e.linked_appointment_id,
                created_at=e.created_at,
            )
            for e in events
        ]
    )


@router.post(
    "/clients/{client_id}/recovery/contacts", response_model=RecoveryCycleRead,
    status_code=status.HTTP_201_CREATED, summary="Registrar contato de recuperação",
)
def log_contact(
    client_id: uuid.UUID, payload: ContactLogCreate,
    session: Session = Depends(get_db), actor: ActorContext = Depends(_manage),
) -> RecoveryCycleRead:
    cycle = client_recovery_service.log_contact(
        session, actor, client_id,
        contact_channel=payload.contact_channel, contact_result=payload.contact_result,
        notes=payload.notes, next_contact_at=payload.next_contact_at,
        linked_appointment_id=payload.linked_appointment_id,
    )
    return _to_cycle_read(cycle)


@router.patch(
    "/clients/{client_id}/recovery/assignment", response_model=RecoveryCycleRead,
    summary="Transferir responsável pelo ciclo de recuperação",
)
def reassign_cycle(
    client_id: uuid.UUID, payload: AssignmentUpdate,
    session: Session = Depends(get_db), actor: ActorContext = Depends(_manage),
) -> RecoveryCycleRead:
    cycle = client_recovery_service.reassign_cycle(
        session, actor, client_id,
        new_assigned_user_id=payload.new_assigned_user_id, expected_version=payload.expected_version,
    )
    return _to_cycle_read(cycle)


@router.post(
    "/clients/{client_id}/recovery/link-appointment", response_model=RecoveryCycleRead,
    summary="Vincular agendamento de retorno ao ciclo ativo",
)
def link_return_appointment(
    client_id: uuid.UUID, payload: LinkAppointmentCreate,
    session: Session = Depends(get_db), actor: ActorContext = Depends(_manage),
) -> RecoveryCycleRead:
    cycle = client_recovery_service.link_return_appointment(
        session, actor, client_id,
        appointment_id=payload.appointment_id, expected_version=payload.expected_version,
    )
    return _to_cycle_read(cycle)


@router.post(
    "/clients/{client_id}/recovery/reopen", response_model=RecoveryCycleRead,
    summary="Reabrir ciclo encerrado como telefone inválido",
)
def reopen_cycle(
    client_id: uuid.UUID, session: Session = Depends(get_db), actor: ActorContext = Depends(_manage)
) -> RecoveryCycleRead:
    cycle = client_recovery_service.reopen_cycle(session, actor, client_id)
    return _to_cycle_read(cycle)


@router.get(
    "/organization/recovery-settings", response_model=RecoverySettingsRead, summary="Configurações da Central de Recuperação",
)
def get_recovery_settings(
    session: Session = Depends(get_db), actor: ActorContext = Depends(_view)
) -> RecoverySettingsRead:
    settings = client_recovery_service.get_effective_settings(session, actor.organization_id)
    return RecoverySettingsRead(
        default_no_return_days=settings.default_no_return_days,
        default_follow_up_days=settings.default_follow_up_days,
        default_snooze_days=settings.default_snooze_days,
        whatsapp_message_template=settings.whatsapp_message_template,
    )


@router.put(
    "/organization/recovery-settings", response_model=RecoverySettingsRead, summary="Salvar configurações da Central de Recuperação",
)
def update_recovery_settings(
    payload: RecoverySettingsUpdate, session: Session = Depends(get_db), actor: ActorContext = Depends(_manage)
) -> RecoverySettingsRead:
    client_recovery_service.update_settings(
        session, actor,
        default_no_return_days=payload.default_no_return_days,
        default_follow_up_days=payload.default_follow_up_days,
        default_snooze_days=payload.default_snooze_days,
        whatsapp_message_template=payload.whatsapp_message_template,
    )
    settings = client_recovery_service.get_effective_settings(session, actor.organization_id)
    return RecoverySettingsRead(
        default_no_return_days=settings.default_no_return_days,
        default_follow_up_days=settings.default_follow_up_days,
        default_snooze_days=settings.default_snooze_days,
        whatsapp_message_template=settings.whatsapp_message_template,
    )
