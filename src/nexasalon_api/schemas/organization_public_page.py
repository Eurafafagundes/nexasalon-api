import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexasalon_api.core.normalize import normalize_whatsapp_number
from nexasalon_api.models.enums import PublicLinkKind, PublicPageButtonStyle

_HEX_PATTERN = r"^#[0-9A-Fa-f]{6}$"
_ALLOWED_URL_SCHEMES = ("http://", "https://")


class PublicLinkInput(BaseModel):
    """Um botão (nativo ou personalizado) no payload de `PUT
    /organization/public-page`. Links NATIVOS (`kind != CUSTOM`) são
    identificados pelo próprio `kind` (único por organização — upsert
    no service, `id` é ignorado/pode vir `None`, nunca criados em
    duplicata nem removidos por aqui). Links PERSONALIZADOS usam `id`
    pra decidir editar (`id` de um já existente) ou criar
    (`id=None`) — ver `services/organization_public_page.py`."""

    id: uuid.UUID | None = None
    kind: PublicLinkKind
    is_visible: bool = True
    custom_label: str | None = Field(default=None, max_length=80)

    # --- kind == CUSTOM ---
    title: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=2048)
    icon_key: str | None = Field(default=None, max_length=40)

    # --- kind == WHATSAPP ---
    whatsapp_number: str | None = Field(default=None, max_length=32)
    whatsapp_message: str | None = Field(default=None, max_length=500)

    # --- kind == INSTAGRAM ---
    instagram_username: str | None = Field(default=None, max_length=80)

    # --- kind == LOCATION ---
    maps_url_override: str | None = Field(default=None, max_length=2048)

    @field_validator("url", "maps_url_override", mode="after")
    @classmethod
    def _validate_external_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.lower().startswith(_ALLOWED_URL_SCHEMES):
            raise ValueError("Link inválido — use um endereço http:// ou https://.")
        return value

    @field_validator("whatsapp_number", mode="after")
    @classmethod
    def _normalize_whatsapp(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digits = normalize_whatsapp_number(value)
        if len(digits) not in (12, 13):
            raise ValueError("Número de WhatsApp inválido — informe DDI, DDD e número.")
        return digits

    @field_validator("instagram_username", mode="after")
    @classmethod
    def _normalize_instagram(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        text = value.strip()
        # Aceita "@usuario", "usuario" ou uma URL do Instagram — sempre
        # normaliza pro usuário puro, nunca guarda a URL inteira.
        for prefix in ("https://www.instagram.com/", "http://www.instagram.com/", "https://instagram.com/", "instagram.com/"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):]
                break
        text = text.lstrip("@").split("/")[0].split("?")[0]
        if not text:
            raise ValueError("Usuário do Instagram inválido.")
        return text


class OrganizationPublicPageUpdate(BaseModel):
    """`PUT /organization/public-page` — salva config + a lista COMPLETA
    de links (reorder/add/edit/remove/toggle) numa única transação (item
    "transação pra salvar config+ordenação"). `display_order` é
    reindexado no backend antes de persistir — nunca confia na ordem
    crua vinda do cliente sem revalidar."""

    is_enabled: bool
    display_name: str | None = Field(default=None, max_length=160)
    short_description: str | None = Field(default=None, max_length=280)
    primary_color_hex: str = Field(pattern=_HEX_PATTERN)
    background_color_hex: str = Field(pattern=_HEX_PATTERN)
    button_style: PublicPageButtonStyle
    # Ordenação = posição no ARRAY, nunca um campo numérico enviado pelo
    # cliente ("nunca confia na ordem crua do cliente, reindexa no
    # backend") — `display_order` real persistido é sempre
    # `enumerate(links)`, ver `services/organization_public_page.py`.
    links: list[PublicLinkInput]

    @model_validator(mode="after")
    def _exactly_one_of_each_native(self) -> "OrganizationPublicPageUpdate":
        natives = [link.kind for link in self.links if link.kind != PublicLinkKind.CUSTOM]
        if sorted(k.value for k in natives) != sorted(
            {PublicLinkKind.BOOKING, PublicLinkKind.WHATSAPP, PublicLinkKind.INSTAGRAM, PublicLinkKind.LOCATION},
            key=lambda k: k.value,
        ):
            raise ValueError("Os 4 botões nativos (agendar, WhatsApp, Instagram, localização) são obrigatórios no payload.")
        return self


class PublicLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # `None` só no caso "config ainda não salva" (defaults em memória,
    # nenhuma linha real no banco) — depois do 1º PUT sempre vem
    # preenchido.
    id: uuid.UUID | None
    kind: PublicLinkKind
    is_visible: bool
    display_order: int
    custom_label: str | None
    title: str | None
    url: str | None
    icon_key: str | None
    whatsapp_number: str | None
    whatsapp_message: str | None
    instagram_username: str | None
    maps_url_override: str | None


class OrganizationPublicPageRead(BaseModel):
    """Resposta do `GET`/`PUT` — sempre inclui `organization_name`/
    `organization_logo_url` DERIVADOS da Organization (nunca duplicados
    em `organization_public_pages`, ver docstring do model). O endereço
    público (`/s/{slug}`) é montado no FRONTEND a partir de
    `Organization.slug` (já disponível via `GET /organization`) +
    `window.location.origin` — mesmo padrão já usado pra `/agendar/
    {slug}` em `agendamento-online-section.tsx`, nunca uma URL absoluta
    vinda do backend. Sem linha ainda (nenhum `PUT` salvo):
    `is_enabled=False` e os demais campos de config vêm com os DEFAULTS
    em memória — nunca grava nada numa leitura."""

    model_config = ConfigDict(from_attributes=False)

    is_enabled: bool
    display_name: str | None
    short_description: str | None
    primary_color_hex: str
    background_color_hex: str
    button_style: PublicPageButtonStyle
    updated_at: datetime | None
    organization_name: str
    organization_logo_url: str | None
    links: list[PublicLinkRead]


class PublicPageDailyPoint(BaseModel):
    date: str
    page_views: int
    booking_clicks: int


class PublicPageChannelCount(BaseModel):
    channel: str
    label: str
    count: int


class PublicPageTrafficSourceCount(BaseModel):
    source: str
    count: int


class PublicPageServiceCount(BaseModel):
    service_id: uuid.UUID
    service_name: str
    count: int


class OrganizationPublicPageAnalyticsRead(BaseModel):
    """`GET /organization/public-page/analytics` — taxa de conversão
    definida EXPLICITAMENTE como `bookings_completed / booking_clicks`
    (nunca `/ page_views` — o funil correto é intenção de agendar →
    agendamento concluído). `None` quando `booking_clicks == 0` (nunca
    `0%`/erro de divisão por zero)."""

    page_views: int
    booking_clicks: int
    whatsapp_clicks: int
    instagram_clicks: int
    maps_clicks: int
    custom_link_clicks: int
    bookings_completed: int
    conversion_rate: Decimal | None
    daily_series: list[PublicPageDailyPoint]
    clicks_by_channel: list[PublicPageChannelCount]
    traffic_sources: list[PublicPageTrafficSourceCount]
    top_services: list[PublicPageServiceCount]
