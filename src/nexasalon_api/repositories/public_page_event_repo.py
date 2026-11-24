import uuid
from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexasalon_api.models.enums import PublicPageEventType
from nexasalon_api.models.public_page_event import PublicPageEvent


def create(session: Session, event: PublicPageEvent) -> PublicPageEvent:
    session.add(event)
    session.flush()
    return event


def find_page_view_by_token(
    session: Session, organization_id: uuid.UUID, attribution_token: str
) -> PublicPageEvent | None:
    """Usado SÓ pra validar um `attribution_token` recebido na criação de
    agendamento (`services/public_page_events.py::resolve_attribution`)
    — nunca aceita o token às cegas: ele só conta como atribuição válida
    se corresponder a um `page_view` de fato registrado PARA ESTA MESMA
    organização (nunca de outra, nunca um valor fabricado pelo
    cliente)."""
    stmt = (
        select(PublicPageEvent)
        .where(
            PublicPageEvent.organization_id == organization_id,
            PublicPageEvent.attribution_token == attribution_token,
            PublicPageEvent.event_type == PublicPageEventType.PAGE_VIEW,
        )
        .order_by(PublicPageEvent.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def count_by_type(
    session: Session,
    organization_id: uuid.UUID,
    *,
    date_from: datetime,
    date_to: datetime,
) -> dict[PublicPageEventType, int]:
    stmt = (
        select(PublicPageEvent.event_type, func.count())
        .where(
            PublicPageEvent.organization_id == organization_id,
            PublicPageEvent.created_at >= date_from,
            PublicPageEvent.created_at <= date_to,
        )
        .group_by(PublicPageEvent.event_type)
    )
    return {row[0]: row[1] for row in session.execute(stmt).all()}


def daily_counts(
    session: Session,
    organization_id: uuid.UUID,
    *,
    event_types: list[PublicPageEventType],
    date_from: datetime,
    date_to: datetime,
):
    day = func.date_trunc("day", PublicPageEvent.created_at)
    stmt = (
        select(day.label("day"), PublicPageEvent.event_type, func.count())
        .where(
            PublicPageEvent.organization_id == organization_id,
            PublicPageEvent.event_type.in_(event_types),
            PublicPageEvent.created_at >= date_from,
            PublicPageEvent.created_at <= date_to,
        )
        .group_by(day, PublicPageEvent.event_type)
        .order_by(day)
    )
    return session.execute(stmt).all()


def clicks_by_link(
    session: Session, organization_id: uuid.UUID, *, date_from: datetime, date_to: datetime
):
    stmt = (
        select(PublicPageEvent.link_id, func.count())
        .where(
            PublicPageEvent.organization_id == organization_id,
            PublicPageEvent.event_type == PublicPageEventType.CUSTOM_LINK_CLICK,
            PublicPageEvent.link_id.is_not(None),
            PublicPageEvent.created_at >= date_from,
            PublicPageEvent.created_at <= date_to,
        )
        .group_by(PublicPageEvent.link_id)
    )
    return session.execute(stmt).all()


def traffic_sources(
    session: Session, organization_id: uuid.UUID, *, date_from: datetime, date_to: datetime
):
    """Agrupa por `utm_source` normalizado quando presente, senão pelo
    host do referrer — nunca inventa "Instagram" sem evidência (só conta
    como tal quando `utm_source`/`referrer_host` de fato indicam isso;
    ver `services/public_page_events.py::_bucket_traffic_source`, que
    processa o resultado cru desta query)."""
    stmt = (
        select(PublicPageEvent.utm_source, PublicPageEvent.referrer_host, func.count())
        .where(
            PublicPageEvent.organization_id == organization_id,
            PublicPageEvent.event_type == PublicPageEventType.PAGE_VIEW,
            PublicPageEvent.created_at >= date_from,
            PublicPageEvent.created_at <= date_to,
        )
        .group_by(PublicPageEvent.utm_source, PublicPageEvent.referrer_host)
    )
    return session.execute(stmt).all()


def booking_completed_appointment_ids(
    session: Session, organization_id: uuid.UUID, *, date_from: datetime, date_to: datetime
) -> list[uuid.UUID]:
    stmt = select(PublicPageEvent.appointment_id).where(
        PublicPageEvent.organization_id == organization_id,
        PublicPageEvent.event_type == PublicPageEventType.BOOKING_COMPLETED,
        PublicPageEvent.appointment_id.is_not(None),
        PublicPageEvent.created_at >= date_from,
        PublicPageEvent.created_at <= date_to,
    )
    return [row[0] for row in session.execute(stmt).all()]


def purge_older_than(session: Session, cutoff: datetime) -> int:
    """Retenção (LGPD) — `services/public_page_events.py::
    purge_events_older_than` decide o `cutoff` real a partir de
    `settings.public_page_events_retention_days`. Devolve a contagem de
    linhas removidas (verificável em teste e em log de execução manual)."""
    stmt = sa_delete(PublicPageEvent).where(PublicPageEvent.created_at < cutoff)
    result = session.execute(stmt)
    session.flush()
    # `.rowcount` existe em tempo de execução (CursorResult de um DELETE)
    # mas não no stub genérico `Result[Any]` do mypy.
    return result.rowcount or 0  # type: ignore[attr-defined]
