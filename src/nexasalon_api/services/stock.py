"""Movimentações, Transferência entre unidades e Visão Geral do
Estoque — Etapa B.

Toda mudança de `StockLevel.quantity_on_hand` passa por
`_apply_delta`, que SEMPRE trava a linha (`stock_level_repo.lock_or_create`,
`SELECT ... FOR UPDATE`) antes de ler/escrever a quantidade — item
explícito "concorrência segura, nunca estoque negativo". Uma SAÍDA que
deixaria o saldo negativo é recusada (`ValidationDomainError`) ANTES de
qualquer linha em `stock_movements` ser criada — nunca existe uma
movimentação "órfã" registrando uma operação que na verdade falhou."""
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import AuditAction, StockMovementDirection, StockMovementReason
from nexasalon_api.models.product import StockLevel
from nexasalon_api.models.stock import StockMovement, StockTransfer
from nexasalon_api.repositories import (
    audit_log_repo,
    branch_repo,
    product_repo,
    stock_level_repo,
    stock_movement_repo,
    stock_transfer_repo,
    user_repo,
)
from nexasalon_api.schemas.stock import MANUAL_REASONS_BY_DIRECTION

_LOW_STOCK_LIMIT = 20
_MOST_CONSUMED_LIMIT = 10
_DEFAULT_FLOW_DAYS = 30


def _resolve_user_name(session: Session, user_id: uuid.UUID) -> str:
    user = user_repo.get(session, user_id)
    return user.name if user is not None else "Usuário removido"


def _get_product_or_404(session: Session, organization_id: uuid.UUID, product_id: uuid.UUID):
    product = product_repo.get(session, organization_id, product_id)
    if product is None:
        raise NotFoundError("Produto não encontrado.")
    return product


def _get_branch_or_404(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID):
    branch = branch_repo.get(session, organization_id, branch_id)
    if branch is None:
        raise NotFoundError("Unidade não encontrada.")
    return branch


def _apply_delta(
    session: Session,
    organization_id: uuid.UUID,
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    direction: StockMovementDirection,
    quantity: Decimal,
) -> StockLevel:
    level = stock_level_repo.lock_or_create(session, organization_id, product_id, branch_id)
    if direction == StockMovementDirection.IN:
        level.quantity_on_hand = level.quantity_on_hand + quantity
    else:
        if level.quantity_on_hand < quantity:
            raise ValidationDomainError(
                f"Estoque insuficiente: saldo atual é {level.quantity_on_hand}, "
                f"tentativa de saída de {quantity}."
            )
        level.quantity_on_hand = level.quantity_on_hand - quantity
    session.flush()
    return level


def _create_movement(
    session: Session,
    actor: ActorContext,
    *,
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    direction: StockMovementDirection,
    reason: StockMovementReason,
    quantity: Decimal,
    unit_cost: Decimal | None = None,
    observation: str | None = None,
    order_id: uuid.UUID | None = None,
    transfer_id: uuid.UUID | None = None,
    inventory_count_id: uuid.UUID | None = None,
) -> StockMovement:
    """Núcleo interno — usado tanto pela criação manual (`record_movement`,
    depois de validar que o motivo é permitido nesse caminho) quanto
    pelos fluxos de sistema (transferência, fechamento de inventário),
    que passam motivos reservados (`TRANSFER_IN`/`TRANSFER_OUT`/
    `INVENTORY_COUNT`) diretamente."""
    _get_product_or_404(session, actor.organization_id, product_id)
    _get_branch_or_404(session, actor.organization_id, branch_id)

    _apply_delta(session, actor.organization_id, product_id, branch_id, direction, quantity)

    name = _resolve_user_name(session, actor.user_id)
    movement = stock_movement_repo.create(
        session,
        actor.organization_id,
        product_id=product_id,
        branch_id=branch_id,
        direction=direction,
        reason=reason,
        quantity=quantity,
        unit_cost=unit_cost,
        observation=observation,
        created_by=actor.user_id,
        created_by_name=name,
        order_id=order_id,
        transfer_id=transfer_id,
        inventory_count_id=inventory_count_id,
    )

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="stock_movement",
        entity_id=movement.id,
        action=AuditAction.CREATE,
        new_values={
            "product_id": str(product_id),
            "branch_id": str(branch_id),
            "direction": direction.value,
            "reason": reason.value,
            "quantity": str(quantity),
        },
    )
    return movement


def record_movement(
    session: Session,
    actor: ActorContext,
    *,
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    direction: StockMovementDirection,
    reason: StockMovementReason,
    quantity: Decimal,
    unit_cost: Decimal | None = None,
    observation: str | None = None,
) -> StockMovement:
    """Entrada/Saída manual (telas dedicadas do item "Entrada/Saída de
    Estoque"). O schema (`StockMovementCreate`) já valida que `reason`
    combina com `direction` no catálogo GERAL — aqui a checagem é mais
    estreita: só os motivos "manuais" (nunca `TRANSFER_IN`/
    `TRANSFER_OUT`/`INVENTORY_COUNT`, reservados aos fluxos de
    sistema)."""
    allowed = MANUAL_REASONS_BY_DIRECTION[direction]
    if reason not in allowed:
        raise ValidationDomainError(
            f"Motivo '{reason.value}' não pode ser registrado manualmente para esta direção."
        )
    return _create_movement(
        session,
        actor,
        product_id=product_id,
        branch_id=branch_id,
        direction=direction,
        reason=reason,
        quantity=quantity,
        unit_cost=unit_cost,
        observation=observation,
    )


def record_system_movement(
    session: Session,
    actor: ActorContext,
    *,
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    direction: StockMovementDirection,
    reason: StockMovementReason,
    quantity: Decimal,
    inventory_count_id: uuid.UUID | None = None,
    observation: str | None = None,
) -> StockMovement:
    """Ponto de entrada para movimentações geradas PELO SISTEMA (hoje:
    fechamento de inventário — `services/inventory_counts.py`), não por
    uma escolha manual do usuário na tela de Entrada/Saída. Diferente
    de `record_movement`, aceita qualquer `reason` do catálogo geral —
    quem chama é responsável por só usar um motivo reservado
    (`INVENTORY_COUNT`) no contexto certo."""
    return _create_movement(
        session,
        actor,
        product_id=product_id,
        branch_id=branch_id,
        direction=direction,
        reason=reason,
        quantity=quantity,
        observation=observation,
        inventory_count_id=inventory_count_id,
    )


def record_sale_movement(
    session: Session,
    actor: ActorContext,
    *,
    product_id: uuid.UUID,
    branch_id: uuid.UUID,
    quantity: Decimal,
    order_id: uuid.UUID,
    unit_cost: Decimal | None = None,
    observation: str | None = None,
) -> StockMovement:
    """Baixa de estoque gerada pelo FECHAMENTO de uma Comanda — Etapa C
    (Estoque ↔ Comanda). Sempre `direction=OUT`/`reason=SALE`, sempre
    com `order_id` preenchido (rastreabilidade: dá pra achar a comanda
    que gerou qualquer saída por venda). `unit_cost` é o custo do
    produto NO MOMENTO da venda (`Product.cost_price` — quem chama é
    responsável por passar o valor certo, ver `services/orders.py::
    close_order`) — guardado só pra quem tem `inventory.view_cost`
    conseguir analisar margem depois; a Comanda em si nunca expõe
    custo em nenhum schema novo desta etapa (ver `schemas/order.py`).

    Levanta `ValidationDomainError` (via `_apply_delta`) se o saldo da
    unidade não cobrir a quantidade — quem chama (`close_order`) deixa
    isso propagar pra abortar o fechamento inteiro."""
    return _create_movement(
        session,
        actor,
        product_id=product_id,
        branch_id=branch_id,
        direction=StockMovementDirection.OUT,
        reason=StockMovementReason.SALE,
        quantity=quantity,
        unit_cost=unit_cost,
        observation=observation,
        order_id=order_id,
    )


def list_movements(
    session: Session,
    actor: ActorContext,
    *,
    product_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    direction: StockMovementDirection | None = None,
    reason: StockMovementReason | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[StockMovement]:
    return stock_movement_repo.list_for_org(
        session,
        actor.organization_id,
        product_id=product_id,
        branch_id=branch_id,
        direction=direction,
        reason=reason,
        date_from=date_from,
        date_to=date_to,
    )


def create_transfer(
    session: Session,
    actor: ActorContext,
    *,
    product_id: uuid.UUID,
    origin_branch_id: uuid.UUID,
    destination_branch_id: uuid.UUID,
    quantity: Decimal,
    observation: str | None = None,
) -> StockTransfer:
    """Transferência entre unidades — gera exatamente DUAS
    movimentações ligadas (`transfer_id`), nunca lançamento financeiro
    (esta função nunca importa/toca `CashMovement`/`Payment`). Se a
    origem não tiver saldo suficiente, `_apply_delta` recusa a SAÍDA e
    a transação inteira (transferência + as duas movimentações) dá
    rollback — nunca fica "só a saída" sem a entrada correspondente."""
    _get_product_or_404(session, actor.organization_id, product_id)
    _get_branch_or_404(session, actor.organization_id, origin_branch_id)
    _get_branch_or_404(session, actor.organization_id, destination_branch_id)
    if origin_branch_id == destination_branch_id:
        raise ValidationDomainError("Unidade de origem e destino devem ser diferentes.")

    name = _resolve_user_name(session, actor.user_id)
    transfer = stock_transfer_repo.create(
        session,
        actor.organization_id,
        product_id=product_id,
        origin_branch_id=origin_branch_id,
        destination_branch_id=destination_branch_id,
        quantity=quantity,
        created_by=actor.user_id,
        created_by_name=name,
        observation=observation,
    )

    _create_movement(
        session,
        actor,
        product_id=product_id,
        branch_id=origin_branch_id,
        direction=StockMovementDirection.OUT,
        reason=StockMovementReason.TRANSFER_OUT,
        quantity=quantity,
        observation=observation,
        transfer_id=transfer.id,
    )
    _create_movement(
        session,
        actor,
        product_id=product_id,
        branch_id=destination_branch_id,
        direction=StockMovementDirection.IN,
        reason=StockMovementReason.TRANSFER_IN,
        quantity=quantity,
        observation=observation,
        transfer_id=transfer.id,
    )

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="stock_transfer",
        entity_id=transfer.id,
        action=AuditAction.CREATE,
        new_values={
            "product_id": str(product_id),
            "origin_branch_id": str(origin_branch_id),
            "destination_branch_id": str(destination_branch_id),
            "quantity": str(quantity),
        },
    )
    return transfer


def get_transfer(session: Session, actor: ActorContext, transfer_id: uuid.UUID) -> StockTransfer:
    transfer = stock_transfer_repo.get(session, actor.organization_id, transfer_id)
    if transfer is None:
        raise NotFoundError("Transferência não encontrada.")
    return transfer


def list_transfers(session: Session, actor: ActorContext) -> list[StockTransfer]:
    return stock_transfer_repo.list_for_org(session, actor.organization_id)


