"""Máquina de estados do `Appointment.status`.

Etapa 3A (+ ajuste na rodada de polimento da Agenda, item "8 status
oficiais"). Duas operações, de propósito SEPARADAS (não uma função
genérica "mude para qualquer status"):

  - `next_status(current, target)`: transições operacionais do dia a dia
    (confirmar, colocar em espera, iniciar atendimento, finalizar,
    marcar como pago, marcar não-comparecimento). NÃO inclui `CANCELLED`
    como destino — cancelar é uma operação com significado e permissão
    próprios (`agenda.cancel`, ver `services/appointments.py`), roteada
    só pelo endpoint dedicado `POST /appointments/{id}/cancel`, nunca
    pelo PATCH genérico de status. Isso evita duplicar a checagem de
    permissão "isto é um cancelamento, exige agenda.cancel" dentro de
    uma função que também serve pra outras transições.
  - `assert_cancellable(current)`: valida que dá pra cancelar a partir
    do status atual.

Fluxo principal: SCHEDULED -> CONFIRMED -> WAITING -> IN_PROGRESS ->
FINISHED -> PAID. Saídas alternativas: CANCELLED (via endpoint próprio)
e NO_SHOW. `PAID` é, por ora, só mais um destino manual válido a partir
de `FINISHED` — a definição AUTOMÁTICA desse status pela Comanda/Caixa
fica pra uma etapa futura (não implementada aqui).

Regra geral: nenhuma transição sai de um estado TERMINAL (`PAID`,
`CANCELLED`, `NO_SHOW`) — inclusive o exemplo citado explicitamente,
`FINISHED -> IN_PROGRESS`, nunca é permitido.
"""
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.models.enums import AppointmentStatus

TERMINAL_STATUSES = frozenset(
    {AppointmentStatus.PAID, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW}
)

# Transições operacionais válidas (fora do cancelamento, que tem seu
# próprio fluxo — ver docstring do módulo).
_ALLOWED_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.SCHEDULED: frozenset(
        {AppointmentStatus.CONFIRMED, AppointmentStatus.WAITING, AppointmentStatus.IN_PROGRESS, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.CONFIRMED: frozenset(
        {AppointmentStatus.WAITING, AppointmentStatus.IN_PROGRESS, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.WAITING: frozenset({AppointmentStatus.IN_PROGRESS, AppointmentStatus.NO_SHOW}),
    AppointmentStatus.IN_PROGRESS: frozenset({AppointmentStatus.FINISHED}),
    AppointmentStatus.FINISHED: frozenset({AppointmentStatus.PAID}),
    AppointmentStatus.PAID: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
}

# De quais estados dá pra cancelar. Terminal -> não dá (inclusive
# cancelar algo já cancelado, ou um FINISHED/NO_SHOW).
_CANCELLABLE_FROM = frozenset(
    {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.WAITING,
        AppointmentStatus.IN_PROGRESS,
    }
)


def next_status(current: AppointmentStatus, target: AppointmentStatus) -> AppointmentStatus:
    """Valida e devolve `target` se a transição `current -> target` for
    permitida; levanta `ValidationDomainError` caso contrário."""
    if target == AppointmentStatus.CANCELLED:
        raise ValidationDomainError(
            "Use POST /appointments/{id}/cancel para cancelar — não o PATCH de status genérico."
        )
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValidationDomainError(
            f"Transição de status inválida: '{current.value}' -> '{target.value}'."
        )
    return target


def assert_cancellable(current: AppointmentStatus) -> None:
    if current not in _CANCELLABLE_FROM:
        raise ValidationDomainError(f"Não é possível cancelar um agendamento com status '{current.value}'.")
