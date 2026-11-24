import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import PublicPageButtonStyle, pg_enum


class OrganizationPublicPage(Base, UUIDPKMixin, TimestampMixin):
    """Config de publicação/identidade visual da página pública NexaLink
    (`/s/{slug}`) — uma linha por Organization. Padrão SPARSE deliberado
    (mesmo raciocínio de `CashRegisterConfig`/`AppointmentStatusStyle`):
    só existe depois do primeiro `PUT` (`services/organization_public_page.py`),
    nunca criada numa leitura — `GET` sem linha ainda devolve defaults EM
    MEMÓRIA.

    `display_name`/`short_description` nascem `NULL` e caem no fallback
    `Organization.name` em memória — nunca sobrescrevem o nome de
    exibição usado no resto do sistema (Agendamento Online, Dashboard
    etc.). NUNCA duplica `Organization.logo_url` — a página pública lê
    direto da Organization; não há upload próprio do NexaLink."""

    __tablename__ = "organization_public_pages"
    __table_args__ = (
        CheckConstraint("primary_color_hex ~ '^#[0-9A-Fa-f]{6}$'", name="ck_organization_public_pages_primary_color_format"),
        CheckConstraint(
            "background_color_hex ~ '^#[0-9A-Fa-f]{6}$'", name="ck_organization_public_pages_background_color_format"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    is_enabled: Mapped[bool] = mapped_column(nullable=False, server_default="false")

    display_name: Mapped[str | None] = mapped_column(String(160))
    short_description: Mapped[str | None] = mapped_column(String(280))
    primary_color_hex: Mapped[str] = mapped_column(String(7), nullable=False, server_default="#7C3AED")
    background_color_hex: Mapped[str] = mapped_column(String(7), nullable=False, server_default="#FFFFFF")
    button_style: Mapped[PublicPageButtonStyle] = mapped_column(
        pg_enum(PublicPageButtonStyle, "public_page_button_style"),
        nullable=False,
        server_default=PublicPageButtonStyle.ROUNDED.value,
    )

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
