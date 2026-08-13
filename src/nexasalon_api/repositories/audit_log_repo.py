import uuid
from typing import Any

from sqlalchemy.orm import Session

from nexasalon_api.models.audit import AuditLog
from nexasalon_api.models.enums import AuditAction


def create(
    session: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    action: AuditAction,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
) -> AuditLog:
    """Append-only — nunca UPDATE/DELETE (ver docstring do model). O
    catálogo de `AuditAction` é só CREATE/UPDATE/DELETE; eventos mais
    específicos (reagendamento, troca de profissional, force_overlap
    etc.) são identificados por uma chave `change_type` dentro de
    `new_values`, não por um novo valor de enum — evita alterar a
    modelagem já aprovada só para nomear casos de UPDATE."""
    log = AuditLog(
        organization_id=organization_id,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        old_values=old_values,
        new_values=new_values,
    )
    session.add(log)
    session.flush()
    return log

def list_for_entity(
    session: Session, organization_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> list[AuditLog]:
    from sqlalchemy import select

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.organization_id == organization_id,
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == entity_id,
        )
        .order_by(AuditLog.created_at)
    )
    return list(session.scalars(stmt).all())
