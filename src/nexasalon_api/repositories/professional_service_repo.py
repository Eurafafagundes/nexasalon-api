import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import ProfessionalService, Service


def list_for_professional(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID
) -> list[ProfessionalService]:
    # Join até Professional pra filtrar organization_id de novo aqui —
    # ProfessionalService não tem a coluna própria (é junção pura), mas
    # a leitura não deve depender só do invariante de escrita (que só
    # deixa vincular professional/service da mesma org). Defesa em
    # profundidade também na leitura, não só na escrita.
    stmt = (
        select(ProfessionalService)
        .join(Professional, Professional.id == ProfessionalService.professional_id)
        .where(
            ProfessionalService.professional_id == professional_id,
            Professional.organization_id == organization_id,
        )
    )
    return list(session.scalars(stmt).all())


def list_for_service(
    session: Session, organization_id: uuid.UUID, service_id: uuid.UUID
) -> list[ProfessionalService]:
    stmt = (
        select(ProfessionalService)
        .join(Service, Service.id == ProfessionalService.service_id)
        .where(
            ProfessionalService.service_id == service_id,
            Service.organization_id == organization_id,
        )
    )
    return list(session.scalars(stmt).all())


def replace_all(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID, items: list[dict]
) -> list[ProfessionalService]:
    """Substitui o conjunto inteiro de serviços do profissional — mesma
    semântica de PUT idempotente do working_hours_repo. `organization_id`
    aqui é redundante com a checagem já feita no service layer antes de
    chamar isso (professional já foi validado como sendo da org), mas
    mantém a mesma defesa em profundidade por consistência com o resto
    do módulo — nunca deleta/insere sem reafirmar de quem é o dado."""
    session.execute(
        delete(ProfessionalService)
        .where(ProfessionalService.professional_id == professional_id)
        .where(
            ProfessionalService.professional_id.in_(
                select(Professional.id).where(
                    Professional.id == professional_id, Professional.organization_id == organization_id
                )
            )
        )
    )
    created = []
    for item in items:
        row = ProfessionalService(professional_id=professional_id, **item)
        session.add(row)
        created.append(row)
    session.flush()
    return created
