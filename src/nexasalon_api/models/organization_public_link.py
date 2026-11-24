import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import PublicLinkKind, pg_enum


class OrganizationPublicLink(Base, UUIDPKMixin, TimestampMixin):
    """Um botão da página pública NexaLink — NATIVO (`kind` em
    booking/whatsapp/instagram/location, uma linha cada, sempre
    existente a partir do 1º `PUT`, nunca deletável, só `is_visible=false`)
    ou PERSONALIZADO (`kind='custom'`, N linhas livres, criáveis/
    removíveis). `display_order` é um ÚNICO espaço compartilhado por
    TODOS os kinds de uma organização — satisfaz "todos os botões,
    inclusive os nativos, precisam de ordenação configurável" sem duas
    fontes de ordem (nunca uma ordem pros nativos e outra pros custom).

    Campos específicos de cada canal ficam NULLABLE na mesma linha larga
    (4 canais fixos não justificam uma tabela por canal — evitaria N
    JOINs só pra montar a página pública inteira). `whatsapp_number`/
    `instagram_username` pré-preenchem (nunca vinculam permanentemente)
    a partir de `Organization.whatsapp`/`Organization.instagram` quando
    o canal nativo é criado pela primeira vez — depois disso podem
    divergir do cadastro (ver `services/organization_public_page.py`)."""

    __tablename__ = "organization_public_links"
    __table_args__ = (
        # DEFERRABLE INITIALLY DEFERRED — reordenar troca `display_order`
        # de várias linhas na mesma transação; sem isto, a constraint
        # colidiria momentaneamente a cada UPDATE individual (ver
        # docstring da migration 0060).
        UniqueConstraint("organization_id", "display_order", deferrable=True, initially="DEFERRED"),
        # UNIQUE parcial — nunca duas linhas nativas (`kind != custom`)
        # pra mesma organização; `custom` fica livre (N linhas).
        Index(
            "ux_organization_public_links_org_native_kind", "organization_id", "kind",
            unique=True, postgresql_where=text("kind != 'custom'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[PublicLinkKind] = mapped_column(pg_enum(PublicLinkKind, "public_link_kind"), nullable=False)

    is_visible: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # Texto do botão — nativos têm default calculado em memória quando
    # `NULL` ("Agendar meu horário", "Falar pelo WhatsApp" etc.).
    custom_label: Mapped[str | None] = mapped_column(String(80))

    # --- kind == CUSTOM ---
    title: Mapped[str | None] = mapped_column(String(80))
    url: Mapped[str | None] = mapped_column(String(2048))
    icon_key: Mapped[str | None] = mapped_column(String(40))

    # --- kind == WHATSAPP --- número com DDI, só dígitos (ver
    # `core/normalize.py::normalize_whatsapp_number`).
    whatsapp_number: Mapped[str | None] = mapped_column(String(20))
    whatsapp_message: Mapped[str | None] = mapped_column(String(500))

    # --- kind == INSTAGRAM --- normalizado, sem @, sem URL.
    instagram_username: Mapped[str | None] = mapped_column(String(80))

    # --- kind == LOCATION --- endereço reaproveitado de Organization
    # quando `NULL`; link do Maps pode ser sobrescrito manualmente.
    maps_url_override: Mapped[str | None] = mapped_column(String(2048))

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
