from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from nexasalon_api.api.deps import get_current_actor, get_db, require_permission
from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.storage import StorageBackend, get_storage_backend
from nexasalon_api.schemas.organization import LogoUploadRead, OrganizationRead, OrganizationUpdate
from nexasalon_api.services import organizations as organizations_service

router = APIRouter(prefix="/organization", tags=["organization"])

_manage = require_permission("organization.manage")


@router.get("", response_model=OrganizationRead, summary="Consultar a organização atual")
def get_current_organization(
    session: Session = Depends(get_db), actor: ActorContext = Depends(get_current_actor)
) -> OrganizationRead:
    """De propósito, sem `require_permission`: dado básico da própria
    organização (nome, slug, fuso, contato) que QUALQUER membro
    autenticado precisa pra a UI funcionar (branding, título da página
    etc.) — não é um dado sensível como financeiro ou configurações.
    Escrita em Organization (abaixo) tem sua própria checagem de
    `organization.manage` — ver docstring de `update_current_organization`."""
    org = organizations_service.get_current_organization(session, actor.organization_id)
    return OrganizationRead.model_validate(org)


@router.put(
    "",
    response_model=OrganizationRead,
    summary="Atualizar Informações do Estabelecimento",
)
def update_current_organization(
    payload: OrganizationUpdate,
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
) -> OrganizationRead:
    """Etapa D — "Informações do Estabelecimento". `organization.manage`
    já existia no catálogo de permissões (migration 0007), reusado aqui
    sem criar chave nova — reserva OWNER (ADMIN é explicitamente
    excluído no seed, ver docstring da migration 0007). O backend é
    sempre a autoridade final: esconder o formulário no frontend nunca
    substitui esta checagem."""
    org = organizations_service.update_organization(session, actor.organization_id, payload)
    return OrganizationRead.model_validate(org)


@router.post(
    "/logo",
    response_model=LogoUploadRead,
    summary="Upload da logo do estabelecimento",
)
async def upload_logo(
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
    actor: ActorContext = Depends(_manage),
    storage: StorageBackend | None = Depends(get_storage_backend),
) -> LogoUploadRead:
    """A logo do ESTABELECIMENTO — nunca substitui a marca NexaSalon na
    navegação do sistema (`components/layout/top-nav.tsx` no frontend
    não é tocado por esta etapa, de propósito). Content-type e tamanho
    são validados no backend (`core/storage.py::validate_logo_upload`)
    — nunca confia só no `accept` do `<input>` do frontend. Arquivo vai
    direto pro storage S3-compatível configurado (`core/storage.py`),
    nunca base64 no banco; só a URL final é persistida."""
    content = await file.read()
    org = organizations_service.upload_organization_logo(
        session,
        actor.organization_id,
        storage=storage,
        content=content,
        content_type=file.content_type,
    )
    return LogoUploadRead(logo_url=org.logo_url)
