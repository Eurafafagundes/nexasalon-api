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
    user_name: str | None = None,
) -> AuditLog:
    """Append-only — nunca UPDATE/DELETE (ver docstring do model). O
    catálogo de `AuditAction` é só CREATE/UPDATE/DELETE; eventos mais
    específicos (reagendamento, troca de profissional, force_overlap
    etc.) são identificados por uma chave `change_type` dentro de
    `new_values`, não por um novo valor de enum — evita alterar a
    modelagem já aprovada só para nomear casos de UPDATE.

    `user_name` (migration 0058) é o SNAPSHOT do nome do usuário no
    momento da ação — quem chama já deve ter resolvido isso (ex.:
    `user_repo.get(session, actor.user_id).name`), nunca um join feito
    aqui dentro: o objetivo é justamente nunca depender do estado ATUAL
    do usuário pra exibir o histórico depois (ver docstring de
    `models/audit.py::AuditLog.user_name`)."""
    log = AuditLog(
        organization_id=organization_id,
        user_id=user_id,
        user_name=user_name,
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
    """Histórico completo de uma entidade, do mais ANTIGO pro mais
    recente (ordem cronológica natural — mantida assim de propósito:
    `tests/test_clients_archiving.py::test_auditoria_registra_estado_
    anterior_novo_e_usuario` já depende desta ordem para indexar
    `logs[0]`/`logs[1]` como "primeira ação"/"segunda ação"). Quem
    precisar exibir "mais recente primeiro" — caso de
    `services/client_observations.py::get_observation_history` — inverte
    a lista no próprio chamador, nunca aqui."""
    from sqlalchemy import select

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.organization_id == organization_id,
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == entity_id,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list(session.scalars(stmt).all())
