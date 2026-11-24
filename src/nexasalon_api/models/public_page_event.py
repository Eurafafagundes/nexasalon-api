import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import PublicPageEventType, pg_enum


class PublicPageEvent(Base):
    """Append-only, alto volume — mesmo molde estrutural de `AuditLog`:
    `BigInteger` autoincrement (não UUID, de propósito — facilita
    particionar por `created_at` no futuro), sem `updated_at`, sem
    UPDATE/DELETE pela aplicação. `organization_id` NOT NULL (sempre
    conhecida aqui — resolvida por slug ANTES de qualquer insert, nunca
    um evento "de plataforma" como em `AuditLog`).

    NUNCA grava IP bruto (rate limit por IP fica só em memória, ver
    `core/rate_limit.py`) nem telefone/CPF — só um cookie opaco
    (`visitor_cookie_id`, `nx_vid` no frontend) e um token opaco por
    carregamento de página (`attribution_token`), nunca fingerprint.

    O índice único PARCIAL em `appointment_id` (só onde
    `event_type='booking_completed'`) garante NO BANCO que um retry de
    rede na criação do agendamento nunca duplica a conversão atribuída —
    ver `services/public_page_events.py::record_booking_completed`.

    Retenção: 90 dias (LGPD) — `services/public_page_events.py::
    purge_events_older_than`, sem scheduler automático nesta rodada
    (nenhuma infra de cron existe no projeto ainda); função testada e
    chamável manualmente/por tarefa futura."""

    __tablename__ = "public_page_events"
    __table_args__ = (
        Index("ix_public_page_events_org_type_created", "organization_id", "event_type", "created_at"),
        Index(
            "ux_public_page_events_booking_completed_appointment",
            "appointment_id",
            unique=True,
            postgresql_where=text("event_type = 'booking_completed'"),
        ),
        Index(
            "ix_public_page_events_org_token",
            "organization_id",
            "attribution_token",
            postgresql_where=text("attribution_token IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[PublicPageEventType] = mapped_column(
        pg_enum(PublicPageEventType, "public_page_event_type"), nullable=False
    )
    link_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_public_links.id", ondelete="SET NULL")
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id", ondelete="SET NULL")
    )

    attribution_token: Mapped[str | None] = mapped_column(String(64))
    visitor_cookie_id: Mapped[str | None] = mapped_column(String(64))

    # Origem — sempre truncada/sanitizada antes de persistir (ver
    # `services/public_page_events.py::_sanitize_attribution`), nunca
    # free-form grande nem a URL completa de referrer (só o host).
    utm_source: Mapped[str | None] = mapped_column(String(80))
    utm_medium: Mapped[str | None] = mapped_column(String(80))
    utm_campaign: Mapped[str | None] = mapped_column(String(80))
    referrer_host: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
