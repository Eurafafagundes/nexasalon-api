import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.enums import PublicLinkKind, PublicPageButtonStyle
from nexasalon_api.models.organization import Organization
from nexasalon_api.models.organization_public_link import OrganizationPublicLink
from nexasalon_api.models.organization_public_page import OrganizationPublicPage
from nexasalon_api.repositories import (
    organization_public_link_repo,
    organization_public_page_repo,
    organization_repo,
)
from nexasalon_api.schemas.organization_public_page import (
    OrganizationPublicPageRead,
    OrganizationPublicPageUpdate,
    PublicLinkInput,
    PublicLinkRead,
)

_DEFAULT_PRIMARY_COLOR = "#7C3AED"
_DEFAULT_BACKGROUND_COLOR = "#FFFFFF"

# Textos padrão por canal nativo (item "texto padrão: ...") — usados
# quando `custom_label` não foi configurado, nunca persistidos (sempre
# calculados na leitura, pra uma mudança futura de texto padrão valer
# retroativamente pra quem nunca personalizou).
DEFAULT_PUBLIC_LINK_LABELS = {
    PublicLinkKind.BOOKING: "Agendar meu horário",
    PublicLinkKind.WHATSAPP: "Falar pelo WhatsApp",
    PublicLinkKind.INSTAGRAM: "Conheça nosso Instagram",
    PublicLinkKind.LOCATION: "Como chegar",
}

_NATIVE_KINDS = (PublicLinkKind.BOOKING, PublicLinkKind.WHATSAPP, PublicLinkKind.INSTAGRAM, PublicLinkKind.LOCATION)


def _virtual_native_link(kind: PublicLinkKind, organization: Organization) -> PublicLinkRead:
    """Linha NATIVA "fantasma" — devolvida só na LEITURA quando a config
    ainda não foi salva nenhuma vez (nenhuma linha real no banco).
    Pré-preenche (nunca vincula) WhatsApp/Instagram a partir do cadastro
    da Organization quando disponível — item "reutilizar... quando
    disponível, não duplicar desnecessariamente". Nunca grava nada."""
    return PublicLinkRead(
        id=None,
        kind=kind,
        is_visible=False,
        display_order=list(_NATIVE_KINDS).index(kind),
        custom_label=None,
        title=None,
        url=None,
        icon_key=None,
        whatsapp_number=organization.whatsapp if kind == PublicLinkKind.WHATSAPP else None,
        whatsapp_message=None,
        instagram_username=organization.instagram if kind == PublicLinkKind.INSTAGRAM else None,
        maps_url_override=None,
    )


def get_public_page_config(session: Session, organization_id: uuid.UUID) -> OrganizationPublicPageRead:
    organization = organization_repo.get(session, organization_id)
    if organization is None:
        raise NotFoundError("Organização não encontrada.")

    page = organization_public_page_repo.get(session, organization_id)
    links = organization_public_link_repo.list_for_org(session, organization_id)

    if links:
        link_reads = [PublicLinkRead.model_validate(link) for link in links]
    else:
        # Nenhuma linha real ainda — defaults em memória (nunca grava
        # numa leitura, mesmo padrão sparse de `CashRegisterConfig`).
        link_reads = [_virtual_native_link(kind, organization) for kind in _NATIVE_KINDS]

    return OrganizationPublicPageRead(
        is_enabled=page.is_enabled if page is not None else False,
        display_name=page.display_name if page is not None else None,
        short_description=page.short_description if page is not None else None,
        primary_color_hex=page.primary_color_hex if page is not None else _DEFAULT_PRIMARY_COLOR,
        background_color_hex=page.background_color_hex if page is not None else _DEFAULT_BACKGROUND_COLOR,
        button_style=page.button_style if page is not None else PublicPageButtonStyle.ROUNDED,
        updated_at=page.updated_at if page is not None else None,
        organization_name=organization.name,
        organization_logo_url=organization.logo_url,
        links=link_reads,
    )


