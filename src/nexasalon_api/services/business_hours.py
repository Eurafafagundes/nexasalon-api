"""Horário de funcionamento do estabelecimento (Etapa M) — só resolve a
janela permitida do dia; quem combina com a jornada do profissional é
`services/availability.py::effective_working_windows_utc` (único ponto
usado por Agenda interna, Novo Agendamento e Agendamento Online — nunca
uma segunda lógica de agenda)."""
import uuid
from datetime import date as date_type
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from nexasalon_api.repositories import business_hours_repo

WEEKDAY_LABELS = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"]


def get_window_utc(
    session: Session, organization_id: uuid.UUID, target_date: date_type, tz: ZoneInfo
) -> list[tuple[datetime, datetime]] | None:
    """`None` = organização ainda não configurou horário de
    funcionamento nenhum — SEM restrição (compatibilidade retroativa,
    comportamento idêntico a antes de `BusinessHours` existir).
    `[]` = configurado e FECHADO neste dia da semana — nenhum horário
    possível, pra ninguém, independente da jornada do profissional.
    `[(start, end)]` = configurado e ABERTO nesse intervalo."""
    rows = business_hours_repo.list_for_organization(session, organization_id)
    if not rows:
        return None

    our_weekday = (target_date.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> 0=domingo..6=sábado
    row = next((r for r in rows if r.weekday == our_weekday), None)
    if row is None or not row.is_open or row.start_time is None or row.end_time is None:
        return []

    start_local = datetime.combine(target_date, row.start_time, tzinfo=tz)
    end_local = datetime.combine(target_date, row.end_time, tzinfo=tz)
    return [(start_local, end_local)]
