"""Comanda/Pagamento — primeira versão funcional (item 3 da rodada
"Agenda visual/status, jornada por profissional e Comanda").

Fluxo: `create_order` copia os itens do Appointment pra dentro da
comanda (preço editável por linha, sem tocar no catálogo nem no
snapshot original) -> `update_item_price` edita uma linha (auditado) ->
`close_order` registra o(s) pagamento(s) e PROMOVE o Appointment pra
`paid` automaticamente via `appointments_service.mark_paid`.

Rodada "status livre + Comanda desacoplada da sequência": a Comanda
pode ser aberta e fechada em QUALQUER status operacional do Appointment
(não exige ter passado por `finished` antes — `mark_paid` só recusa se
o Appointment já está `paid` ou `cancelled`). "Pago" nunca é um clique
manual na Agenda: só existe como consequência de `close_order`.

`close_order` agora também exige, por lançamento de pagamento, um
`cash_register_id` de um caixa ABERTO da mesma organização (item
"Caixa Diário" — pagamento nunca é registrado sem caixa selecionado,
reaproveitando `services/cash_register.py::assert_register_open_and_in_org`,
nunca abre um caixa sozinho).

Etapa C (Estoque ↔ Comanda) acrescenta, sem alterar nada do que já
existia acima:

  - `add_product_item`/`remove_product_item`/`update_product_item` —
    produto na comanda ABERTA. Remover antes do fechamento nunca gera
    baixa (não existe movimentação pra desfazer — nenhuma foi criada
    ainda, a baixa só acontece no fechamento).
  - `update_order_item` (renomeado de `update_item_price`, mesmo
    contrato HTTP) — agora também aceita editar `duration_minutes`,
    cada campo auditado independentemente.
  - `close_order` passa a: (a) somar produtos no total da comanda,
    nunca duas contas de faturamento diferentes; (b) travar a linha da
    comanda (`order_repo.get_for_update`) ANTES de checar o status —
    torna um retry de fechamento seguro (a segunda tentativa, seja
    sequencial ou concorrente, sempre enxerga `status=closed` já
    commitado e recusa com 409, nunca refaz a baixa); (c) baixar
    estoque de cada linha de produto (`stock_service.record_sale_movement`,
    reason=SALE, `order_id` preenchido) DENTRO da mesma transação da
    request — estoque insuficiente levanta `ValidationDomainError`,
    que aborta a transação inteira (nenhum pagamento fica "pendurado",
    a comanda continua `OPEN`, ver `api/deps.py::get_db`)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.client_privacy import client_can_view_contact
from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.models.enums import (
    AuditAction,
    CashRegisterStatus,
    OrderProductItemKind,
    OrderStatus,
    StockMovementDirection,
)
from nexasalon_api.models.order import Order
from nexasalon_api.repositories import (
    appointment_repo,
    audit_log_repo,
    branch_repo,
    cash_register_repo,
    client_repo,
    order_item_repo,
    order_product_item_repo,
    order_repo,
    organization_repo,
    payment_repo,
    product_repo,
    professional_repo,
    service_repo,
    stock_movement_repo,
    user_repo,
)
from nexasalon_api.schemas.order import (
    ConsolidatedOrderClose,
    OrderCancel,
    OrderClose,
    OrderConsumptionCorrection,
    OrderItemUpdate,
    OrderObservationUpdate,
    OrderProductItemCreate,
    OrderProductItemUpdate,
    OrderReceiptRead,
    OrderReopen,
)
from nexasalon_api.services import appointments as appointments_service
from nexasalon_api.services import cash_register as cash_register_service
from nexasalon_api.services import commissions as commissions_service
from nexasalon_api.services import order_totals
from nexasalon_api.services import payment_fees as payment_fees_service
from nexasalon_api.services import stock as stock_service


def _get_order_or_404(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = order_repo.get(session, organization_id, order_id)
    if order is None:
        raise NotFoundError("Comanda não encontrada.")
    return order


def _get_order_for_update_or_404(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    order = order_repo.get_for_update(session, organization_id, order_id)
    if order is None:
        raise NotFoundError("Comanda não encontrada.")
    return order


def _reload(session: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    session.flush()
    order = order_repo.get(session, organization_id, order_id)
    assert order is not None
    return order


def create_order(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Order:
    organization_id = actor.organization_id
    appointment = appointment_repo.get(session, organization_id, appointment_id)
    if appointment is None:
        raise NotFoundError("Agendamento não encontrado.")
    if order_repo.get_by_appointment(session, organization_id, appointment_id) is not None:
        raise ConflictError("Este agendamento já tem uma comanda.")
    if not appointment.items:
        raise ValidationDomainError("Agendamento sem nenhum serviço — não é possível abrir comanda.")

    # Etapa H (`Financeiro > Caixa > Configurações do Caixa`) — "exigir
    # caixa aberto para criar Comanda" (padrão ON) e "bloquear
    # operações se existir caixa aberto de dia anterior" (padrão ON),
    # ambos aplicados no backend, nunca só desabilitando botão.
    cash_register_service.assert_operational_prerequisites(session, actor, appointment.branch_id, purpose="order")

    # Corrida (item "Abrir Comanda — eliminar duplicidade"): dois cliques
    # rápidos podem passar os dois pelo check `get_by_appointment` acima
    # antes de qualquer um commitar. `begin_nested()` abre um SAVEPOINT —
    # se o INSERT abaixo violar a unique parcial `uq_orders_appointment_
    # id_active` (o "perdedor" da corrida), só este savepoint reverte
    # (nunca a transação inteira/os GUCs de RLS já setados por
    # `api/deps.py::get_db`); devolvemos a comanda que "venceu" em vez de
    # propagar erro — POST /orders fica genuinamente idempotente sob
    # concorrência real, não só no caminho feliz check-then-create.
    # Rodada "Comanda, Auditoria, Status Personalizado e Estoque por Peso"
    # (drawer unificado): a Order ainda não existe no momento em que o
    # atendente digita a observação da visita no drawer de criação do
    # agendamento — esse texto fica temporariamente em `Appointment.
    # notes` só até aqui. É uma cópia ÚNICA, feita uma vez, no instante
    # em que a Order nasce; a partir deste ponto `Order.observation` é a
    # ÚNICA fonte de verdade (editada via `update_observation`,
    # concorrência otimista por `observation_version`) — nunca voltamos a
    # ler/sincronizar de `Appointment.notes` depois disso, evitando a
    # duplicação perigosa de duas fontes de verdade divergentes.
    order_observation = (appointment.notes or "").strip() or None
    try:
        with session.begin_nested():
            order = order_repo.create(
                session,
                organization_id,
                appointment_id=appointment.id,
                branch_id=appointment.branch_id,
                client_id=appointment.client_id,
                created_by=actor.user_id,
                observation=order_observation,
            )
            # Item de performance ("quick win 4") — resolve TODOS os
            # serviços e profissionais dos itens do agendamento em duas
            # queries em lote (`WHERE id IN (...)`), antes do loop, em
            # vez de 1 `get` de cada por item (`1 + 2N` -> `1 + 2`).
            # Mesmíssima resolução de nome/fallback de antes — só troca
            # ONDE a busca acontece, nunca o resultado.
            services_by_id = {
                s.id: s for s in service_repo.list_by_ids(session, organization_id, {i.service_id for i in appointment.items})
            }
            professionals_by_id = {
                p.id: p
                for p in professional_repo.list_by_ids(
                    session, organization_id, [i.professional_id for i in appointment.items]
                )
            }
            for item in appointment.items:
                # Copia o snapshot do AppointmentItem 1:1 na abertura — a
                # partir daqui os dois vivem independentes (editar o preço
                # da comanda não altera o item original do agendamento,
                # nem vice-versa). `service_name`/`professional_name` são
                # capturados AGORA (item "snapshot histórico") — nem
                # `AppointmentItem` nem `Service`/`Professional` guardam
                # esse nome já congelado, e ler o catálogo atual depois
                # mudaria como uma venda antiga aparece se o serviço for
                # renomeado ou o profissional sair.
                service = services_by_id.get(item.service_id)
                professional = professionals_by_id.get(item.professional_id)
                order_item_repo.create(
                    session,
                    organization_id,
                    order_id=order.id,
                    appointment_item_id=item.id,
                    service_id=item.service_id,
                    professional_id=item.professional_id,
                    duration_minutes=item.duration_minutes,
                    price=item.price,
                    service_name=service.name if service is not None else "Serviço removido",
                    professional_name=professional.name if professional is not None else "Profissional removido",
                )
    except IntegrityError:
        existing = order_repo.get_by_appointment(session, organization_id, appointment_id)
        if existing is None:
            raise
        return existing
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.CREATE,
        new_values={"appointment_id": str(appointment_id), "items_count": len(appointment.items)},
    )
    return _reload(session, organization_id, order.id)


def get_order(session: Session, actor: ActorContext, order_id: uuid.UUID) -> Order:
    return _get_order_or_404(session, actor.organization_id, order_id)


def list_orders(
    session: Session,
    actor: ActorContext,
    *,
    status: OrderStatus | None = None,
    client_id: uuid.UUID | None = None,
    professional_id: uuid.UUID | None = None,
    order_number: int | None = None,
    date_from=None,
    date_to=None,
) -> list[Order]:
    """Financeiro > Comandas (Abertas/Finalizadas, item 13/14)."""
    return order_repo.list_for_org(
        session, actor.organization_id, status=status, client_id=client_id, professional_id=professional_id,
        order_number=order_number, date_from=date_from, date_to=date_to,
    )


def get_order_by_appointment(session: Session, actor: ActorContext, appointment_id: uuid.UUID) -> Order | None:
    return order_repo.get_by_appointment(session, actor.organization_id, appointment_id)


def get_related_orders(session: Session, actor: ActorContext, order: Order) -> list[Order]:
    """Outras comandas NÃO canceladas da MESMA cliente no MESMO dia
    operacional do agendamento desta comanda (fuso da unidade de cada
    comanda) — item "Comandas relacionadas" (Etapa I). Exemplo do
    pedido: Amanda -> Manutenção com Duda; Amanda -> Progressiva com
    Ianka, mesmo dia, profissionais diferentes — as duas comandas
    aparecem relacionadas uma da outra. Nunca inclui a própria `order`."""
    organization_id = actor.organization_id
    appointment = appointment_repo.get(session, organization_id, order.appointment_id)
    if appointment is None or appointment.starts_at is None:
        return []

    candidates = order_repo.list_active_for_client(session, organization_id, order.client_id)
    appointments_by_id = {
        item.id: item
        for item in appointment_repo.list_by_ids(
            session, organization_id, [candidate.appointment_id for candidate in candidates]
        )
    }
    branches_by_id = {
        branch.id: branch
        for branch in branch_repo.list_by_ids(
            session, organization_id, {order.branch_id, *(candidate.branch_id for candidate in candidates)}
        )
    }
    organization = organization_repo.get(session, organization_id)
    assert organization is not None

    def _timezone(branch_id: uuid.UUID) -> ZoneInfo:
        branch = branches_by_id.get(branch_id)
        return ZoneInfo((branch.timezone if branch and branch.timezone else None) or organization.timezone)

    target_day = appointment.starts_at.astimezone(_timezone(order.branch_id)).date()
    related: list[Order] = []
    for candidate in candidates:
        if candidate.id == order.id:
            continue
        c_appointment = appointments_by_id.get(candidate.appointment_id)
        if c_appointment is None or c_appointment.starts_at is None:
            continue
        c_tz = _timezone(candidate.branch_id)
        if c_appointment.starts_at.astimezone(c_tz).date() == target_day:
            related.append(candidate)
    return related


def get_order_related(session: Session, actor: ActorContext, order_id: uuid.UUID) -> tuple[Order, list[Order], str]:
    """Monta `GET /orders/{id}/related` — devolve a comanda, as
    relacionadas e o nome da cliente (pra montar o texto discreto "Esta
    cliente possui N comandas hoje" no frontend)."""
    order = _get_order_or_404(session, actor.organization_id, order_id)
    related = get_related_orders(session, actor, order)
    client = client_repo.get(session, actor.organization_id, order.client_id)
    client_name = client.name if client is not None else "Cliente"
    return order, related, client_name


def get_order_receipt(session: Session, actor: ActorContext, order_id: uuid.UUID) -> OrderReceiptRead:
    """Comprovante de Atendimento (Etapa D) — ver docstring de
    `schemas/order.py::OrderReceiptRead` pro raciocínio completo
    (snapshot-only, nunca NF/NFS-e, sem observações internas). Só
    comandas FECHADAS emitem comprovante — item do pedido "em uma
    Comanda fechada/paga, disponibilizar 'Imprimir comprovante'"; uma
    comanda aberta ainda não tem pagamento registrado pra mostrar."""
    order = _get_order_or_404(session, actor.organization_id, order_id)
    if order.status != OrderStatus.CLOSED:
        raise ValidationDomainError("Comprovante só está disponível para comandas fechadas.")
    client = client_repo.get(session, actor.organization_id, order.client_id)
    if client is None:
        raise NotFoundError("Cliente da comanda não encontrado.")
    organization = organization_repo.get(session, actor.organization_id)
    if organization is None:
        raise NotFoundError("Organização não encontrada.")
    can_view_contact = client_can_view_contact(actor.permissions)
    return OrderReceiptRead.build(order, client, organization, can_view_contact=can_view_contact)


def update_order_item(
    session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID, data: OrderItemUpdate
) -> Order:
    """Edita `price` e/ou `duration_minutes` de uma linha de SERVIÇO —
    cada campo alterado gera sua PRÓPRIA linha de auditoria. Nunca
    escreve de volta em `AppointmentItem`/`Service` (ver docstring do
    módulo) — "duração" aqui é só o snapshot local da comanda, então
    editar não reescreve retroativamente nenhum relatório/KPI que já
    tenha sido calculado em cima do `AppointmentItem` original."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível editar uma linha de comanda aberta.")
    item = next((i for i in order.items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Item da comanda não encontrado.")

    changes: dict[str, tuple[str, str]] = {}
    if data.price is not None and data.price != item.price:
        changes["price"] = (str(item.price), str(data.price))
        item.price = data.price
    if data.duration_minutes is not None and data.duration_minutes != item.duration_minutes:
        changes["duration_minutes"] = (str(item.duration_minutes), str(data.duration_minutes))
        item.duration_minutes = data.duration_minutes

    if not changes:
        return _reload(session, organization_id, order_id)

    session.flush()
    # Uma linha de AuditLog POR CAMPO alterado (item explícito "preço
    # anterior, preço novo, usuário, data/hora" — os dois últimos já
    # vêm de `AuditLog.user_id`/`created_at`) — nunca uma linha só
    # misturando os dois campos, pra o histórico deixar claro qual
    # mudança aconteceu quando o usuário edita só um dos dois.
    for field, (old_value, new_value) in changes.items():
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=actor.user_id,
            entity_type="order_item",
            entity_id=item.id,
            action=AuditAction.UPDATE,
            old_values={field: old_value},
            new_values={field: new_value, "change_type": f"manual_{field}_edit", "order_id": str(order_id)},
        )
    return _reload(session, organization_id, order_id)


# Nome anterior, mantido como alias — nenhuma rota nem teste externo
# precisa saber que foi renomeado.
update_item_price = update_order_item


def update_observation(
    session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderObservationUpdate
) -> Order:
    """Edita a observação da comanda — item "Comanda — Observação +
    Auditoria". DELIBERADAMENTE não guarda `order.status != OPEN` (única
    exceção a essa regra neste módulo): a observação é conteúdo
    operacional, não financeiro, e continua editável com a comanda
    FECHADA sem reabrir nem tocar em nenhum estado de pagamento/estoque.

    Auditoria DEDICADA (nunca `order.updated_at`, que também muda por
    pagamento/produto/status): `observation_updated_at`/`_by`/`_by_name`
    guardam só a ÚLTIMA edição (sem histórico de versões — item
    explícito "não precisamos de histórico de todas as versões nesta
    rodada"). Também emite um `AuditLog` (mesmo idioma do resto deste
    módulo), que preserva o histórico completo de edições pra quem
    precisar investigar depois, mesmo sem expor isso na UI desta rodada.

    Concorrência: `expected_observation_version` OBRIGATÓRIO — contador
    inteiro explícito (nasce em `0`, "nunca editada" é um valor real,
    nunca `NULL`). Recusa com `ConflictError` (409) sempre que não
    bater com `order.observation_version` atual, ANTES de qualquer
    outra decisão (inclusive antes do atalho "nada mudou" logo abaixo)
    — mesmo quando o texto enviado coincide por acaso com o valor
    atual, um chamador com versão desatualizada precisa recarregar
    primeiro. Isto cobre também o caso antes descoberto na auditoria
    "última correção pré-push": duas edições concorrentes partindo
    AMBAS de uma observação nunca editada (`version=0`) — a primeira
    grava e vira `version=1`; a segunda, ainda com `expected=0`, recusa
    com 409 em vez de sobrescrever silenciosamente (um timestamp `NULL`
    inicial não distinguia essas duas situações)."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)

    if data.expected_observation_version != order.observation_version:
        raise ConflictError("Esta observação foi alterada por outra pessoa. Atualize os dados antes de salvar novamente.")

    old_observation = order.observation
    # String vazia (ou só espaços) vira `None` — "Nenhuma observação
    # registrada" precisa de um estado limpo, não uma string vazia
    # persistida (mesmo raciocínio de nunca inventar dado — aqui o
    # inverso: nunca fingir que existe observação quando o usuário só
    # apagou o texto).
    new_observation = data.observation.strip() or None
    if new_observation == old_observation:
        return _reload(session, organization_id, order_id)

    actor_user = user_repo.get(session, actor.user_id)
    now = datetime.now(timezone.utc)

    order.observation = new_observation
    order.observation_updated_at = now
    order.observation_updated_by = actor.user_id
    order.observation_updated_by_name = actor_user.name if actor_user is not None else None
    order.observation_version = order.observation_version + 1
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.UPDATE,
        old_values={"observation": old_observation},
        new_values={"observation": new_observation, "change_type": "observation_edit"},
    )
    return _reload(session, organization_id, order_id)


