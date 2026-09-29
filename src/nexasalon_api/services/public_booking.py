"""Leituras do Agendamento Online público (Etapa K) — organização,
serviços/profissionais elegíveis e disponibilidade. A CRIAÇÃO do
agendamento em si mora em `services/appointments.py::create_public_appointment`
(reaproveita a validação de agenda existente) — este módulo só resolve o
que a página pública precisa MOSTRAR antes da confirmação.

Segurança (item explícito do pedido): cada função aqui devolve só o
necessário pro fluxo de agendamento — nenhuma consulta neste módulo
inclui campo financeiro/interno/de estoque/de usuário.
"""
import uuid
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError
from nexasalon_api.models.organization import Branch
from nexasalon_api.models.professional import Professional
from nexasalon_api.models.service import ProfessionalService, Service, ServiceCategory
from nexasalon_api.repositories import (
    branch_repo,
    organization_repo,
    professional_repo,
    professional_service_repo,
    service_category_repo,
    service_repo,
)
from nexasalon_api.services import availability
from nexasalon_api.services.availability import AvailabilitySlot


def is_online_booking_eligible(
    professional: Professional, service: Service, link: ProfessionalService | None
) -> bool:
    """Regra CANÔNICA de elegibilidade de uma combinação
    profissional+serviço pro Agendamento Online — as 4 condições
    precisam ser TODAS verdadeiras:

    - `Professional.allow_online_booking`
    - `Service.allow_online_booking`
    - `ProfessionalService.is_active` (o vínculo existe e está ativo —
      "realiza o serviço")
    - `ProfessionalService.allow_online_booking` (esta combinação
      específica está habilitada online)

    Usada em TODO ponto do fluxo PÚBLICO que precisa decidir "esta
    combinação pode aparecer/ser reservada online" —
    `list_public_professionals` (abaixo) e
    `services/appointments.py::create_public_appointment`/
    `create_public_appointment_for_customer` — pra nunca haver uma
    segunda cópia divergente desta regra.

    NUNCA chamada por `services/appointments.py::_build_item_snapshot`
    (compartilhado com a agenda interna) — a agenda interna continua
    ignorando `ProfessionalService.allow_online_booking` por completo,
    só `is_active` importa lá (item explícito do pedido)."""
    if link is None or not link.is_active or not link.allow_online_booking:
        return False
    return professional.allow_online_booking and service.allow_online_booking


def _lead_time_bounds(session: Session, organization_id: uuid.UUID) -> tuple[datetime, datetime]:
    """`earliest_start`/`latest_start` pra `compute_availability`, a
    partir de `Organization.online_booking_min_lead_minutes`/
    `online_booking_max_lead_days` (Etapa K, já existentes) — os MESMOS
    campos já aplicados na CONFIRMAÇÃO
    (`services/appointments.py::_assert_online_booking_lead_time`), só
    que agora também na LISTAGEM (correção: antes só a confirmação
    respeitava a antecedência, então a listagem oferecia horários que a
    confirmação ia recusar em seguida). `now` em UTC — comparável
    direto com `AvailabilitySlot.start_at`, que também é UTC-aware."""
    organization = organization_repo.get(session, organization_id)
    now = datetime.now(timezone.utc)
    earliest = availability.earliest_public_booking_start(organization, now)
    latest = now + timedelta(days=organization.online_booking_max_lead_days)
    return earliest, latest


def get_default_branch(session: Session, organization_id: uuid.UUID) -> Branch:
    """A página pública desta primeira versão não pede unidade (o fluxo
    do pedido é só "Serviço -> Profissional -> Horário -> Dados ->
    Confirmação", sem passo de unidade) — usa a primeira unidade ATIVA
    da organização. Suficiente pro caso comum (uma unidade); múltiplas
    unidades por organização com página pública própria por unidade fica
    fora do escopo desta etapa."""
    branches = branch_repo.list_all(session, organization_id)
    if not branches:
        raise NotFoundError("Esta organização ainda não tem nenhuma unidade cadastrada.")
    return branches[0]


