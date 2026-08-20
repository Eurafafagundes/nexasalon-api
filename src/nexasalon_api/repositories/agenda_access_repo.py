import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from nexasalon_api.models.agenda_access import MembershipAgendaGrant


def list_for_membership(session: Session, membership_id: uuid.UUID) -> list[MembershipAgendaGrant]:
    stmt = select(MembershipAgendaGrant).where(MembershipAgendaGrant.membership_id == membership_id)
    return list(session.scalars(stmt).all())


def replace_for_membership(
    session: Session,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    grants: list[tuple[uuid.UUID, bool, bool]],
) -> list[MembershipAgendaGrant]:
    """Substitui TODAS as linhas desta membership pelo conjunto
    informado (`(professional_id, can_view, can_edit)`) — semântica de
    "PUT o estado inteiro", igual ao resto do catálogo de overrides
    deste módulo (nunca um PATCH incremental que poderia deixar lixo de
    uma seleção anterior)."""
    session.execute(delete(MembershipAgendaGrant).where(MembershipAgendaGrant.membership_id == membership_id))
    rows = []
    for professional_id, can_view, can_edit in grants:
        row = MembershipAgendaGrant(
            organization_id=organization_id,
            membership_id=membership_id,
            professional_id=professional_id,
            can_view=can_view,
            can_edit=can_edit,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows
