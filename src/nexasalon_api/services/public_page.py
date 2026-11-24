import uuid
from urllib.parse import quote

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.enums import PublicLinkKind
from nexasalon_api.models.organization import Organization
from nexasalon_api.models.organization_public_link import OrganizationPublicLink
from nexasalon_api.repositories import (
    organization_public_link_repo,
    organization_public_page_repo,
)
from nexasalon_api.schemas.public_page import PublicPageLinkRead, PublicPageRead
from nexasalon_api.services.organization_public_page import DEFAULT_PUBLIC_LINK_LABELS


def _organization_address(organization: Organization) -> str | None:
    parts = [
        p
        for p in (
            organization.address_line,
            organization.address_number,
            organization.neighborhood,
            organization.city,
            organization.state.value if organization.state else None,
        )
        if p
    ]
    return ", ".join(parts) if parts else None


def _resolve_label(link: OrganizationPublicLink) -> str:
    if link.custom_label:
        return link.custom_label
    if link.kind == PublicLinkKind.CUSTOM:
        return link.title or "Link"
    return DEFAULT_PUBLIC_LINK_LABELS[link.kind]


def _build_href(link: OrganizationPublicLink, organization: Organization) -> str | None:
    """`None` = botão sem config válida — NUNCA aparece na página (item
    "não mostrar botão sem config válida"), mesmo que `is_visible=True`."""
    if link.kind == PublicLinkKind.BOOKING:
        # Navegação interna — o frontend acrescenta a querystring de
        # atribuição (`?nxat=...`) no clique, nunca aqui.
        return f"/agendar/{organization.slug}"
    if link.kind == PublicLinkKind.WHATSAPP:
        if not link.whatsapp_number:
            return None
        message = link.whatsapp_message or "Olá! Gostaria de informações sobre os serviços e horários disponíveis."
        return f"https://wa.me/{link.whatsapp_number}?text={quote(message)}"
    if link.kind == PublicLinkKind.INSTAGRAM:
        if not link.instagram_username:
            return None
        return f"https://www.instagram.com/{link.instagram_username}/"
    if link.kind == PublicLinkKind.LOCATION:
        if link.maps_url_override:
            return link.maps_url_override
        address = _organization_address(organization)
        if not address:
            return None
        return f"https://www.google.com/maps/search/?api=1&query={quote(address)}"
    # CUSTOM
    return link.url


def get_public_page(session: Session, organization: Organization) -> PublicPageRead:
    """Monta o payload da página pública — chamada só depois que
    `organization` já foi resolvida por slug sob o mecanismo de RLS de
    `get_public_page_context` (`api/deps.py`). 404 (`NotFoundError`,
    mesma mensagem de slug inexistente — anti-enumeração) quando a
    página nunca foi publicada OU está desativada: os dois casos são
    estruturalmente indistinguíveis por fora."""
    page = organization_public_page_repo.get(session, organization.id)
    if page is None or not page.is_enabled:
        raise NotFoundError("Página não encontrada.")

    db_links = organization_public_link_repo.list_for_org(session, organization.id)
    booking_enabled_here = False
    built: list[PublicPageLinkRead] = []
    for link in db_links:
        if not link.is_visible:
            continue
        if link.kind == PublicLinkKind.BOOKING and not organization.online_booking_enabled:
            # Botão de agendar oculto quando o Agendamento Online está
            # desligado — o resto da página continua normal (item
            # "NexaLink funciona independente do agendamento online
            # estar ativo").
            continue
        href = _build_href(link, organization)
        if href is None:
            continue
        if link.kind == PublicLinkKind.BOOKING:
            booking_enabled_here = True
        built.append(
            PublicPageLinkRead(id=link.id, kind=link.kind, label=_resolve_label(link), href=href, icon_key=link.icon_key)
        )

    return PublicPageRead(
        display_name=page.display_name or organization.name,
        short_description=page.short_description,
        logo_url=organization.logo_url,
        primary_color_hex=page.primary_color_hex,
        background_color_hex=page.background_color_hex,
        button_style=page.button_style,
        booking_available=booking_enabled_here,
        links=built,
    )


def resolve_link_for_click(
    session: Session, organization_id: uuid.UUID, link_id: uuid.UUID | None
) -> OrganizationPublicLink | None:
    """Valida `link_id` recebido num evento de clique — nunca persiste um
    `link_id` que não pertence a esta organização (ver
    `services/public_page_events.py::record_event`)."""
    if link_id is None:
        return None
    return organization_public_link_repo.get(session, organization_id, link_id)
