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
from nexasalon_api.models.service import Service
from nexasalon_api.repositories import (
    branch_repo,
    organization_repo,
    professional_repo,
    professional_service_repo,
    service_repo,
)
from nexasalon_api.services import availability
from nexasalon_api.services.availability import AvailabilitySlot


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
    earliest = now + timedelta(minutes=organization.online_booking_min_lead_minutes)
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


def list_public_services(session: Session, organization_id: uuid.UUID) -> list[Service]:
    """Só serviços ATIVOS e com `allow_online_booking=true` — a mesma
    flag já existente em `Service` (sem migration nova), "serviços
    habilitados para online" do pedido."""
    return [s for s in service_repo.list_all(session, organization_id) if s.allow_online_booking]


def list_public_professionals(
    session: Session, organization_id: uuid.UUID, *, service_id: uuid.UUID | None = None
) -> list[Professional]:
    """Só profissionais ATIVOS, `allow_online_booking=true` e com agenda
    própria (`has_schedule=true` — um profissional sem agenda não tem
    disponibilidade nenhuma pra oferecer). Se `service_id` for informado,
    filtra ainda pelos que de fato executam esse serviço
    (`ProfessionalService` ativo) — "profissionais habilitados para
    online" do pedido, já cruzado com o serviço escolhido no passo
    anterior do fluxo."""
    eligible = [
        p
        for p in professional_repo.list_all(session, organization_id)
        if p.allow_online_booking and p.has_schedule
    ]
    if service_id is None:
        return eligible
    links = professional_service_repo.list_for_service(session, organization_id, service_id)
    linked_ids = {link.professional_id for link in links if link.is_active}
    return [p for p in eligible if p.id in linked_ids]


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
