import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.core.storage import (
    StorageBackend,
    build_professional_photo_key,
    require_storage_backend,
    validate_professional_photo_upload,
)
from nexasalon_api.models.enums import AuditAction, OrganizationStatus
from nexasalon_api.models.organization import Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService
from nexasalon_api.repositories import (
    audit_log_repo,
    branch_repo,
    professional_repo,
    professional_service_repo,
    service_repo,
    working_hours_repo,
)
from nexasalon_api.schemas.professional import (
    ProfessionalCreate,
    ProfessionalServiceItem,
    ProfessionalUpdate,
    WorkingHourItem,
)


def _assert_branch_in_org(
    session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID | None
) -> None:
    """Branch deve pertencer à mesma organização do profissional."""
    if branch_id is None:
        return
    if not branch_repo.exists(session, organization_id, branch_id):
        raise ValidationDomainError("branch_id não pertence a esta organização.")


def list_professionals(
    session: Session, organization_id: uuid.UUID, include_inactive: bool = False
) -> list[Professional]:
    return professional_repo.list_all(session, organization_id, include_inactive)


def get_professional(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID
) -> Professional:
    professional = professional_repo.get(session, organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")
    return professional


def create_professional(
    session: Session, organization_id: uuid.UUID, data: ProfessionalCreate
) -> Professional:
    # Serializa criações concorrentes do mesmo tenant. Sem o lock, duas
    # requisições simultâneas poderiam observar count=2 e ambas criar a
    # quarta vaga, furando o limite garantido pelo backend.
    organization = session.get(Organization, organization_id, with_for_update=True)
    if organization is None:
        raise NotFoundError("Organização não encontrada.")
    trial_is_active = (
        organization.status == OrganizationStatus.TRIAL
        and organization.trial_ends_at is not None
        and organization.trial_ends_at > datetime.now(timezone.utc)
    )
    if trial_is_active and organization.professional_limit is not None:
        current_count = professional_repo.count_all(session, organization_id)
        if current_count >= organization.professional_limit:
            raise ValidationDomainError(
                f"O período de teste permite no máximo {organization.professional_limit} profissionais."
            )
    _assert_branch_in_org(session, organization_id, data.branch_id)
    return professional_repo.create(session, organization_id, **data.model_dump())


def update_professional(
    session: Session,
    organization_id: uuid.UUID,
    professional_id: uuid.UUID,
    data: ProfessionalUpdate,
) -> Professional:
    professional = get_professional(session, organization_id, professional_id)
    _assert_branch_in_org(session, organization_id, data.branch_id)
    for field, value in data.model_dump().items():
        setattr(professional, field, value)
    return professional_repo.save(session, professional)


def upload_professional_photo(
    session: Session,
    organization_id: uuid.UUID,
    professional_id: uuid.UUID,
    *,
    storage: StorageBackend | None,
    content: bytes,
    content_type: str | None,
) -> Professional:
    """Etapa L, Bloco 3 — upload REAL de foto (substitui a UX de "cole a
    URL da foto"), reaproveitando INTEGRALMENTE a infraestrutura já usada
    pela logo do estabelecimento (`core/storage.py`, mesmo backend
    S3-compatível, mesma validação server-side de MIME/tamanho). Nunca
    grava base64 no banco — só a URL pública resultante em
    `Professional.photo_url`, substituindo o ponteiro anterior (o objeto
    antigo no bucket não é apagado — ver docstring de
    `build_professional_photo_key`)."""
    professional = get_professional(session, organization_id, professional_id)
    validate_professional_photo_upload(
        content_type=content_type, size_bytes=len(content)
    )
    backend = require_storage_backend(storage)
    key = build_professional_photo_key(professional_id, content_type)  # type: ignore[arg-type]
    photo_url = backend.upload(key=key, content=content, content_type=content_type)  # type: ignore[arg-type]
    professional.photo_url = photo_url
    return professional_repo.save(session, professional)


def set_professional_active(
    session: Session,
    organization_id: uuid.UUID,
    professional_id: uuid.UUID,
    is_active: bool,
) -> Professional:
    """Desativar não apaga o profissional nem seu histórico de
    atendimentos/comissões (FK é RESTRICT, não CASCADE)."""
    professional = get_professional(session, organization_id, professional_id)
    professional.is_active = is_active
    return professional_repo.save(session, professional)


def list_working_hours(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID
) -> list[WorkingHours]:
    get_professional(
        session, organization_id, professional_id
    )  # 404 se não existir/for de outra org
    return working_hours_repo.list_for_professional(
        session, organization_id, professional_id
    )


def replace_working_hours(
    session: Session,
    organization_id: uuid.UUID,
    professional_id: uuid.UUID,
    items: list[WorkingHourItem],
) -> list[WorkingHours]:
    get_professional(session, organization_id, professional_id)
    payload = [item.model_dump() for item in items]
    return working_hours_repo.replace_all(
        session, organization_id, professional_id, payload
    )


def list_professional_services(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID
) -> list[ProfessionalService]:
    get_professional(session, organization_id, professional_id)
    return professional_service_repo.list_for_professional(
        session, organization_id, professional_id
    )


def replace_professional_services(
    session: Session,
    organization_id: uuid.UUID,
    professional_id: uuid.UUID,
    items: list[ProfessionalServiceItem],
    *,
    user_id: uuid.UUID | None = None,
) -> list[ProfessionalService]:
    get_professional(session, organization_id, professional_id)

    # Etapa C2 — pendência registrada na C1: `replace_professional_services`
    # não gerava AuditLog nenhum. Como o endpoint SUBSTITUI o conjunto
    # inteiro (delete + insert, `replace_all`), o "antes" só existe se
    # lido ANTES de chamar `replace_all` — daqui pra frente cada linha
    # some do banco. Audita só o que de fato MUDOU (tipo ou valor de
    # comissão), por serviço — nunca um log genérico "algo mudou".
    before_by_service = {
        row.service_id: row
        for row in professional_service_repo.list_for_professional(
            session, organization_id, professional_id
        )
    }

    payload = []
    for item in items:
        # Profissional só pode receber serviços da MESMA organização —
        # reuso do repo já filtrado por organization_id: se o serviço for
        # de outra org, `get` simplesmente não encontra.
        if service_repo.get(session, organization_id, item.service_id) is None:
            raise ValidationDomainError(
                f"service_id {item.service_id} não pertence a esta organização (ou não existe)."
            )
        payload.append(item.model_dump())

    result = professional_service_repo.replace_all(
        session, organization_id, professional_id, payload
    )

    for row in result:
        prior = before_by_service.get(row.service_id)
        old_type = prior.commission_type if prior is not None else None
        old_value = prior.commission_value if prior is not None else None
        if old_type == row.commission_type and old_value == row.commission_value:
            continue
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=user_id,
            entity_type="professional_service",
            entity_id=row.id,
            action=AuditAction.CREATE if prior is None else AuditAction.UPDATE,
            old_values={
                "professional_id": str(professional_id),
                "service_id": str(row.service_id),
                "commission_type": old_type.value if old_type is not None else None,
                "commission_value": str(old_value) if old_value is not None else None,
            },
            new_values={
                "professional_id": str(professional_id),
                "service_id": str(row.service_id),
                "commission_type": row.commission_type.value
                if row.commission_type is not None
                else None,
                "commission_value": str(row.commission_value)
                if row.commission_value is not None
                else None,
            },
        )

    return result