def cancel_order(session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderCancel) -> Order:
    """Cancela uma comanda `OPEN` criada por engano (Etapa F, item
    "Cancelar/Excluir Comanda") — NUNCA apaga a linha; ela sai das
    operações abertas (`get_by_appointment` passa a ignorá-la) mas
    continua em `GET /orders`/histórico pra sempre.

    Mesmo lock de `close_order` (`get_for_update`, `SELECT ... FOR
    UPDATE`) ANTES de checar o status — mesma proteção de retry: uma
    segunda tentativa de cancelamento (ou um cancelamento correndo
    junto com um fechamento) sempre enxerga o estado JÁ decidido pela
    primeira, nunca cancela duas vezes nem cancela uma comanda que
    acabou de ser fechada por outra requisição.

    Só aceita `status == OPEN` — e só `OPEN` estrutural, sem
    `Payment`/baixa de estoque, existe nesta versão (as duas coisas só
    nascem dentro de `close_order`, na mesma transação que vira o
    status pra `closed`), então a checagem de status sozinha já garante
    "sem pagamento, sem baixa definitiva, sem fechamento" (item
    explícito do pedido). Nunca aceita cancelar uma comanda já
    `closed` — pedido explícito: `CLOSED` precisa passar por
    `reopen_order` (exige `orders.reopen`, permission separada e mais
    sensível) ANTES de poder ser cancelada — nunca um cancelamento
    direto que pulasse a reversão financeira. Já `cancelled` é só
    idempotência (segunda tentativa da mesma ação).

    Rodada "Reabertura e Cancelamento de Comandas": se a comanda tinha
    um Appointment vinculado, ele também é cancelado (libera o horário,
    some da grade operacional — ver `agenda-grid.tsx`, que já filtra
    `cancelled` client-side) — nunca APAGADO, continua no histórico.
    Ver `appointments_service.cancel_appointment_for_order_cancel` pro
    porquê disso NUNCA falha a operação inteira (idempotente, aceita
    `FINISHED` também)."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ConflictError(
            "Só é possível cancelar uma comanda aberta e sem pagamento/fechamento — "
            "esta já foi fechada ou cancelada. Se ela já foi paga, reabra a comanda antes de cancelar."
        )

    order.status = OrderStatus.CANCELLED
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.UPDATE,
        old_values={"status": "open"},
        new_values={"status": "cancelled", "change_type": "cancel", "reason": data.reason},
    )
    appointments_service.cancel_appointment_for_order_cancel(session, actor, order.appointment_id)
    return _reload(session, organization_id, order_id)


def reopen_order(session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderReopen) -> Order:
    """Rodada "Reabertura e Cancelamento de Comandas" — desfaz o
    fechamento de uma comanda `CLOSED`, voltando pra `OPEN` (fluxo
    completo: `CLOSED -> reopen -> OPEN -> cancel -> CANCELLED`, nunca
    um cancelamento direto de uma comanda fechada — ver `cancel_order`).
    Exige `orders.reopen` (checado na rota) — permission SEPARADA de
    `orders.cancel`, deliberadamente mais restrita (só OWNER/ADMIN por
    padrão, migration 0045): reabrir desfaz uma venda JÁ RECEBIDA, uma
    operação bem mais sensível que cancelar uma comanda ainda sem
    pagamento.

    NUNCA apaga/edita o `Payment` original nem o `StockMovement`
    original (ledger append-only, mesmo raciocínio de todo o resto do
    domínio) — marca o pagamento como revertido
    (`Payment.reversed_at`/`reversed_by`) e gera um movimento de
    estoque COMPENSATÓRIO (mesmo mecanismo de `correct_consumption`),
    nunca reescreve histórico. Faturamento/Extrato/Comissão já páram de
    contar sozinhos assim que `Order.status` deixa de ser `CLOSED`
    (todos filtram por isso) — a única coisa que NÃO se corrige sozinha
    é o resumo do Caixa (lê `Payment` direto por `cash_register_id`,
    sem olhar pro status da Order — por isso `Payment.reversed_at`
    existe, ver `services/cash_register.py::build_summary`).

    DUAS travas ANTES de reverter qualquer coisa (falha rápido, nunca
    reversão parcial):

      1. Caixa relacionado a algum pagamento já FECHADO — bloqueia.
         Reverter um pagamento cujo caixa já foi conferido/fechado
         mudaria silenciosamente uma reconciliação física já feita
         (`CashRegister.expected_amount`/`counted_amount`/`difference`
         são fotos congeladas no fechamento do caixa, nunca
         recalculadas depois) — pedido explícito do usuário pra nunca
         permitir isso silenciosamente.
      2. Comissão de algum serviço já LIQUIDADA (`OrderItem.
         commission_settlement_id` preenchido) — bloqueia. Um
         `CommissionSettlement` é imutável por design (documentado em
         `models/commission.py`, sem rota de edição/exclusão) — dinheiro
         já pode ter sido efetivamente repassado ao profissional; desfazer
         isso é uma correção administrativa (`CommissionAdjustment` na
         próxima liquidação), não algo que a reabertura da comanda deva
         tentar resolver sozinha.

    Mesmo lock (`get_for_update`) ANTES de checar status de
    `close_order`/`cancel_order` — retry-safe, nunca reabre duas vezes."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.CLOSED:
        raise ConflictError("Só é possível reabrir uma comanda finalizada (fechada).")

    active_payments = [p for p in order.payments if p.reversed_at is None]

    # Trava 1 — qualquer caixa relacionado já fechado bloqueia a
    # reabertura inteira (nunca reversão parcial: ou reverte tudo, ou
    # nada).
    register_ids = {p.cash_register_id for p in active_payments}
    for register_id in register_ids:
        register = cash_register_repo.get(session, organization_id, register_id)
        if register is None or register.status != CashRegisterStatus.OPEN:
            raise ValidationDomainError(
                "Não é possível reabrir: o caixa relacionado a um dos pagamentos desta comanda já está "
                "fechado. Procure o fluxo administrativo de ajuste/estorno para corrigir o caixa fechado "
                "antes de reabrir esta comanda."
            )

    # Trava 2 — comissão já liquidada (paga ao profissional) bloqueia.
    if any(item.commission_settlement_id is not None for item in order.items):
        raise ValidationDomainError(
            "Não é possível reabrir: a comissão de um dos serviços desta comanda já foi incluída numa "
            "liquidação de pagamento ao profissional. Faça o ajuste manual em Comissões antes de reabrir."
        )

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None
    now = datetime.now(timezone.utc)

    # Reverte pagamentos — nunca apaga/edita a linha original.
    for payment in active_payments:
        payment.reversed_at = now
        payment.reversed_by = actor.user_id
        payment.reversed_by_name = actor_name
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=actor.user_id,
            entity_type="payment",
            entity_id=payment.id,
            action=AuditAction.UPDATE,
            old_values={"reversed": False},
            new_values={
                "reversed": True, "change_type": "reopen_reversal", "order_id": str(order_id),
                "amount": str(payment.amount), "cash_register_id": str(payment.cash_register_id),
                "reason": data.reason,
            },
        )

    # Reverte a baixa de estoque de cada linha de produto já baixada —
    # movimento COMPENSATÓRIO (IN, reason=ADJUSTMENT), nunca edita o
    # StockMovement original. Volta `stock_movement_id` pra `NULL`: é
    # o que faz a comanda reaberta se comportar como qualquer comanda
    # `OPEN` de verdade (permite editar/remover a linha de novo — ver
    # `remove_product_item`, que exige exatamente essa invariante — e
    # gera uma baixa NOVA no próximo fechamento, com a quantidade que
    # sobreviver até lá).
    for product_item in order.product_items:
        if product_item.stock_movement_id is None:
            continue
        movement = stock_service.record_consumption_correction(
            session,
            actor,
            product_id=product_item.product_id,
            branch_id=order.branch_id,
            order_id=order.id,
            quantity=product_item.quantity,
            direction=StockMovementDirection.IN,
            observation=(
                f"Reabertura da Comanda #{order.order_number} — devolve ao estoque a baixa de "
                f"'{product_item.product_name}' feita no fechamento anterior."
            ),
            idempotency_key=uuid.uuid4(),
        )
        product_item.stock_movement_id = None
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=actor.user_id,
            entity_type="order_product_item",
            entity_id=product_item.id,
            action=AuditAction.UPDATE,
            old_values={"stock_movement_id": None},
            new_values={
                "change_type": "reopen_stock_reversal",
                "compensating_stock_movement_id": str(movement.id),
            },
        )

    # Desfaz a promoção automática do Appointment pra `paid` (ver
    # `close_order` -> `mark_paid`) — volta pra `finished`, nunca deixa
    # a Order `OPEN` de novo com o Appointment ainda `paid`.
    appointments_service.demote_appointment_from_paid_for_order_reopen(session, actor, order.appointment_id)

    order.status = OrderStatus.OPEN
    order.closed_at = None
    order.closed_by = None
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.UPDATE,
        old_values={"status": "closed"},
        new_values={
            "status": "open", "change_type": "reopen", "reason": data.reason,
            "reversed_payment_ids": [str(p.id) for p in active_payments],
        },
    )
    return _reload(session, organization_id, order_id)


