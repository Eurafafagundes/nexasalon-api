import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexasalon_api.core.config import settings
from nexasalon_api.models.appointment import AppointmentItem
from nexasalon_api.models.enums import PublicPageEventType
from nexasalon_api.models.public_page_event import PublicPageEvent
from nexasalon_api.repositories import public_page_event_repo, service_repo
from nexasalon_api.schemas.organization_public_page import (
    OrganizationPublicPageAnalyticsRead,
    PublicPageChannelCount,
    PublicPageDailyPoint,
    PublicPageServiceCount,
    PublicPageTrafficSourceCount,
)
from nexasalon_api.schemas.public_page import PublicPageEventCreate
from nexasalon_api.services.public_page import resolve_link_for_click

_CHANNEL_LABELS = {
    PublicPageEventType.BOOKING_CLICK: "Agendar",
    PublicPageEventType.WHATSAPP_CLICK: "WhatsApp",
    PublicPageEventType.INSTAGRAM_CLICK: "Instagram",
    PublicPageEventType.MAPS_CLICK: "Como chegar",
    PublicPageEventType.CUSTOM_LINK_CLICK: "Links personalizados",
}

# Tamanho máximo persistido por campo de origem — truncar aqui, NUNCA no
# banco (erro de "value too long" não pode derrubar o registro de um
# evento de navegação anônima). Mesmos limites das colunas (migration 0060).
_MAX_UTM_LENGTH = 80
_MAX_REFERRER_HOST_LENGTH = 255