def _apply_link_fields(link: OrganizationPublicLink, data: PublicLinkInput) -> None:
    link.is_visible = data.is_visible
    link.custom_label = data.custom_label
    if data.kind == PublicLinkKind.CUSTOM:
        link.title = data.title
        link.url = data.url
        link.icon_key = data.icon_key
    elif data.kind == PublicLinkKind.WHATSAPP:
        link.whatsapp_number = data.whatsapp_number
        link.whatsapp_message = data.whatsapp_message
    elif data.kind == PublicLinkKind.INSTAGRAM:
        link.instagram_username = data.instagram_username
    elif data.kind == PublicLinkKind.LOCATION:
        link.maps_url_override = data.maps_url_override


def update_public_page(
    session: Session,
    organization_id: uuid.UUID,
    payload: OrganizationPublicPageUpdate,
    *,
    updated_by: uuid.UUID | None,
) -> OrganizationPublicPageRead:
    """Salva config + a lista COMPLETA de links numa única transação
    (mesmo commit do request, nunca um segundo round-trip). Links
    NATIVOS são upsert por `kind` (nunca deletados); links PERSONALIZADOS
    ausentes do payload (por `id`) são removidos — "adicionar/editar/
    remover". `display_order` real persistido é SEMPRE a posição no
    array do payload (`enumerate`), nunca o que o cliente mandar como
    número — "nunca confia na ordem crua do cliente, reindexa no
    backend"."""
    organization = organization_repo.get(session, organization_id)
    if organization is None:
        raise NotFoundError("Organização não encontrada.")

    custom_ids_in_payload = {item.id for item in payload.links if item.kind == PublicLinkKind.CUSTOM and item.id}
    existing_links = organization_public_link_repo.list_for_org(session, organization_id)
    existing_by_kind: dict[PublicLinkKind, OrganizationPublicLink] = {}
    existing_custom_by_id: dict[uuid.UUID, OrganizationPublicLink] = {}
    for existing_link in existing_links:
        if existing_link.kind == PublicLinkKind.CUSTOM:
            existing_custom_by_id[existing_link.id] = existing_link
        else:
            existing_by_kind[existing_link.kind] = existing_link

    # Remove custom links que saíram do payload — nativos NUNCA são
    # removidos, mesmo que (por engano do cliente) não venham na lista
    # (a validação do schema já exige os 4 presentes).
    for link_id, stale_link in existing_custom_by_id.items():
        if link_id not in custom_ids_in_payload:
            organization_public_link_repo.delete(session, stale_link)

    for position, item in enumerate(payload.links):
        link: OrganizationPublicLink | None
        if item.kind == PublicLinkKind.CUSTOM:
            link = existing_custom_by_id.get(item.id) if item.id else None
        else:
            link = existing_by_kind.get(item.kind)
        is_new = link is None
        if link is None:
            link = OrganizationPublicLink(organization_id=organization_id, kind=item.kind)
        _apply_link_fields(link, item)
        link.updated_by = updated_by
        # `display_order` real: posição no array, nunca um valor confiado
        # do cliente — reindexado aqui ANTES de qualquer flush (ver
        # abaixo). A constraint única `(organization_id, display_order)`
        # é DEFERRABLE INITIALLY DEFERRED (migration 0060): reordenar
        # troca o valor de várias linhas na mesma transação, e só
        # precisa ficar consistente no COMMIT, nunca a cada UPDATE
        # individual.
        link.display_order = position
        if is_new:
            # `session.add` puro — NUNCA `organization_public_link_repo.
            # create` aqui (faria flush imediato, antes de todos os
            # campos acima estarem atribuídos, violando o NOT NULL de
            # `display_order`/`kind` num INSERT incompleto). Um único
            # flush em lote, depois do laço, insere tudo de uma vez já
            # completo.
            session.add(link)

    session.flush()

    page = organization_public_page_repo.get(session, organization_id)
    if page is None:
        page = OrganizationPublicPage(organization_id=organization_id)
        organization_public_page_repo.create(session, page)

    page.is_enabled = payload.is_enabled
    page.display_name = payload.display_name
    page.short_description = payload.short_description
    page.primary_color_hex = payload.primary_color_hex
    page.background_color_hex = payload.background_color_hex
    page.button_style = payload.button_style
    page.updated_by = updated_by
    organization_public_page_repo.save(session, page)

    return get_public_page_config(session, organization_id)