def add_product_item(
    session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderProductItemCreate
) -> Order:
    """Adiciona um produto à comanda ABERTA — dois sentidos possíveis
    (`data.item_type`, migration 0039, item "Comanda → Consumo de
    estoque"):

      - `SALE` (default, comportamento original, inalterado): `unit_price`
        nasce sempre do catálogo (`Product.sale_price`), nunca do
        payload — mesma filosofia de `OrderItem.price` nascer de
        `AppointmentItem.price`. Exige `product.for_sale=True`.
      - `CONSUMPTION`: produto consumido internamente durante o serviço
        (ex.: cabelo usado numa progressiva) — NÃO exige
        `product.for_sale` (um produto de uso interno é, por definição,
        o caso comum aqui, mas nada impede consumir internamente um
        produto que também é vendido). `unit_price` vem do payload,
        `0` por padrão (consumo sem cobrança separada) ou um valor
        explícito quando a cliente paga pelo consumo à parte.

    A baixa de estoque NÃO acontece aqui pra nenhum dos dois — só no
    fechamento (ver `close_order`); adicionar/remover um produto numa
    comanda aberta nunca move uma unidade sequer de `StockLevel`.

    Item "produto precisa pertencer/estar disponível na unidade da
    comanda": `Product` é catálogo de ORGANIZAÇÃO (não tem `branch_id`
    — só `StockLevel`, por linha, é por unidade, ver `models/product.py`),
    então não existe "produto da unidade X" pra bloquear aqui. A regra
    vira, na prática, "a disponibilidade é sempre checada contra a
    unidade da COMANDA, nunca outra": tanto aqui (produto só precisa
    existir/estar ativo na organização) quanto na baixa (`close_order`
    sempre chama `record_sale_movement` com `branch_id=order.branch_id`
    — nunca lê/decrementa `StockLevel` de nenhuma outra unidade). Não
    exigimos uma linha de `StockLevel` já existente pra esta unidade no
    momento de adicionar: `StockLevel` nasce sob demanda na primeira
    movimentação (Etapa B), então isso bloquearia a primeira venda
    legítima de um produto que a unidade ainda não tinha estocado."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível adicionar produto a uma comanda aberta.")

    product = product_repo.get(session, organization_id, data.product_id)
    if product is None:
        raise NotFoundError("Produto não encontrado.")
    if not product.is_active:
        raise ValidationDomainError("Produto inativo não pode ser adicionado à comanda.")

    if data.item_type == OrderProductItemKind.CONSUMPTION:
        unit_price = data.unit_price if data.unit_price is not None else Decimal("0")
    else:
        if not product.for_sale:
            raise ValidationDomainError("Este produto é de uso interno e não pode ser vendido em comanda.")
        if product.sale_price is None:
            raise ValidationDomainError("Produto sem preço de venda definido — defina um preço antes de vender.")
        unit_price = product.sale_price

    quantity = stock_service.resolve_quantity_in_product_unit(
        session, organization_id, product.id, data.quantity, data.input_unit
    )

    item = order_product_item_repo.create(
        session,
        organization_id,
        order_id=order.id,
        product_id=product.id,
        product_name=product.name,
        quantity=quantity,
        unit_price=unit_price,
        item_type=data.item_type,
    )
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order_product_item",
        entity_id=item.id,
        action=AuditAction.CREATE,
        new_values={
            "order_id": str(order_id), "product_id": str(product.id),
            "quantity": str(quantity), "unit_price": str(unit_price), "item_type": data.item_type.value,
        },
    )
    return _reload(session, organization_id, order_id)


def remove_product_item(session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID) -> Order:
    """Remove uma linha de produto ANTES do fechamento — nunca gera
    baixa de estoque (item explícito do pedido), porque nenhuma
    movimentação chegou a ser criada pra esta linha (baixa só
    acontece em `close_order`, e só pra linhas que sobreviverem até
    lá)."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível remover produto de uma comanda aberta.")
    item = next((i for i in order.product_items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Produto da comanda não encontrado.")
    # Sanidade estrutural: uma comanda OPEN nunca deveria ter uma linha
    # já com `stock_movement_id` preenchido (baixa só acontece no
    # fechamento, que promove a comanda pra CLOSED na mesma transação)
    # — mas a checagem custa nada e documenta a invariante explicitamente.
    assert item.stock_movement_id is None, "linha de comanda aberta não deveria ter baixa de estoque associada"

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order_product_item",
        entity_id=item.id,
        action=AuditAction.DELETE,
        old_values={
            "order_id": str(order_id), "product_id": str(item.product_id),
            "quantity": str(item.quantity), "unit_price": str(item.unit_price),
        },
    )
    order_product_item_repo.delete(session, item)
    return _reload(session, organization_id, order_id)


def update_product_item(
    session: Session, actor: ActorContext, order_id: uuid.UUID, item_id: uuid.UUID, data: OrderProductItemUpdate
) -> Order:
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ValidationDomainError("Só é possível editar uma linha de comanda aberta.")
    item = next((i for i in order.product_items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Produto da comanda não encontrado.")

    changes: dict[str, tuple[str, str]] = {}
    if data.quantity is not None and data.quantity != item.quantity:
        changes["quantity"] = (str(item.quantity), str(data.quantity))
        item.quantity = data.quantity
    if data.unit_price is not None and data.unit_price != item.unit_price:
        changes["unit_price"] = (str(item.unit_price), str(data.unit_price))
        item.unit_price = data.unit_price

    if not changes:
        return _reload(session, organization_id, order_id)

    session.flush()
    for field, (old_value, new_value) in changes.items():
        audit_log_repo.create(
            session,
            organization_id=organization_id,
            user_id=actor.user_id,
            entity_type="order_product_item",
            entity_id=item.id,
            action=AuditAction.UPDATE,
            old_values={field: old_value},
            new_values={field: new_value, "change_type": f"manual_{field}_edit", "order_id": str(order_id)},
        )
    return _reload(session, organization_id, order_id)


def close_order(session: Session, actor: ActorContext, order_id: uuid.UUID, data: OrderClose) -> Order:
    """Fecha a comanda: registra pagamento(s), baixa estoque de cada
    linha de produto e promove o Appointment pra `paid` — tudo dentro
    da MESMA transação da request (`api/deps.py::get_db` só commita no
    final; qualquer exceção aqui reverte tudo junto).

    Retry-safety (item explícito "retry não pode gerar duas saídas"):
    a linha da comanda é travada com `SELECT ... FOR UPDATE`
    (`order_repo.get_for_update`) ANTES de checar o status. Duas
    tentativas de fechamento da MESMA comanda — sequenciais ou
    concorrentes — nunca correm em paralelo de verdade: a segunda
    bloqueia até a primeira commitar, e então lê `status=closed` já
    persistido e recusa com 409, sem criar um segundo Payment nem uma
    segunda baixa de estoque. Isto é ortogonal ao lock de CONCORRÊNCIA
    de estoque em si (`stock_level_repo.lock_or_create`, `SELECT ...
    FOR UPDATE` na linha de `StockLevel`) — que é quem serializa duas
    comandas DIFERENTES disputando a última unidade do mesmo produto
    na mesma unidade (ver `services/stock.py`)."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.OPEN:
        raise ConflictError("Esta comanda já está fechada.")

    # Etapa H — "exigir caixa aberto para receber pagamento" (padrão
    # ON; hoje já é sempre verdade na prática, pois todo `Payment`
    # exige um `cash_register_id` de caixa aberto — ver validação logo
    # abaixo) e "bloquear operações se existir caixa aberto de dia
    # anterior" (padrão ON — este SIM é um comportamento novo: mesmo
    # apontando pra um caixa aberto hoje, o fechamento é recusado
    # enquanto existir um caixa de dia anterior ainda aberto na mesma
    # unidade).
    cash_register_service.assert_operational_prerequisites(session, actor, order.branch_id, purpose="payment")

    order_totals_breakdown = order_totals.order_total_breakdown(order)
    total = order_totals_breakdown.total
    # Etapa "Overpayment" — enquanto o Nexa não modela troco/crédito/
    # estorno, nenhum lançamento pode ultrapassar o SALDO da comanda no
    # momento em que é aplicado (nunca só a soma agregada no final —
    # um lançamento isolado que já estoura o saldo é recusado ali
    # mesmo, mesmo que um lançamento seguinte "compensasse" a soma).
    # `data.payments` é processada em ORDEM (a mesma semântica de fila
    # que `close_orders_consolidated` já usa pra dividir entre
    # comandas) — cada entrada consome do MESMO saldo que a anterior
    # deixou. Uma comanda com total 0 fecha com `payments=[]` (saldo
    # nunca fica positivo, loop nem executa).
    saldo = total
    for payment_in in data.payments:
        if payment_in.amount > saldo:
            raise ValidationDomainError("O valor do pagamento não pode ser maior que o saldo da comanda.")
        saldo -= payment_in.amount
    if saldo > 0:
        raise ValidationDomainError(
            f"Valor pago (R$ {total - saldo}) é menor que o total da comanda (R$ {total})."
        )

    # Valida TODOS os caixas informados antes de criar qualquer
    # Payment — falha rápido (sem criar pagamento parcial) se algum
    # dos lançamentos apontar pra um caixa de outra organização, que
    # não existe, ou que já está fechado.
    for payment_in in data.payments:
        cash_register_service.assert_register_open_and_in_org(session, organization_id, payment_in.cash_register_id)

    # Baixa de estoque — ANTES de criar qualquer Payment (falha rápido:
    # se faltar estoque pra algum produto, a comanda nem chega a
    # registrar pagamento). `product_repo.get` pode devolver `None`
    # numa borda teórica (produto desativado depois de adicionado à
    # comanda continua vendável — só bloqueamos inativo no MOMENTO de
    # adicionar, ver `add_product_item` — então "sumir" só aconteceria
    # se o produto tivesse sido de fato apagado, o que o FK RESTRICT
    # impede enquanto existir referência; o `is not None` é defesa em
    # profundidade, não um caminho esperado).
    for product_item in order.product_items:
        if product_item.stock_movement_id is not None:
            # Defesa em profundidade — não deveria ser alcançável (o
            # lock da comanda já impede uma segunda passagem por aqui
            # pra qualquer retry), mas se acontecer, nunca baixa de novo.
            continue
        product = product_repo.get(session, organization_id, product_item.product_id)
        # `item_type` decide só o MOTIVO da baixa (venda vs consumo
        # interno) — a baixa em si, o lock, a checagem de saldo e a
        # idempotência (`stock_movement_id`) são idênticos pros dois
        # (ver docstring de `models/order.py::OrderProductItem`).
        record_movement = (
            stock_service.record_sale_movement
            if product_item.item_type == OrderProductItemKind.SALE
            else stock_service.record_internal_use_movement
        )
        try:
            movement = record_movement(
                session,
                actor,
                product_id=product_item.product_id,
                branch_id=order.branch_id,
                quantity=product_item.quantity,
                order_id=order.id,
                unit_cost=product.cost_price if product is not None else None,
            )
        except ValidationDomainError as exc:
            raise ValidationDomainError(
                f"Estoque insuficiente para '{product_item.product_name}': {exc.message}"
            ) from exc
        product_item.stock_movement_id = movement.id
    session.flush()

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None

    for payment_in in data.payments:
        # Etapa N3 — a taxa é resolvida por PAYMENT individual (nunca
        # sobre o total da comanda — item explícito "pagamento
        # dividido"), no momento exato da criação, e congelada como
        # snapshot (ver `services/payment_fees.py::resolve_fee`).
        fee = payment_fees_service.resolve_fee(
            session, organization_id,
            method=payment_in.method, card_brand=payment_in.card_brand,
            installments=payment_in.installments, amount=payment_in.amount,
        )
        payment_repo.create(
            session,
            organization_id,
            order_id=order.id,
            cash_register_id=payment_in.cash_register_id,
            method=payment_in.method,
            card_brand=payment_in.card_brand,
            installments=payment_in.installments,
            amount=payment_in.amount,
            created_by=actor.user_id,
            created_by_name=actor_name,
            payment_fee_rule_id=fee.payment_fee_rule_id,
            fee_percent_snapshot=fee.fee_percent_snapshot,
            fee_amount_snapshot=fee.fee_amount_snapshot,
            net_amount_snapshot=fee.net_amount_snapshot,
            fee_status=fee.fee_status,
        )

    # Etapa C2 — comissão resolvida por ITEM individual (nunca sobre o
    # total da comanda — mesmo raciocínio de "1 OrderItem = 1 serviço +
    # 1 profissional + 1 valor" já estabelecido pra granularidade
    # analítica, N2), no momento exato do fechamento, e congelada como
    # snapshot (ver `services/commissions.py::resolve_commission`).
    for item in order.items:
        commission = commissions_service.resolve_commission(
            session, organization_id,
            professional_id=item.professional_id, service_id=item.service_id, price=item.price,
        )
        item.commission_type_snapshot = commission.commission_type_snapshot
        item.commission_value_snapshot = commission.commission_value_snapshot
        item.commission_amount_snapshot = commission.commission_amount_snapshot
        item.commission_status = commission.commission_status

    # Promove o Appointment ANTES de marcar a comanda como fechada: se a
    # transição falhar (ex.: agendamento já `paid` ou `cancelled` — ver
    # `appointment_state_machine.mark_paid`), a comanda não fica
    # fechada/com pagamento registrado sem o agendamento ter virado
    # `paid` — evita os dois ficarem inconsistentes entre si. Item
    # "não condicione a Comanda a ter passado por todos os status":
    # `mark_paid` promove de QUALQUER status operacional direto pra
    # `paid`, não exige `finished` antes. O usuário nunca precisa
    # marcar "Pago" manualmente à parte: fechar a comanda já faz isso.
    appointments_service.mark_paid(session, actor, order.appointment_id)

    order.status = OrderStatus.CLOSED
    order.closed_at = datetime.now(timezone.utc)
    order.closed_by = actor.user_id
    session.flush()

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order",
        entity_id=order.id,
        action=AuditAction.UPDATE,
        old_values={"status": "open"},
        new_values={
            # Chegar aqui só é possível com `saldo == 0` (ver checagem
            # acima) — total é sempre o valor efetivamente pago.
            "status": "closed", "change_type": "close_order", "paid_total": str(total),
            "services_total": str(order_totals_breakdown.services_total),
            "products_total": str(order_totals_breakdown.products_total),
        },
    )
    return _reload(session, organization_id, order_id)


def close_orders_consolidated(
    session: Session, actor: ActorContext, primary_order_id: uuid.UUID, data: ConsolidatedOrderClose
) -> list[Order]:
    """Fecha VÁRIAS comandas relacionadas (mesma cliente, mesmo dia) de
    uma vez só — item "Fechamento consolidado" (Etapa I). Tudo dentro
    da MESMA transação da request (`api/deps.py::get_db` só commita no
    final; qualquer exceção reverte tudo — nenhuma comanda fecha
    "pela metade").

    Retry-safety: TODAS as comandas envolvidas são travadas
    (`order_repo.get_for_update`) em ordem DETERMINÍSTICA (por `id`,
    nunca a ordem recebida no payload) ANTES de checar qualquer status
    — evita deadlock entre duas requisições concorrentes disputando o
    mesmo conjunto de comandas em ordens diferentes, e garante que um
    retry sempre enxerga o estado já commitado (nenhuma comanda fecha
    duas vezes, nenhum pagamento/baixa duplicado — mesmo raciocínio de
    `close_order`, só que pra N comandas de uma vez).

    Split de pagamento: `data.payments` é tratada como uma FILA — cada
    comanda (na ordem de `order_number`, a mais legível pro usuário)
    consome da fila até cobrir o PRÓPRIO total; um lançamento que não
    fecha exato numa fronteira de comanda é DIVIDIDO em duas linhas de
    `Payment` (mesmo método/caixa/bandeira, valor reduzido em cada) — a
    soma por método continua batendo com o que a cliente de fato pagou.

    Overpayment (pagar a mais que o total consolidado) é RECUSADO —
    enquanto o Nexa não modela troco/crédito/estorno, `paid_total` tem
    que bater EXATO com `grand_total` (nunca só `>=`). Uma comanda com
    total 0 dentro do lote simplesmente não consome nada da fila."""
    organization_id = actor.organization_id

    order_ids: list[uuid.UUID] = list(dict.fromkeys(data.order_ids))
    if primary_order_id not in order_ids:
        order_ids.append(primary_order_id)
    if len(order_ids) < 2:
        raise ValidationDomainError("Fechamento consolidado exige pelo menos 2 comandas.")

    # Lock em ordem determinística (por id) — nunca a ordem do payload,
    # pra duas requisições concorrentes com o mesmo conjunto nunca
    # travarem uma na outra em sentidos opostos.
    locked_by_id: dict[uuid.UUID, Order] = {}
    for oid in sorted(order_ids):
        locked_by_id[oid] = _get_order_for_update_or_404(session, organization_id, oid)
    orders = [locked_by_id[oid] for oid in order_ids]

    if len({o.client_id for o in orders}) > 1:
        raise ValidationDomainError("Fechamento consolidado exige comandas da mesma cliente.")

    for order in orders:
        if order.status != OrderStatus.OPEN:
            raise ConflictError(f"A comanda #{order.order_number} já está fechada ou cancelada.")

    # Etapa H — mesma trava de `close_order`, uma vez por UNIDADE
    # distinta entre as comandas envolvidas (o caso comum é todas na
    # mesma unidade, mas o código não assume isso).
    for branch_id in {o.branch_id for o in orders}:
        cash_register_service.assert_operational_prerequisites(session, actor, branch_id, purpose="payment")

    orders_by_number = sorted(orders, key=lambda o: o.order_number)

    totals: dict[uuid.UUID, Decimal] = {order.id: order_totals.order_total(order) for order in orders_by_number}
    grand_total = sum(totals.values(), Decimal("0"))

    paid_total = sum((p.amount for p in data.payments), Decimal("0"))
    if paid_total > grand_total:
        raise ValidationDomainError("O valor do pagamento não pode ser maior que o saldo da comanda.")
    if paid_total < grand_total:
        raise ValidationDomainError(
            f"Valor pago (R$ {paid_total}) é menor que o total consolidado (R$ {grand_total})."
        )

    for payment_in in data.payments:
        cash_register_service.assert_register_open_and_in_org(session, organization_id, payment_in.cash_register_id)

    # Baixa de estoque de CADA comanda — antes de qualquer Payment,
    # mesmo raciocínio "falha rápido" de `close_order`: estoque
    # insuficiente em QUALQUER comanda do lote aborta o lote inteiro
    # (nenhuma fecha, nenhum pagamento é criado).
    for order in orders_by_number:
        for product_item in order.product_items:
            if product_item.stock_movement_id is not None:
                continue
            product = product_repo.get(session, organization_id, product_item.product_id)
            record_movement = (
                stock_service.record_sale_movement
                if product_item.item_type == OrderProductItemKind.SALE
                else stock_service.record_internal_use_movement
            )
            try:
                movement = record_movement(
                    session, actor, product_id=product_item.product_id, branch_id=order.branch_id,
                    quantity=product_item.quantity, order_id=order.id,
                    unit_cost=product.cost_price if product is not None else None,
                )
            except ValidationDomainError as exc:
                raise ValidationDomainError(
                    f"Estoque insuficiente para '{product_item.product_name}' "
                    f"(comanda #{order.order_number}): {exc.message}"
                ) from exc
            product_item.stock_movement_id = movement.id
    session.flush()

    actor_user = user_repo.get(session, actor.user_id)
    actor_name = actor_user.name if actor_user is not None else None

    # Fila de pagamento — `amount` mutável (por isso um dict, não o
    # próprio schema imutável) pra "descontar" conforme cada comanda
    # consome parte de um lançamento.
    queue: list[dict] = [
        {
            "method": p.method, "amount": p.amount, "cash_register_id": p.cash_register_id,
            "card_brand": p.card_brand, "installments": p.installments,
        }
        for p in data.payments
    ]

    def _create_payment(order: Order, entry: dict, amount: Decimal) -> None:
        # Mesma função de domínio de `close_order` acima (nunca duas
        # implementações da fórmula) — cada Payment resultante do split
        # resolve a taxa sobre o PRÓPRIO valor (`amount`), nunca sobre o
        # total consolidado nem sobre o `entry["amount"]` original antes
        # do split.
        fee = payment_fees_service.resolve_fee(
            session, organization_id,
            method=entry["method"], card_brand=entry["card_brand"],
            installments=entry["installments"], amount=amount,
        )
        payment_repo.create(
            session, organization_id, order_id=order.id, cash_register_id=entry["cash_register_id"],
            method=entry["method"], card_brand=entry["card_brand"], installments=entry["installments"],
            amount=amount, created_by=actor.user_id, created_by_name=actor_name,
            payment_fee_rule_id=fee.payment_fee_rule_id,
            fee_percent_snapshot=fee.fee_percent_snapshot,
            fee_amount_snapshot=fee.fee_amount_snapshot,
            net_amount_snapshot=fee.net_amount_snapshot,
            fee_status=fee.fee_status,
        )

    for order in orders_by_number:
        remaining = totals[order.id]
        while remaining > 0:
            if not queue:
                # Não deveria ser alcançável — já validamos
                # `paid_total >= grand_total` acima. Defesa em
                # profundidade: nunca fecha parcialmente se acontecer.
                raise ValidationDomainError(
                    "Falha ao distribuir pagamento entre as comandas — soma insuficiente."
                )
            entry = queue[0]
            take = min(entry["amount"], remaining)
            _create_payment(order, entry, take)
            entry["amount"] -= take
            remaining -= take
            if entry["amount"] <= 0:
                queue.pop(0)

    # Nunca sobra nada na fila aqui — `paid_total == grand_total` já foi
    # validado acima (overpayment é recusado antes de chegar neste
    # ponto), então a distribuição consome a fila inteira exatamente.

    # Etapa C2 — mesma resolução de comissão de `close_order`, item por
    # item, em CADA comanda do lote (nunca duas implementações).
    for order in orders_by_number:
        for item in order.items:
            commission = commissions_service.resolve_commission(
                session, organization_id,
                professional_id=item.professional_id, service_id=item.service_id, price=item.price,
            )
            item.commission_type_snapshot = commission.commission_type_snapshot
            item.commission_value_snapshot = commission.commission_value_snapshot
            item.commission_amount_snapshot = commission.commission_amount_snapshot
            item.commission_status = commission.commission_status

    now = datetime.now(timezone.utc)
    for order in orders_by_number:
        appointments_service.mark_paid(session, actor, order.appointment_id)
        order.status = OrderStatus.CLOSED
        order.closed_at = now
        order.closed_by = actor.user_id
    session.flush()

    sibling_numbers = [o.order_number for o in orders_by_number]
    for order in orders_by_number:
        audit_log_repo.create(
            session, organization_id=organization_id, user_id=actor.user_id, entity_type="order",
            entity_id=order.id, action=AuditAction.UPDATE,
            old_values={"status": "open"},
            new_values={
                "status": "closed", "change_type": "consolidated_close",
                "order_total": str(totals[order.id]), "grand_total": str(grand_total),
                "related_order_numbers": sibling_numbers,
            },
        )

    return [_reload(session, organization_id, o.id) for o in orders_by_number]


def correct_consumption(
    session: Session,
    actor: ActorContext,
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    data: OrderConsumptionCorrection,
) -> Order:
    """Corrige um consumo (`item_type=CONSUMPTION`) já registrado numa
    comanda FECHADA — item "Não duplicar baixa" / "correção
    pós-fechamento". NUNCA edita `item.quantity` nem o `StockMovement`
    original (os dois continuam congelados exatamente como ficaram no
    fechamento — ver docstring de `models/order.py::OrderProductItem`):
    gera um movimento COMPENSATÓRIO novo (`reason=ADJUSTMENT`,
    `order_id` vinculado), preservando o ledger append-only.

    Idempotência (auditoria "última correção pré-push", item 1) —
    TRANSACIONAL, nunca só um `disabled` de botão no frontend:
    `data.idempotency_key` é única por TENTATIVA de correção (o
    frontend gera uma chave nova ao abrir o formulário, reenvia a MESMA
    em qualquer retry/double-click). Duas camadas:

      1. Checagem otimista ANTES de tentar criar — se já existe uma
         `StockMovement` com esta chave, a correção já foi efetivada;
         devolve o estado atual sem criar nada novo nem duplicar o
         `AuditLog`.
      2. `SAVEPOINT` (`session.begin_nested()`) em volta da criação —
         cobre a corrida REAL entre duas transações concorrentes que
         passam as duas pela checagem otimista antes de qualquer uma
         commitar: a que perder a corrida do índice único parcial
         (`uq_stock_movements_idempotency_key`) recebe `IntegrityError`,
         tratado aqui como "a outra já corrigiu" — nunca como erro
         genérico, nunca cria uma segunda compensação.

    A trava de linha da comanda (`_get_order_for_update_or_404`) reforça
    a mesma garantia transacional já usada em `close_order`/
    `cancel_order` pra qualquer mutação de comanda."""
    organization_id = actor.organization_id
    order = _get_order_for_update_or_404(session, organization_id, order_id)
    if order.status != OrderStatus.CLOSED:
        raise ValidationDomainError("Correção de consumo só se aplica a uma comanda já fechada.")
    item = next((i for i in order.product_items if i.id == item_id), None)
    if item is None:
        raise NotFoundError("Produto da comanda não encontrado.")
    if item.item_type != OrderProductItemKind.CONSUMPTION:
        raise ValidationDomainError("Só é possível corrigir consumo de itens do tipo 'consumo interno'.")
    if item.stock_movement_id is None:
        raise ValidationDomainError("Este item ainda não gerou baixa de estoque — nada para corrigir.")

    # Camada 1 — checagem otimista (caminho feliz: nenhuma corrida real).
    if stock_movement_repo.get_by_idempotency_key(session, organization_id, data.idempotency_key) is not None:
        return _reload(session, organization_id, order_id)

    direction = StockMovementDirection.OUT if data.quantity_delta > 0 else StockMovementDirection.IN
    quantity = abs(data.quantity_delta)
    observation = (
        f"Correção de consumo — Comanda #{order.order_number}, produto '{item.product_name}': {data.reason}"
    )
    try:
        with session.begin_nested():
            movement = stock_service.record_consumption_correction(
                session, actor, product_id=item.product_id, branch_id=order.branch_id, order_id=order.id,
                quantity=quantity, direction=direction, observation=observation,
                idempotency_key=data.idempotency_key,
            )
    except ValidationDomainError as exc:
        raise ValidationDomainError(f"Não foi possível corrigir o consumo: {exc.message}") from exc
    except IntegrityError:
        # Camada 2 — corrida real: outra transação com a MESMA chave
        # commitou primeiro. Nunca cria uma segunda compensação.
        existing = stock_movement_repo.get_by_idempotency_key(session, organization_id, data.idempotency_key)
        if existing is None:
            raise
        return _reload(session, organization_id, order_id)

    audit_log_repo.create(
        session,
        organization_id=organization_id,
        user_id=actor.user_id,
        entity_type="order_product_item",
        entity_id=item.id,
        action=AuditAction.UPDATE,
        old_values={"quantity": str(item.quantity)},
        new_values={
            "change_type": "consumption_correction",
            "quantity_delta": str(data.quantity_delta),
            "reason": data.reason,
            "compensating_stock_movement_id": str(movement.id),
            "idempotency_key": str(data.idempotency_key),
        },
    )
    return _reload(session, organization_id, order_id)
