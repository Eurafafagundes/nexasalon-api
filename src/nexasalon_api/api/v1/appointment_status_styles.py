from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_current_actor, get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.enums import AppointmentStatus
from nexasalon_api.schemas.appointment_status_style import AppointmentStatusStyleRead, AppointmentStatusStyleUpdate
from nexasalon_api.services import appointment_status_styles as service

router = APIRouter(prefix="/appointment-status-styles", tags=["appointment-status-styles"])

_manage = require_permission("settings.manage")


@router.get(
    "", response_model=list[AppointmentStatusStyleRead],
    summary="Personalizações de nome/cor dos status da Agenda (Configurações > Status da Agenda)",
)
def list_appointment_status_styles(
    session: Session = Depends(get_db), actor: ActorContext = Depends(get_current_actor)
) -> list[AppointmentStatusStyleRead]:
    """De propósito, sem `require_permission` além de estar autenticado
    (mesmo padrão de `GET /organization`): QUALQUER membro precisa
    disto pra a Agenda renderizar as cores/nomes certos, não é uma
    configuração sensível como financeiro. Só a ESCRITA (abaixo) exige
    `settings.manage`. Resposta é SPARSE — só os status que a
    organização já personalizou; o resto fica no padrão de fábrica do
    próprio frontend."""
    styles = service.list_styles(session, actor)
    return [AppointmentStatusStyleRead.model_validate(s) for s in styles]


@router.put(
    "/{status_code}", response_model=AppointmentStatusStyleRead | None,
    summary="Personalizar nome/cor de um status oficial (OWNER/ADMIN)",
)
def set_appointment_status_style(
    status_code: AppointmentStatus,
    payload: AppointmentStatusStyleUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> AppointmentStatusStyleRead | None:
    """`status_code` é validado automaticamente pelo FastAPI contra o
    enum `AppointmentStatus` (8 valores fixos) — não existe caminho pra
    personalizar um "status" que não seja um dos 8 oficiais; criar
    status extras está fora desta rodada. Enviar os dois campos nulos
    reseta pro padrão de fábrica (mesma semântica do DELETE abaixo)."""
    style = service.set_style(session, actor, status_code, payload)
    return AppointmentStatusStyleRead.model_validate(style) if style is not None else None


@router.delete(
    "/{status_code}", status_code=status.HTTP_204_NO_CONTENT,
    summary="Resetar um status pro nome/cor padrão de fábrica (OWNER/ADMIN)",
)
def reset_appointment_status_style(
    status_code: AppointmentStatus,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> None:
    service.reset_style(session, actor, status_code)
