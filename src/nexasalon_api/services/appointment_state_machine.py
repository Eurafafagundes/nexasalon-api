"""Máquina de estados do `Appointment.status`.

Etapa 3A (+ ajuste na rodada de polimento da Agenda, item "8 status
oficiais") + rodada "status livre + Comanda desacoplada da sequência".

A partir desta rodada, os 6 status OPERACIONAIS (`SCHEDULED`,
`CONFIRMED`, `WAITING`, `IN_PROGRESS`, `FINISHED`, `NO_SHOW`) formam um
grafo LIVRE: qualquer um pode virar qualquer outro manualmente (avançar
OU regredir — ex.: `CONFIRMED -> SCHEDULED`, `IN_PROGRESS -> WAITING`,
`NO_SHOW -> CONFIRMED`), desde que quem operar tenha `agenda.edit`. Não
existe mais uma sequência linear obrigatória — a Agenda é operação do
dia a dia, o usuário pode corrigir um clique errado ou uma mudança de
plano sem ficar preso a uma ordem fixa.

Duas operações continuam FORA desse grafo livre, cada uma com
significado e trava próprios:

  - `CANCELLED`: nunca é destino do PATCH genérico — só chega lá via
    `POST /appointments/{id}/cancel` (`assert_cancellable`), que exige
    `agenda.cancel`. Uma vez cancelado, é terminal: nenhuma transição
    manual sai dali (não existe "descancelar" nesta rodada).
  - `PAID`: nunca é destino do PATCH genérico de status — é
    EXCLUSIVAMENTE uma consequência automática de fechar a Comanda com
    pagamento registrado (`services/orders.py::close_order` ->
    `mark_paid`, abaixo). Isso é proposital: "Pago" reflete um
    pagamento de fato existente (`Payment`/`Order` fechada), nunca um
    clique isolado na Agenda — item "não misture status operacional
    com status financeiro". Uma vez pago, também é terminal pro grafo
    livre: `next_status` recusa QUALQUER transição manual a partir de
    `PAID` (avanço ou regressão), porque desfazer um pagamento exige um
    fluxo financeiro explícito de estorno/reversão que esta rodada não
    implementa — tentar "voltar" um `PAID` por engano no PATCH genérico
    devia dar erro, não silenciosamente desfazer uma cobrança.

Resumo das travas de `next_status` (usado pelo PATCH genérico,
`services/appointments.py::update_status`):
  - `target == CANCELLED` -> sempre recusa (use o endpoint dedicado).
  - `target == PAID` -> sempre recusa (isso vem da Comanda, não do
    PATCH genérico — use `mark_paid` via `close_order`).
  - `current in (PAID, CANCELLED)` -> sempre recusa (terminal pro
    grafo livre; `PAID` só sai via estorno, não implementado; `CANCELLED`
    não tem "descancelar").
  - Qualquer outro par operacional -> permitido nos dois sentidos.
"""
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.models.enums import AppointmentStatus

# Status operacionais: participam do grafo livre de `next_status`. Fora
# daqui ficam `PAID` (só automático, via Comanda) e `CANCELLED` (só via
# endpoint dedicado) — ver docstring do módulo.
OPERATIONAL_STATUSES = frozenset(
    {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.WAITING,
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.FINISHED,
        AppointmentStatus.NO_SHOW,
    }
)

# Nenhuma transição manual sai destes — nem pra outro status
# operacional, nem entre si. `PAID` só se desfaz com um fluxo de
# estorno (não implementado); `CANCELLED` não tem "descancelar".
_TERMINAL_FOR_MANUAL_CHANGE = frozenset({AppointmentStatus.PAID, AppointmentStatus.CANCELLED})

# De quais estados dá pra cancelar. Terminal -> não dá (inclusive
# cancelar algo já cancelado, ou um FINISHED/NO_SHOW/PAID).
_CANCELLABLE_FROM = frozenset(
    {
        AppointmentStatus.SCHEDULED,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.WAITING,
        AppointmentStatus.IN_PROGRESS,
    }
)


def next_status(current: AppointmentStatus, target: AppointmentStatus) -> AppointmentStatus:
    """Valida uma transição MANUAL (PATCH genérico de status). Livre
    entre os 6 status operacionais, nos dois sentidos; recusa `target`
    em (`CANCELLED`, `PAID`) e recusa qualquer transição a partir de um
    `current` terminal (`PAID`, `CANCELLED`) — ver docstring do módulo.
    Levanta `ValidationDomainError` caso contrário."""
    if target == AppointmentStatus.CANCELLED:
        raise ValidationDomainError(
            "Use POST /appointments/{id}/cancel para cancelar — não o PATCH de status genérico."
        )
    if target == AppointmentStatus.PAID:
        raise ValidationDomainError(
            "'Pago' não é um status que se marca manualmente — finalize o pagamento pela Comanda "
            "(POST /orders/{id}/close), que promove o agendamento automaticamente."
        )
    if current in _TERMINAL_FOR_MANUAL_CHANGE:
        if current == AppointmentStatus.PAID:
            raise ValidationDomainError(
                "Agendamento já pago — reverter exige um fluxo financeiro de estorno (não implementado "
                "nesta versão), não é uma mudança manual de status."
            )
        raise ValidationDomainError("Agendamento cancelado não pode ter o status alterado.")
    # target sempre está em OPERATIONAL_STATUSES aqui (únicos valores
    # restantes do enum depois de excluir CANCELLED/PAID acima) — grafo
    # livre, qualquer operacional -> qualquer operacional.
    return target


def mark_paid(current: AppointmentStatus) -> AppointmentStatus:
    """Promove pra `PAID` — usado SÓ por `services/orders.py::close_order`
    ao registrar o pagamento que fecha a Comanda. Item "não condicione a
    Comanda a o atendimento ter passado por todos os status": qualquer
    status operacional pode virar `PAID` diretamente (não exige ter
    passado por `FINISHED` antes). Recusa se já está `PAID` (não paga de
    novo — a comanda já fechada também barra isso, esta é uma segunda
    trava no nível do Appointment) ou `CANCELLED` (não faz sentido
    cobrar um agendamento cancelado)."""
    if current == AppointmentStatus.PAID:
        raise ValidationDomainError("Agendamento já está pago.")
    if current == AppointmentStatus.CANCELLED:
        raise ValidationDomainError("Agendamento cancelado não pode ser marcado como pago.")
    return AppointmentStatus.PAID


def assert_cancellable(current: AppointmentStatus) -> None:
    if current not in _CANCELLABLE_FROM:
        raise ValidationDomainError(f"Não é possível cancelar um agendamento com status '{current.value}'.")