def _truncate(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value[:max_length]


def record_event(
    session: Session, organization_id: uuid.UUID, payload: PublicPageEventCreate
) -> None:
    """`POST /public/s/{slug}/events` — nunca aceita `booking_completed`
    aqui (fora do `Literal` do schema, ver `schemas/public_page.py`).
    `link_id` só é persistido se de fato pertencer a esta organização
    (`resolve_link_for_click`) — nunca um id de outra organização
    adivinhado/vazado."""
    link = resolve_link_for_click(session, organization_id, payload.link_id)
    event = PublicPageEvent(
        organization_id=organization_id,
        event_type=PublicPageEventType(payload.event_type),
        link_id=link.id if link is not None else None,
        attribution_token=payload.attribution_token,
        visitor_cookie_id=_truncate(payload.visitor_cookie_id, 64),
        utm_source=_truncate(payload.utm_source, _MAX_UTM_LENGTH),
        utm_medium=_truncate(payload.utm_medium, _MAX_UTM_LENGTH),
        utm_campaign=_truncate(payload.utm_campaign, _MAX_UTM_LENGTH),
        referrer_host=_truncate(payload.referrer_host, _MAX_REFERRER_HOST_LENGTH),
    )
    public_page_event_repo.create(session, event)


# Janela de validade de um `attribution_token` — um `page_view` muito
# antigo não deveria mais contar como origem de uma conversão (ex.:
# alguém reabre uma aba de navegador esquecida há semanas). 7 dias cobre
# qualquer ciclo realista de decisão de agendar depois de ver a página.
_ATTRIBUTION_TOKEN_MAX_AGE = timedelta(days=7)


def resolve_attribution(
    session: Session, organization_id: uuid.UUID, attribution_token: str | None
) -> bool:
    """Valida um `attribution_token` recebido na criação de agendamento —
    NUNCA aceito às cegas (item explícito "validar a origem, organização
    e validade do attribution_token, evitando conversões falsas ou
    duplicadas"). Exige DOIS eventos reais, registrados para ESTA MESMA
    organização e com o MESMO token (nunca de outra organização, nunca
    um valor fabricado/copiado):

      1. um `page_view` — confirma que o token corresponde a um
         carregamento real da página NexaLink;
      2. um `booking_click` — confirma que a visita de fato terminou num
         clique no botão "Agendar" (fecha a lacuna de só exigir
         `page_view`: sem isto, qualquer agendamento criado por OUTRO
         caminho, carregando um token antigo de uma visita anterior,
         seria contado como conversão do NexaLink mesmo sem a pessoa
         nunca ter clicado em agendar a partir de lá).

    Ambos dentro da janela de validade (a partir do `page_view`, sempre
    o mais antigo dos dois no fluxo real). Token ausente/inválido/
    expirado/sem o clique → `False`, NUNCA bloqueia o agendamento em si
    (atribuição é só analytics, nunca uma condição pra criar a
    reserva)."""
    if not attribution_token:
        return False
    page_view = public_page_event_repo.find_event_by_token(
        session, organization_id, attribution_token, PublicPageEventType.PAGE_VIEW
    )
    if page_view is None:
        return False
    if datetime.now(timezone.utc) - page_view.created_at > _ATTRIBUTION_TOKEN_MAX_AGE:
        return False
    booking_click = public_page_event_repo.find_event_by_token(
        session, organization_id, attribution_token, PublicPageEventType.BOOKING_CLICK
    )
    return booking_click is not None


def record_booking_completed(
    session: Session, organization_id: uuid.UUID, appointment_id: uuid.UUID, attribution_token: str | None
) -> None:
    """Chamada DENTRO da mesma transação de `appointments_service.
    create_public_appointment_for_customer` (ver `api/v1/public_booking.py::
    create_booking`) — nunca um POST público solto. Token inválido/
    ausente: não grava nada, nunca levanta erro (uma falha de atribuição
    nunca pode impedir ou reverter um agendamento real).

    Dedup: `ux_public_page_events_booking_completed_appointment`
    (índice único parcial no banco) garante zero duplicidade mesmo sob
    retry — usa SAVEPOINT (`begin_nested`, mesmo padrão de `services/
    orders.py::create_order`) pra que uma eventual violação não derrube
    a transação inteira do agendamento."""
    if not resolve_attribution(session, organization_id, attribution_token):
        return

    event = PublicPageEvent(
        organization_id=organization_id,
        event_type=PublicPageEventType.BOOKING_COMPLETED,
        appointment_id=appointment_id,
        attribution_token=attribution_token,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        pass  # já existe um booking_completed pra este appointment — retry, ignorado


def _bucket_traffic_source(utm_source: str | None, referrer_host: str | None) -> str:
    if utm_source:
        return utm_source.strip().lower()
    if referrer_host:
        host = referrer_host.strip().lower()
        if "instagram.com" in host:
            return "instagram"
        if "facebook.com" in host or "fb.com" in host:
            return "facebook"
        if "google." in host:
            return "google"
        if "whatsapp.com" in host or "wa.me" in host:
            return "whatsapp"
        return host
    return "Direto/Desconhecido"


def get_analytics(
    session: Session, organization_id: uuid.UUID, *, date_from: datetime, date_to: datetime
) -> OrganizationPublicPageAnalyticsRead:
    counts = public_page_event_repo.count_by_type(session, organization_id, date_from=date_from, date_to=date_to)
    page_views = counts.get(PublicPageEventType.PAGE_VIEW, 0)
    booking_clicks = counts.get(PublicPageEventType.BOOKING_CLICK, 0)
    whatsapp_clicks = counts.get(PublicPageEventType.WHATSAPP_CLICK, 0)
    instagram_clicks = counts.get(PublicPageEventType.INSTAGRAM_CLICK, 0)
    maps_clicks = counts.get(PublicPageEventType.MAPS_CLICK, 0)
    custom_link_clicks = counts.get(PublicPageEventType.CUSTOM_LINK_CLICK, 0)
    bookings_completed = counts.get(PublicPageEventType.BOOKING_COMPLETED, 0)

    # Definição EXPLÍCITA: concluídos / cliques em Agendar (nunca /
    # visualizações) — funil de intenção -> ação. `None` sem cliques,
    # nunca "0%"/erro de divisão por zero.
    conversion_rate = (
        (Decimal(bookings_completed) / Decimal(booking_clicks)) if booking_clicks > 0 else None
    )

    daily_rows = public_page_event_repo.daily_counts(
        session, organization_id,
        event_types=[PublicPageEventType.PAGE_VIEW, PublicPageEventType.BOOKING_CLICK],
        date_from=date_from, date_to=date_to,
    )
    daily: dict[str, dict[str, int]] = {}
    current = date_from.date()
    while current <= date_to.date():
        daily[current.isoformat()] = {"page_views": 0, "booking_clicks": 0}
        current += timedelta(days=1)
    for day, event_type, count in daily_rows:
        key = day.date().isoformat()
        if key not in daily:
            continue
        if event_type == PublicPageEventType.PAGE_VIEW:
            daily[key]["page_views"] = count
        elif event_type == PublicPageEventType.BOOKING_CLICK:
            daily[key]["booking_clicks"] = count
    daily_series = [
        PublicPageDailyPoint(date=day, page_views=v["page_views"], booking_clicks=v["booking_clicks"])
        for day, v in sorted(daily.items())
    ]

    clicks_by_channel = [
        PublicPageChannelCount(channel=event_type.value, label=label, count=counts.get(event_type, 0))
        for event_type, label in _CHANNEL_LABELS.items()
    ]

    traffic_rows = public_page_event_repo.traffic_sources(session, organization_id, date_from=date_from, date_to=date_to)
    traffic_buckets: dict[str, int] = {}
    for utm_source, referrer_host, count in traffic_rows:
        bucket = _bucket_traffic_source(utm_source, referrer_host)
        traffic_buckets[bucket] = traffic_buckets.get(bucket, 0) + count
    traffic_sources = [
        PublicPageTrafficSourceCount(source=source, count=count)
        for source, count in sorted(traffic_buckets.items(), key=lambda item: item[1], reverse=True)
    ]

    appointment_ids = public_page_event_repo.booking_completed_appointment_ids(
        session, organization_id, date_from=date_from, date_to=date_to
    )
    top_services: list[PublicPageServiceCount] = []
    if appointment_ids:
        stmt = (
            select(AppointmentItem.service_id, func.count())
            .where(AppointmentItem.organization_id == organization_id, AppointmentItem.appointment_id.in_(appointment_ids))
            .group_by(AppointmentItem.service_id)
            .order_by(func.count().desc())
            .limit(5)
        )
        rows = session.execute(stmt).all()
        if rows:
            services = {s.id: s for s in service_repo.list_by_ids(session, organization_id, {r[0] for r in rows})}
            top_services = [
                PublicPageServiceCount(
                    service_id=service_id, service_name=services[service_id].name if service_id in services else "Serviço", count=count
                )
                for service_id, count in rows
            ]

    return OrganizationPublicPageAnalyticsRead(
        page_views=page_views,
        booking_clicks=booking_clicks,
        whatsapp_clicks=whatsapp_clicks,
        instagram_clicks=instagram_clicks,
        maps_clicks=maps_clicks,
        custom_link_clicks=custom_link_clicks,
        bookings_completed=bookings_completed,
        conversion_rate=conversion_rate,
        daily_series=daily_series,
        clicks_by_channel=clicks_by_channel,
        traffic_sources=traffic_sources,
        top_services=top_services,
    )


def purge_events_older_than(session: Session, *, retention_days: int | None = None) -> int:
    """Retenção (LGPD) — remove eventos mais antigos que
    `settings.public_page_events_retention_days` (90 por padrão). Sem
    scheduler automático nesta rodada (nenhuma infra de cron existe no
    projeto ainda) — chamável manualmente ou por uma tarefa futura.
    Devolve a contagem de linhas removidas (verificável em teste/log)."""
    days = retention_days if retention_days is not None else settings.public_page_events_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return public_page_event_repo.purge_older_than(session, cutoff)
