import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.core.storage import (
    StorageBackend,
    build_logo_key,
    require_storage_backend,
    validate_logo_upload,
)
from nexasalon_api.models.organization import BusinessHours, Organization
from nexasalon_api.repositories import business_hours_repo, organization_repo
from nexasalon_api.schemas.organization import BusinessHourItem, OrganizationUpdate


def get_current_organization(session: Session, organization_id: uuid.UUID) -> Organization:
    org = organization_repo.get(session, organization_id)
    if org is None:
        # só acontece se o ator DEV ONLY apontar pra uma org que não existe
        # mais — não deveria ocorrer em uso normal.
        raise NotFoundError("Organização do contexto atual não encontrada.")
    return org


def _slug_taken_by_other_org(session: Session, slug: str, organization_id: uuid.UUID) -> bool:
    """Checagem de unicidade GLOBAL de slug (a URL pública `/agendar/<slug>`
    não é escopada por organização — dois estabelecimentos não podem
    dividir a mesma URL). Sob RLS normal (autenticado, `app.current_org_id`
    já apontando pra `organization_id`), a policy `tenant_isolation` de
    `organizations` só deixa este SELECT enxergar a PRÓPRIA organização —
    por isso liga o mesmo flag de sessão da rota pública
    (`public_booking_lookup`, migration 0028) só pelo tempo desta consulta,
    e desliga de novo logo em seguida, dentro da MESMA transação. Mesmo
    espírito de `SET LOCAL app.allow_overlap` em
    `services/appointments.py::_maybe_allow_overlap`: um flag transacional
    estreito, nunca um bypass geral de RLS."""
    session.execute(text("SELECT set_config('app.public_booking_lookup', 'true', true)"))
    try:
        existing = organization_repo.get_by_slug(session, slug)
    finally:
        session.execute(text("SELECT set_config('app.public_booking_lookup', 'false', true)"))
    return existing is not None and existing.id != organization_id


def update_organization(session: Session, organization_id: uuid.UUID, data: OrganizationUpdate) -> Organization:
    """Escrita gated por `organization.manage` na rota (ver
    `api/v1/organizations.py`) — o service não reconfirma permissão,
    só confia no `organization_id` do ator já autorizado (mesmo padrão
    de todo o resto do domínio: a checagem de RBAC mora inteiramente na
    dependency da rota)."""
    org = get_current_organization(session, organization_id)
    # `exclude_unset` (não full-replace como `ClientUpdate`): diferente
    # de Client, `Organization.timezone` é NOT NULL com um default de
    # negócio — um payload que não menciona `timezone` não pode viver
    # zerando a coluna. Campo OMITIDO do payload mantém o valor atual;
    # campo enviado explicitamente como `null` (ex.: "remover CNPJ
    # cadastrado") limpa o valor — é o comportamento certo pra um
    # formulário de configurações que pode ser salvo por seção.
    payload = data.model_dump(exclude_unset=True)

    # `slug` é a ÚNICA exceção à regra "null limpa o valor" acima — é
    # NOT NULL/UNIQUE no banco (é a URL pública do Agendamento Online),
    # nunca pode ser removido, só trocado por outro slug válido.
    if "slug" in payload:
        new_slug = payload["slug"]
        if new_slug is None:
            raise ValidationDomainError("Slug não pode ser removido — informe um novo slug.")
        if new_slug != org.slug and _slug_taken_by_other_org(session, new_slug, organization_id):
            raise ConflictError("Este slug já está em uso por outra organização.")

    for field, value in payload.items():
        setattr(org, field, value)
    return organization_repo.save(session, org)


def upload_organization_logo(
    session: Session,
    organization_id: uuid.UUID,
    *,
    storage: StorageBackend | None,
    content: bytes,
    content_type: str | None,
) -> Organization:
    """Valida (server-side, nunca confia no frontend) e envia pro
    storage configurado (`core/storage.py`) — nunca grava base64 no
    banco, só a URL pública resultante em `Organization.logo_url`."""
    validate_logo_upload(content_type=content_type, size_bytes=len(content))
    backend = require_storage_backend(storage)
    org = get_current_organization(session, organization_id)
    key = build_logo_key(organization_id, content_type)  # type: ignore[arg-type]
    logo_url = backend.upload(key=key, content=content, content_type=content_type)  # type: ignore[arg-type]
    org.logo_url = logo_url
    return organization_repo.save(session, org)


def list_business_hours(session: Session, organization_id: uuid.UUID) -> list[BusinessHours]:
    """Vazio = organização ainda não configurou horário de
    funcionamento (ver docstring de `models/organization.py::
    BusinessHours` — compatibilidade retroativa, sem restrição
    nenhuma até a primeira gravação)."""
    return business_hours_repo.list_for_organization(session, organization_id)


def replace_business_hours(
    session: Session, organization_id: uuid.UUID, items: list[BusinessHourItem]
) -> list[BusinessHours]:
    get_current_organization(session, organization_id)  # 404 se a org não existir
    payload = [item.model_dump() for item in items]
    return business_hours_repo.replace_all(session, organization_id, payload)
