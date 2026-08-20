import uuid

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.core.storage import (
    StorageBackend,
    build_logo_key,
    require_storage_backend,
    validate_logo_upload,
)
from nexasalon_api.models.organization import Organization
from nexasalon_api.repositories import organization_repo
from nexasalon_api.schemas.organization import OrganizationUpdate


def get_current_organization(session: Session, organization_id: uuid.UUID) -> Organization:
    org = organization_repo.get(session, organization_id)
    if org is None:
        # só acontece se o ator DEV ONLY apontar pra uma org que não existe
        # mais — não deveria ocorrer em uso normal.
        raise NotFoundError("Organização do contexto atual não encontrada.")
    return org


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
    for field, value in data.model_dump(exclude_unset=True).items():
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