def get_overview(
    session: Session,
    actor: ActorContext,
    *,
    branch_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_cost: bool = False,
) -> dict:
    """Visão Geral (KPIs + lista acionável de estoque baixo + mais
    consumidos + fluxo entrada×saída). Toda agregação é feita em Python
    sobre os dados já filtrados por organização (mesmo estilo de
    `services/cash_register.py::build_summary`) — volume de dados de
    uma única organização/salão nunca justifica SQL agregado aqui."""
    if branch_id is not None:
        _get_branch_or_404(session, actor.organization_id, branch_id)

    now = date_to or datetime.now(timezone.utc)
    since = date_from or (now - timedelta(days=_DEFAULT_FLOW_DAYS))

    levels = stock_level_repo.list_for_org(session, actor.organization_id)
    if branch_id is not None:
        levels = [lv for lv in levels if lv.branch_id == branch_id]

    products_by_id = {p.id: p for p in product_repo.list_all(session, actor.organization_id, include_inactive=True)}
    active_product_ids = {pid for pid, p in products_by_id.items() if p.is_active}

    # Agrega por produto (soma entre unidades quando `branch_id` não
    # filtra uma unidade específica) — "produto sem estoque"/"produto
    # com estoque baixo" nunca conta a mesma unidade duas vezes nem
    # ignora que o mesmo produto pode estar ok numa unidade e baixo
    # noutra (cada linha de `StockLevel` entra separadamente na lista
    # acionável, mesmo que o total do produto já soma tudo).
    qty_by_product: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    low_stock_items = []
    for lv in levels:
        if lv.product_id not in active_product_ids:
            continue
        qty_by_product[lv.product_id] += lv.quantity_on_hand
        if lv.minimum_quantity > 0 and Decimal("0") < lv.quantity_on_hand <= lv.minimum_quantity:
            product = products_by_id.get(lv.product_id)
            low_stock_items.append(
                {
                    "product_id": lv.product_id,
                    "product_name": product.name if product else "—",
                    "branch_id": lv.branch_id,
                    "quantity_on_hand": lv.quantity_on_hand,
                    "minimum_quantity": lv.minimum_quantity,
                }
            )

    products_in_stock = sum(1 for pid in active_product_ids if qty_by_product.get(pid, Decimal("0")) > 0)
    products_out_of_stock = sum(
        1 for pid in active_product_ids if qty_by_product.get(pid, Decimal("0")) <= 0
    )
    low_stock_items.sort(key=lambda item: (item["quantity_on_hand"] / item["minimum_quantity"]))
    low_stock_items = low_stock_items[:_LOW_STOCK_LIMIT]

    stock_value: Decimal | None = None
    if include_cost:
        stock_value = sum(
            (qty_by_product.get(pid, Decimal("0")) * products_by_id[pid].cost_price for pid in active_product_ids),
            Decimal("0"),
        )

    movements = stock_movement_repo.list_for_org(
        session, actor.organization_id, branch_id=branch_id, date_from=since, date_to=now
    )

    consumed_by_product: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    flow_by_day: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"in": Decimal("0"), "out": Decimal("0")})
    for m in movements:
        day = m.created_at.date().isoformat()
        if m.direction == StockMovementDirection.OUT:
            flow_by_day[day]["out"] += m.quantity
            consumed_by_product[m.product_id] += m.quantity
        else:
            flow_by_day[day]["in"] += m.quantity

    most_consumed = sorted(
        (
            {
                "product_id": pid,
                "product_name": products_by_id[pid].name if pid in products_by_id else "—",
                "total_quantity_out": total,
            }
            for pid, total in consumed_by_product.items()
        ),
        key=lambda item: item["total_quantity_out"],
        reverse=True,
    )[:_MOST_CONSUMED_LIMIT]

    flow = [
        {"date": day, "in_total": totals["in"], "out_total": totals["out"]}
        for day, totals in sorted(flow_by_day.items())
    ]

    return {
        "products_in_stock": products_in_stock,
        "products_out_of_stock": products_out_of_stock,
        "low_stock_count": len(
            [lv for lv in levels if lv.minimum_quantity > 0 and Decimal("0") < lv.quantity_on_hand <= lv.minimum_quantity]
        ),
        "stock_value": stock_value,
        "low_stock_items": low_stock_items,
        "most_consumed": most_consumed,
        "flow": flow,
    }
