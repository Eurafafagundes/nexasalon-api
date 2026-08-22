"""Cálculo de disponibilidade — Etapa 3A.

Recebe organização (contexto autenticado) + unidade + profissional +
serviço + data, devolve os horários livres. A grade (15/30min) é só o
alinhamento dos horários de INÍCIO oferecidos — nunca encurta/estica a
duração real do serviço: cada slot oferecido garante que
`[start, start + duração_efetiva)` cabe inteiro num intervalo livre.

Fontes de indisponibilidade combinadas:
  1. Fora da jornada (`WorkingHours` do profissional, por dia da semana —
     pode haver mais de uma linha por dia = turno partido).
  2. `ScheduleBlock` que atinja o profissional (escopo profissional,
     unidade ou organização) sobrepondo a janela.
  3. `AppointmentItem` existentes do profissional cujo status "ocupa"
     agenda (ver `appointment_item_repo.OCCUPYING_STATUSES` — a MESMA
     regra usada pela checagem de conflito ao criar/editar um
     agendamento, sem duplicação: SCHEDULED/CONFIRMED/WAITING/
     IN_PROGRESS ocupam; FINISHED/CANCELLED/NO_SHOW não).
"""
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta
from datetime import timezone as timezone_type
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.organization import Organization
from nexasalon_api.repositories import (
    appointment_item_repo,
    branch_repo,
    organization_repo,
    professional_repo,
    professional_service_repo,
    schedule_block_repo,
    service_repo,
)

ALLOWED_SLOT_MINUTES = frozenset({15, 30})


@dataclass(frozen=True)
class AvailabilitySlot:
    start_at: datetime
    end_at: datetime


def earliest_public_booking_start(organization: Organization, now: datetime) -> datetime:
    """"Permitir agendamento para o mesmo dia" (Etapa M, Configurações >
    Agendamento Online > Regras de agendamento) — fonte ÚNICA usada
    tanto pela LISTAGEM de horários (`services/public_booking.py::
    _lead_time_bounds`) quanto pela CONFIRMAÇÃO
    (`services/appointments.py::_assert_online_booking_lead_time`), pra
    nunca a listagem oferecer o que a confirmação recusaria.

    `online_booking_same_day_enabled=True` (default, comportamento
    IDÊNTICO ao de antes desta função existir): `now +
    online_booking_min_lead_minutes` — se for baixo o bastante, hoje
    continua aparecendo. `False`: hoje NUNCA aparece, mesmo com
    antecedência mínima pequena — o primeiro instante possível é amanhã
    00:00 no fuso da organização (ainda recortado por
    `effective_working_windows_utc` como qualquer outro horário)."""
    if organization.online_booking_same_day_enabled:
        return now + timedelta(minutes=organization.online_booking_min_lead_minutes)
    tz = ZoneInfo(organization.timezone)
    tomorrow_local_date = (now.astimezone(tz) + timedelta(days=1)).date()
    return datetime.combine(tomorrow_local_date, time.min, tzinfo=tz).astimezone(timezone_type.utc)


