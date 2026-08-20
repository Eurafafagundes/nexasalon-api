"""Fluxo de Inventário — contagem do sistema vs. contagem real.

Abrir um inventário tira uma FOTO (`system_quantity`) do saldo atual de
cada produto ativo naquela unidade — essa foto nunca é recalculada
depois, mesmo que outra movimentação aconteça enquanto o inventário
segue aberto (é exatamente esse "antes" que o inventário está
comparando com a contagem física real, item "sistema vs. real"). Fechar
gera UMA `StockMovement` (reason=`inventory_count`) por item cuja
diferença for != 0, nunca sobrescreve `StockLevel.quantity_on_hand`
diretamente — o ajuste passa pelo mesmo caminho auditável de qualquer
outra movimentação (`services/stock.py::record_system_movement`, que
por sua vez usa `_apply_delta` com o mesmo lock de concorrência)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import AuditAction, InventoryCountStatus, StockMovementDirection, StockMovementReason
from nexasalon_api.models.stock import InventoryCount
from nexasalon_api.repositories import (
    audit_log_repo,
    branch_repo,
    inventory_count_repo,
    product_repo,
    stock_level_repo,
    user_repo,
)
from nexasalon_api.services import stock as stock_service


def _resolve_user_name(session: Session, user_id: uuid.UUID) -> str:
    user = user_repo.get(session, user_id)
    return user.name if user is not None else "Usuário removido"


def _get_count_or_404(session: Session, organization_id: uuid.UUID, count_id: uuid.UUID) -> InventoryCount:
    count = inventory_count_repo.get(session, organization_id, count_id)
    if count is None:
        raise NotFoundError("Inventário não encontrado.")
    return count


def open_count(
    session: Session, actor: ActorContext, *, branch_id: uuid.UUID, notes: str | None = None
) -> InventoryCount:
    if branch_repo.get(session, actor.organization_id, branch_id) is None:
        raise NotFoundError("Unidade não encontrada.")

    existing = inventory_count_repo.get_open_for_branch(session, actor.organization_id, branch_id)
    if existing is not None:
        raise ConflictError("Já existe um inventário aberto para esta unidade. Feche-o antes de abrir outro.")

    name = _resolve_user_name(session, actor.user_id)
    count = inventory_count_repo.create(
        session, actor.organization_id, branch_id=branch_id, created_by=actor.user_id, created_by_name=name, notes=notes
    )

    products = product_repo.list_all(session, actor.organization_id, include_inactive=False)
    for product in products:
        level = stock_level_repo.get(session, actor.organization_id, product.id, branch_id)
        system_quantity = level.quantity_on_hand if level is not None else Decimal("0")
        inventory_count_repo.add_item(
            session, actor.organization_id, count.id, product_id=product.id, system_quantity=system_quantity
        )

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="inventory_count",
        entity_id=count.id,
        action=AuditAction.CREATE,
        new_values={"branch_id": str(branch_id), "items_count": len(products)},
    )
    return count


def get_count(session: Session, actor: ActorContext, count_id: uuid.UUID) -> InventoryCount:
    return _get_count_or_404(session, actor.organization_id, count_id)


def list_counts(
    session: Session, actor: ActorContext, *, status: InventoryCountStatus | None = None
) -> list[InventoryCount]:
    return inventory_count_repo.list_for_org(session, actor.organization_id, status=status)


def list_items(session: Session, actor: ActorContext, count_id: uuid.UUID):
    _get_count_or_404(session, actor.organization_id, count_id)
    return inventory_count_repo.list_items(session, actor.organization_id, count_id)


def set_item_count(
    session: Session, actor: ActorContext, count_id: uuid.UUID, product_id: uuid.UUID, counted_quantity: Decimal | None
):
    count = _get_count_or_404(session, actor.organization_id, count_id)
    if count.status != InventoryCountStatus.OPEN:
        raise ValidationDomainError("Este inventário já foi fechado — não é possível editar contagens.")

    item = inventory_count_repo.get_item(session, actor.organization_id, count_id, product_id)
    if item is None:
        raise NotFoundError("Item de inventário não encontrado (produto não fazia parte desta contagem).")

    item.counted_quantity = counted_quantity
    session.flush()
    return item


def close_count(
    session: Session, actor: ActorContext, count_id: uuid.UUID, *, notes: str | None = None
) -> InventoryCount:
    count = _get_count_or_404(session, actor.organization_id, count_id)
    if count.status != InventoryCountStatus.OPEN:
        raise ConflictError("Este inventário já está fechado.")

    items = inventory_count_repo.list_items(session, actor.organization_id, count_id)
    uncounted = [item for item in items if item.counted_quantity is None]
    if uncounted:
        raise ValidationDomainError(
            f"Ainda há {len(uncounted)} produto(s) sem contagem registrada — "
            "conte todos os itens antes de fechar o inventário."
        )

    adjustments_made = 0
    for item in items:
        difference = item.counted_quantity - item.system_quantity
        if difference == 0:
            continue
        direction = StockMovementDirection.IN if difference > 0 else StockMovementDirection.OUT
        stock_service.record_system_movement(
            session,
            actor,
            product_id=item.product_id,
            branch_id=count.branch_id,
            direction=direction,
            reason=StockMovementReason.INVENTORY_COUNT,
            quantity=abs(difference),
            inventory_count_id=count.id,
            observation=f"Ajuste de inventário — sistema: {item.system_quantity}, contado: {item.counted_quantity}.",
        )
        adjustments_made += 1

    if notes:
        count.notes = notes
    name = _resolve_user_name(session, actor.user_id)
    count = inventory_count_repo.close(
        session, count, closed_by=actor.user_id, closed_by_name=name, closed_at=datetime.now(timezone.utc)
    )

    audit_log_repo.create(
        session,
        organization_id=actor.organization_id,
        user_id=actor.user_id,
        entity_type="inventory_count",
        entity_id=count.id,
        action=AuditAction.UPDATE,
        new_values={"change_type": "close", "adjustments_made": adjustments_made},
    )
    return count
