import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from nexasalon_api.models.professional import Professional


def get(session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID) -> Professional | None:
    stmt = select(Professional).where(
        Professional.id == professional_id, Professional.organization_id == organization_id
    )
    return session.scalars(stmt).first()


def list_all(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[Professional]:
    stmt = select(Professional).where(Professional.organization_id == organization_id).order_by(Professional.name)
    if not include_inactive:
        stmt = stmt.where(Professional.is_active.is_(True))
    return list(session.scalars(stmt).all())


def list_by_ids(
    session: Session, organization_id: uuid.UUID, professional_ids: list[uuid.UUID]
) -> list[Professional]:
    """Usado por `services/agenda_access.py` pra validar, em lote, que
    todo profissional referenciado numa configuração de acesso existe
    NESTA organização (defesa em profundidade — RLS/FK já impediriam
    referenciar outra org, mas com uma mensagem de erro clara em vez de
    um 500)."""
    if not professional_ids:
        return []
    stmt = select(Professional).where(
        Professional.organization_id == organization_id, Professional.id.in_(professional_ids)
    )
    return list(session.scalars(stmt).all())


def create(session: Session, organization_id: uuid.UUID, **fields) -> Professional:
    professional = Professional(organization_id=organization_id, **fields)
    session.add(professional)
    session.flush()
    return professional


def save(session: Session, professional: Professional) -> Professional:
    session.flush()
    return professional


def get_by_user(session: Session, organization_id: uuid.UUID, user_id: uuid.UUID) -> Professional | None:
    stmt = select(Professional).where(
        Professional.organization_id == organization_id, Professional.user_id == user_id
    )
    return session.scalars(stmt).first()


def list_schedule_columns(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None = None
) -> list[Professional]:
    """Profissionais que devem virar COLUNA na Agenda principal — monta
    a lista dinamicamente a partir de 3 flags de configuração, nunca de
    uma lista fixa: `is_active`, `has_schedule` (existe agenda pra ele) e
    `show_on_main_schedule` (aparece na grade principal, não só em
    agendas individuais). Ordenado por `display_order` (a ordem que a
    própria organização escolheu), com o nome como desempate estável.

    `branch_id`, se informado, inclui tanto quem atende SÓ naquela
    unidade quanto quem atende em qualquer unidade (`branch_id IS NULL`
    no Professional — mesma convenção usada na validação de
    agendamento, ver `services/appointments.py`)."""
    stmt = (
        select(Professional)
        .where(
            Professional.organization_id == organization_id,
            Professional.is_active.is_(True),
            Professional.has_schedule.is_(True),
            Professional.show_on_main_schedule.is_(True),
        )
    )
    if branch_id is not None:
        stmt = stmt.where(or_(Professional.branch_id.is_(None), Professional.branch_id == branch_id))
    stmt = stmt.order_by(Professional.display_order, Professional.name)
    return list(session.scalars(stmt).all())