def effective_timezone(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> ZoneInfo:
    branch = branch_repo.get(session, organization_id, branch_id)
    organization = organization_repo.get(session, organization_id)
    tz_name = (branch.timezone if branch and branch.timezone else None) or organization.timezone
    return ZoneInfo(tz_name)


def effective_duration_and_price(service, professional_service) -> tuple[int, Decimal]:
    duration = (
        professional_service.duration_override_minutes
        if professional_service and professional_service.duration_override_minutes
        else service.default_duration_minutes
    )
    price = (
        professional_service.price_override
        if professional_service and professional_service.price_override is not None
        else service.default_price
    )
    return duration, Decimal(str(price))


def working_windows_utc(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID, target_date: date_type, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    from nexasalon_api.repositories import working_hours_repo

    our_weekday = (target_date.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> 0=domingo..6=sábado
    rows = working_hours_repo.list_for_professional(session, organization_id, professional_id)
    windows = []
    for wh in rows:
        if not wh.is_active or wh.weekday != our_weekday:
            continue
        start_local = datetime.combine(target_date, wh.start_time, tzinfo=tz)
        end_local = datetime.combine(target_date, wh.end_time, tzinfo=tz)
        windows.append((start_local, end_local))
    windows.sort(key=lambda w: w[0])
    return windows


def _intersect_windows(
    windows: list[tuple[datetime, datetime]], allowed: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Recorta cada janela de `windows` ao que sobrar depois de
    intersectar com `allowed` — o oposto de `_subtract_busy` (que
    remove interseção; isto MANTÉM só a interseção). Usado pra aplicar
    o horário de funcionamento (`allowed`) por cima da jornada do
    profissional (`windows`), item explícito "estabelecimento aberto ∩
    profissional trabalha"."""
    result: list[tuple[datetime, datetime]] = []
    for w_start, w_end in windows:
        for a_start, a_end in allowed:
            start = max(w_start, a_start)
            end = min(w_end, a_end)
            if start < end:
                result.append((start, end))
    return result


def effective_working_windows_utc(
    session: Session, organization_id: uuid.UUID, professional_id: uuid.UUID, target_date: date_type, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Fonte ÚNICA de "quando este profissional pode ser agendado neste
    dia" — jornada do profissional (`working_windows_utc`) já recortada
    pelo horário de funcionamento do estabelecimento (camada SUPERIOR,
    item explícito do pedido: "Horário do estabelecimento é a camada
    superior"). Usado tanto por `compute_availability` (Agenda interna,
    Novo Agendamento e, através dele, Agendamento Online) quanto por
    `services/appointments.py::_assert_within_working_hours` (validação
    na criação/edição de um agendamento) — nunca dois cálculos
    diferentes, então a disponibilidade nunca depende de qual usuário
    está logado nem de qual tela chamou."""
    from nexasalon_api.services import business_hours as business_hours_service

    windows = working_windows_utc(session, organization_id, professional_id, target_date, tz)
    if not windows:
        return []

    business_window = business_hours_service.get_window_utc(session, organization_id, target_date, tz)
    if business_window is None:
        return windows  # organização não configurou horário de funcionamento ainda — sem restrição extra
    if not business_window:
        return []  # estabelecimento fechado neste dia — ninguém disponível, independente da jornada

    return _intersect_windows(windows, business_window)


def _subtract_busy(
    windows: list[tuple[datetime, datetime]], busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Subtrai os intervalos ocupados de cada janela livre — o clássico
    "furar buracos" numa lista de intervalos, sem nenhuma lib externa."""
    free: list[tuple[datetime, datetime]] = []
    busy_sorted = sorted(busy, key=lambda b: b[0])
    for win_start, win_end in windows:
        cursor = win_start
        for busy_start, busy_end in busy_sorted:
            if busy_end <= cursor or busy_start >= win_end:
                continue  # não sobrepõe esta janela
            if busy_start > cursor:
                free.append((cursor, min(busy_start, win_end)))
            cursor = max(cursor, busy_end)
            if cursor >= win_end:
                break
        if cursor < win_end:
            free.append((cursor, win_end))
    return free


def _generate_slots(
    free_intervals: list[tuple[datetime, datetime]], duration_minutes: int, slot_minutes: int, tz: ZoneInfo
) -> list[AvailabilitySlot]:
    slots: list[AvailabilitySlot] = []
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=slot_minutes)
    for free_start, free_end in free_intervals:
        # Alinha o primeiro candidato à grade absoluta do dia (00:00 do
        # fuso efetivo), não ao início da janela — grade previsível
        # (sempre em :00/:15/:30/:45, por ex.), não deslocada por onde a
        # jornada por acaso começa.
        day_start = free_start.replace(hour=0, minute=0, second=0, microsecond=0)
        offset_minutes = (free_start - day_start).total_seconds() / 60
        aligned_offset = -(-offset_minutes // slot_minutes) * slot_minutes  # ceil pro próximo múltiplo
        candidate = day_start + timedelta(minutes=aligned_offset)
        while candidate + duration <= free_end:
            slots.append(AvailabilitySlot(start_at=candidate, end_at=candidate + duration))
            candidate += step
    return slots


def compute_availability(
    session: Session,
    organization_id: uuid.UUID,
    *,
    branch_id: uuid.UUID,
    professional_id: uuid.UUID,
    service_id: uuid.UUID,
    target_date: date_type,
    slot_minutes: int = 15,
    earliest_start: datetime | None = None,
    latest_start: datetime | None = None,
) -> list[AvailabilitySlot]:
    """`earliest_start`/`latest_start` (ambos `None` por padrão — sem
    nenhuma restrição extra, comportamento IDÊNTICO ao de antes desta
    correção) recortam os slots retornados por horário de início. Só o
    Agendamento Online público passa esses dois parâmetros hoje
    (`services/public_booking.py::get_public_availability`) — "nunca
    retornar data/horário passado, nem violar a antecedência mín/máx
    configurada" (item de correção). A Agenda interna continua sem
    nenhuma restrição de "agora"/antecedência aqui (a recepção pode
    legitimamente agendar num horário que já passou, ex. registrar um
    atendimento feito sem hora marcada — essa é uma decisão de produto
    já existente, não alterada por esta correção)."""
    if slot_minutes not in ALLOWED_SLOT_MINUTES:
        raise ValidationDomainError("slot_minutes deve ser 15 ou 30.")

    if not branch_repo.exists(session, organization_id, branch_id):
        raise NotFoundError("Unidade não encontrada.")

    professional = professional_repo.get(session, organization_id, professional_id)
    if professional is None:
        raise NotFoundError("Profissional não encontrado.")
    if professional.branch_id is not None and professional.branch_id != branch_id:
        raise ValidationDomainError("Este profissional não atende nesta unidade.")

    service = service_repo.get(session, organization_id, service_id)
    if service is None:
        raise NotFoundError("Serviço não encontrado.")

    professional_service = professional_service_repo.get_for_pair(
        session, organization_id, professional_id, service_id
    )
    if professional_service is None or not professional_service.is_active:
        raise ValidationDomainError("Este profissional não executa este serviço.")

    duration_minutes, _price = effective_duration_and_price(service, professional_service)
    tz = effective_timezone(session, organization_id, branch_id)

    windows = effective_working_windows_utc(session, organization_id, professional_id, target_date, tz)
    if not windows:
        return []

    range_start = min(w[0] for w in windows)
    range_end = max(w[1] for w in windows)

    busy: list[tuple[datetime, datetime]] = []
    for item in appointment_item_repo.list_busy_for_professional_on_range(
        session, organization_id, professional_id=professional_id, range_start=range_start, range_end=range_end
    ):
        busy.append((item.start_at, item.end_at))
    for block in schedule_block_repo.list_overlapping(
        session,
        organization_id,
        professional_id=professional_id,
        branch_id=branch_id,
        range_start=range_start,
        range_end=range_end,
    ):
        busy.append((block.start_at, block.end_at))

    free_intervals = _subtract_busy(windows, busy)
    slots = _generate_slots(free_intervals, duration_minutes, slot_minutes, tz)
    if earliest_start is not None:
        slots = [s for s in slots if s.start_at >= earliest_start]
    if latest_start is not None:
        slots = [s for s in slots if s.start_at <= latest_start]
    return slots
