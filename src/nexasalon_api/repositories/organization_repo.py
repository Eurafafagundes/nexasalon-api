import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexasalon_api.models.organization import Organization


def get(session: Session, organization_id: uuid.UUID) -> Organization | None:
    return session.get(Organization, organization_id)


def get_by_slug(session: Session, slug: str) -> Organization | None:
    """Busca por slug — Etapa K (Agendamento Online público). Sob RLS
    normal (autenticado, `app.current_org_id` já setado) só enxerga a
    PRÓPRIA organização, então isto só devolve uma organização de outro
    tenant quando o chamador ligou deliberadamente o flag de sessão
    `app.public_booking_lookup` (ver `api/deps.py::get_public_context` e
    `services/organizations.py`, migration 0028) — nunca por acidente."""
    stmt = select(Organization).where(Organization.slug == slug)
    return session.scalars(stmt).first()


def create(session: Session, **fields) -> Organization:
    """Usado pelo CLI de bootstrap (`cli/bootstrap_owner.py`, Etapa 3C) e
    pelo signup público (`services/signup.py`) — ÚNICO ponto canônico de
    criação de `Organization` na aplicação (nunca instanciar `Organization`
    direto num service/rota nova).

    `online_booking_auto_confirm` nasce `False` ("Agendado", nunca
    "Confirmado" automaticamente) pra toda organização NOVA — item
    "Agendamento Online deve nascer como Agendado" — SEM migration: o
    `server_default` da coluna continua `true` só por compatibilidade
    com quem já tinha o toggle ligado antes desta mudança (organizações
    existentes mantêm o valor que já estava gravado; só quem cria uma
    organização a partir de agora recebe o novo padrão). Quem chamar
    `create(...)` passando `online_booking_auto_confirm` explicitamente
    continua no controle — nunca sobrescreve um valor informado."""
    fields.setdefault("online_booking_auto_confirm", False)
    organization = Organization(**fields)
    session.add(organization)
    session.flush()
    return organization


def save(session: Session, organization: Organization) -> Organization:
    """`organization` já deve ter os atributos alterados via `setattr`
    (ver `services/organizations.py::update_organization`) — mesmo
    padrão de `client_repo.save`."""
    session.flush()
    return organization
