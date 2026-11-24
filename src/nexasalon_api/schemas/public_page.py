import uuid
from typing import Literal

from pydantic import BaseModel, Field

from nexasalon_api.models.enums import PublicLinkKind, PublicPageButtonStyle

# `booking_completed` DELIBERADAMENTE fora desta lista — nunca aceito
# por um POST público livre, só gravado internamente na criação do
# agendamento (ver `api/v1/public_booking.py::create_booking`).
PublicPageEventTypeInput = Literal[
    "page_view", "booking_click", "whatsapp_click", "instagram_click", "maps_click", "custom_link_click"
]


class PublicPageLinkRead(BaseModel):
    """Um botão já resolvido pra renderização — `href` é o link FINAL
    (wa.me com mensagem codificada, URL do Instagram, link do Maps, ou a
    URL do link personalizado), nunca dado cru que o frontend precise
    reprocessar. Nunca aparece um botão sem config válida (ver
    `services/public_page.py::_build_public_links`)."""

    id: uuid.UUID
    kind: PublicLinkKind
    label: str
    href: str
    icon_key: str | None


class PublicPageRead(BaseModel):
    """`GET /public/s/{organization_slug}` — allowlist explícito, nunca
    financeiro/cliente/dado interno. `booking_available` é `False` tanto
    quando o botão de agendar está oculto na config do NexaLink quanto
    quando `Organization.online_booking_enabled=False` — o botão some,
    o resto da página continua existindo."""

    display_name: str
    short_description: str | None
    logo_url: str | None
    primary_color_hex: str
    background_color_hex: str
    button_style: PublicPageButtonStyle
    booking_available: bool
    links: list[PublicPageLinkRead]


class PublicPageEventCreate(BaseModel):
    """`POST /public/s/{organization_slug}/events`. `attribution_token`
    obrigatório — gerado pelo FRONTEND a cada carregamento de página
    (nunca pelo backend, nunca reaproveitado entre carregamentos
    diferentes), permite correlacionar depois um `booking_completed`
    (ver `services/public_page_events.py::resolve_attribution`, que
    NUNCA aceita um token às cegas — valida que corresponde a um
    `page_view` real desta mesma organização)."""

    event_type: PublicPageEventTypeInput
    link_id: uuid.UUID | None = None
    attribution_token: str = Field(min_length=8, max_length=64)
    visitor_cookie_id: str | None = Field(default=None, max_length=64)
    utm_source: str | None = Field(default=None, max_length=80)
    utm_medium: str | None = Field(default=None, max_length=80)
    utm_campaign: str | None = Field(default=None, max_length=80)
    referrer_host: str | None = Field(default=None, max_length=255)
