"""Personalização de nome/cor dos 8 status oficiais (Configurações >
Status da Agenda). Ver docstring de `models/appointment_status_style.py`
pro raciocínio de isolamento — nada aqui toca `Appointment.status`,
`services/appointment_state_machine.py`, Comanda ou auditoria de
agendamento; é uma camada de apresentação por organização.

Leitura (`list_styles`) é aberta a qualquer membro autenticado (mesmo
padrão de `services/organizations.py::get_current_organization` — dado
de UI, não sensível). Escrita (`set_style`/`reset_style`) é só chamada
por rotas atrás de `require_permission("settings.manage")`
(OWNER/ADMIN, catálogo já existente desde a migration 0007) — este
módulo não reforça a permissão de novo, confia no dependency da rota
(mesmo padrão dos demais services do projeto)."""
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.models.appointment_status_style import AppointmentStatusStyle
from nexasalon_api.models.enums import AppointmentStatus, AuditAction
from nexasalon_api.repositories import appointment_status_style_repo, audit_log_repo
from nexasalon_api.schemas.appointment_status_style import AppointmentStatusStyleUpdate


def list_styles(session: Session, actor: ActorContext) -> list[AppointmentStatusStyle]:
    return appointment_status_style_repo.list_for_org(session, actor.organization_id)


def set_style(
    session: Session, actor: ActorContext, status_code: AppointmentStatus, data: AppointmentStatusStyleUpdate
) -> AppointmentStatusStyle | None:
    """`label`/`color_hex` ambos `None` -> equivale a resetar (mesmo
    caminho de `reset_style`, sem deixar uma linha "vazia" na tabela)."""
    if data.label is None and data.color_hex is None:
        return reset_style(session, actor, status_code)

    old = appointment_status_style_repo.get(session, actor.organization_id, status_code)
    old_values = (
        {"label": old.label, "color_hex": old.color_hex} if old is not None else {"label": None, "color_hex": None}
    )

    style = appointment_status_style_repo.upsert(
        session, actor.organization_id, status_code,
        label=data.label, color_hex=data.color_hex, updated_by=actor.user_id,
    )

    audit_log_repo.create(
        session, organization_id=actor.organization_id, user_id=actor.user_id,
        entity_type="appointment_status_style", entity_id=style.id,
        action=AuditAction.CREATE if old is None else AuditAction.UPDATE,
        old_values=old_values,
        new_values={"status_code": status_code.value, "label": style.label, "color_hex": style.color_hex},
    )
    return style


def reset_style(session: Session, actor: ActorContext, status_code: AppointmentStatus) -> None:
    old = appointment_status_style_repo.get(session, actor.organization_id, status_code)
    appointment_status_style_repo.delete(session, actor.organization_id, status_code)
    if old is not None:
        audit_log_repo.create(
            session, organization_id=actor.organization_id, user_id=actor.user_id,
            entity_type="appointment_status_style", entity_id=old.id,
            action=AuditAction.DELETE,
            old_values={"status_code": status_code.value, "label": old.label, "color_hex": old.color_hex},
            new_values={"change_type": "reset_to_default"},
        )
    return None