def list_public_services(
    session: Session,
    organization_id: uuid.UUID,
    *,
    category_id: uuid.UUID | None = None,
    uncategorized_only: bool = False,
) -> list[Service]:
    """Só serviços ATIVOS e com `allow_online_booking=true` — a mesma
    flag já existente em `Service` (sem migration nova), "serviços
    habilitados para online" do pedido.

    Etapa M — "Categoria → Serviço": `category_id` filtra por categoria
    escolhida; `uncategorized_only` filtra pela pseudo-categoria
    "Outros serviços" (`category_id IS NULL`, ver
    `list_public_categories`). Os dois nunca são usados juntos."""
    services = [s for s in service_repo.list_all(session, organization_id) if s.allow_online_booking]
    if uncategorized_only:
        return [s for s in services if s.category_id is None]
    if category_id is not None:
        return [s for s in services if s.category_id == category_id]
    return services


def list_public_categories(
    session: Session, organization_id: uuid.UUID
) -> tuple[list[ServiceCategory], bool]:
    """Categorias ATIVAS com pelo menos 1 serviço elegível online (item
    explícito "categoria vazia não aparece") + um flag `has_uncategorized`
    pra rota montar a pseudo-categoria sintética "Outros serviços" quando
    aplicável (ver `api/v1/public_booking.py::list_categories` — nunca
    uma linha real em `service_categories`, então não pertence aqui)."""
    services = list_public_services(session, organization_id)
    category_ids = {s.category_id for s in services if s.category_id is not None}
    has_uncategorized = any(s.category_id is None for s in services)
    if not category_ids:
        return [], has_uncategorized
    all_categories = service_category_repo.list_all(session, organization_id)
    categories = [c for c in all_categories if c.id in category_ids]
    return categories, has_uncategorized


def list_public_professionals(
    session: Session, organization_id: uuid.UUID, *, service_id: uuid.UUID | None = None
) -> list[Professional]:
    """Só profissionais ATIVOS, `allow_online_booking=true` e com agenda
    própria (`has_schedule=true` — um profissional sem agenda não tem
    disponibilidade nenhuma pra oferecer). Sem `service_id`, é só isso —
    não há vínculo nenhum pra cruzar ainda (ex.: listagem inicial antes de
    escolher serviço).

    Com `service_id`, a elegibilidade passa a ser a regra CANÔNICA de
    `is_online_booking_eligible` (inclui, pela primeira vez aqui,
    `Service.allow_online_booking` — antes esta função nunca checava a
    flag do SERVIÇO, só a do profissional; um `service_id` de um serviço
    não público já filtra pra lista vazia agora, em vez de vazar quem o
    executa)."""
    professionals = [p for p in professional_repo.list_all(session, organization_id) if p.has_schedule]
    if service_id is None:
        return [p for p in professionals if p.allow_online_booking]
    service = service_repo.get(session, organization_id, service_id)
    if service is None:
        return []
    links_by_professional = {
        link.professional_id: link
        for link in professional_service_repo.list_for_service(session, organization_id, service_id)
    }
    return [
        p for p in professionals if is_online_booking_eligible(p, service, links_by_professional.get(p.id))
    ]


def get_public_availability(
    session: Session,
    organization_id: uuid.UUID,
    *,
    branch_id: uuid.UUID,
    service_id: uuid.UUID,
    professional_id: uuid.UUID | None,
    target_date: date_type,
) -> list[AvailabilitySlot]:
    """Reaproveita INTEGRALMENTE `services/availability.py::compute_availability`
    — a mesma jornada/intervalo/bloqueio/duração/conflito/timezone do
    agendamento interno, "não criar uma segunda lógica de agenda" (item
    explícito do pedido). `professional_id=None` ("Qualquer profissional")
    é a ÚNICA parte nova: união dos horários de todos os elegíveis pro
    serviço, deduplicados por horário de início — o profissional real só
    é resolvido na confirmação (`services/appointments.py::
    resolve_public_professional_for_slot`)."""
    earliest_start, latest_start = _lead_time_bounds(session, organization_id)

    if professional_id is not None:
        return availability.compute_availability(
            session,
            organization_id,
            branch_id=branch_id,
            professional_id=professional_id,
            service_id=service_id,
            target_date=target_date,
            earliest_start=earliest_start,
            latest_start=latest_start,
        )

    merged: dict = {}
    for professional in list_public_professionals(session, organization_id, service_id=service_id):
        for slot in availability.compute_availability(
            session,
            organization_id,
            branch_id=branch_id,
            professional_id=professional.id,
            service_id=service_id,
            target_date=target_date,
            earliest_start=earliest_start,
            latest_start=latest_start,
        ):
            merged.setdefault(slot.start_at, slot)
    return sorted(merged.values(), key=lambda s: s.start_at)
