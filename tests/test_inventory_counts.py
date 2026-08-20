"""Testes de `services/inventory_counts.py` — contagem de inventário
(sistema vs. real). Cobre: foto do saldo do sistema na abertura, exige
todos os itens contados antes de fechar, gera ajuste (`StockMovement`
reason=inventory_count) só onde há diferença, nunca sobrescreve saldo
diretamente."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, ValidationDomainError
from nexasalon_api.models.enums import InventoryCountStatus, StockMovementDirection, StockMovementReason
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.repositories import stock_level_repo
from nexasalon_api.schemas.product import ProductCreate
from nexasalon_api.services import inventory_counts, products, stock

_PERMS = frozenset({"inventory.view", "inventory.view_cost", "inventory.manage"})


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org inventário", slug=f"org-inv-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Responsável")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=_PERMS,
    )


def _branch(session, org_id) -> uuid.UUID:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b.id


def test_abertura_tira_foto_do_saldo_atual_por_produto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product = products.create_product(session, actor, ProductCreate(name="Produto A"))
    stock.record_movement(
        session, actor, product_id=product.id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("8"),
    )

    count = inventory_counts.open_count(session, actor, branch_id=branch_id)
    items = inventory_counts.list_items(session, actor, count.id)
    assert len(items) == 1
    assert items[0].product_id == product.id
    assert items[0].system_quantity == Decimal("8")
    assert items[0].counted_quantity is None


def test_nao_permite_dois_inventarios_abertos_na_mesma_unidade(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    inventory_counts.open_count(session, actor, branch_id=branch_id)

    with pytest.raises(ConflictError):
        inventory_counts.open_count(session, actor, branch_id=branch_id)


def test_fechar_com_item_nao_contado_e_recusado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    products.create_product(session, actor, ProductCreate(name="Produto A"))

    count = inventory_counts.open_count(session, actor, branch_id=branch_id)
    with pytest.raises(ValidationDomainError):
        inventory_counts.close_count(session, actor, count.id)

    # continua aberto — fechar nunca aplica parcial
    reloaded = inventory_counts.get_count(session, actor, count.id)
    assert reloaded.status == InventoryCountStatus.OPEN


def test_fechar_gera_ajuste_so_para_produtos_com_diferenca(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    p1 = products.create_product(session, actor, ProductCreate(name="Produto 1"))
    p2 = products.create_product(session, actor, ProductCreate(name="Produto 2"))
    stock.record_movement(
        session, actor, product_id=p1.id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("10"),
    )
    stock.record_movement(
        session, actor, product_id=p2.id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("5"),
    )

    count = inventory_counts.open_count(session, actor, branch_id=branch_id)
    # p1: sistema=10, contado=7 (perda de 3). p2: sistema=5, contado=5 (sem diferença).
    inventory_counts.set_item_count(session, actor, count.id, p1.id, Decimal("7"))
    inventory_counts.set_item_count(session, actor, count.id, p2.id, Decimal("5"))

    closed = inventory_counts.close_count(session, actor, count.id)
    assert closed.status == InventoryCountStatus.CLOSED
    assert closed.closed_by == actor.user_id

    level_p1 = stock_level_repo.get(session, org_id, p1.id, branch_id)
    level_p2 = stock_level_repo.get(session, org_id, p2.id, branch_id)
    assert level_p1.quantity_on_hand == Decimal("7")
    assert level_p2.quantity_on_hand == Decimal("5")

    movements_p1 = stock.list_movements(session, actor, product_id=p1.id, reason=StockMovementReason.INVENTORY_COUNT)
    movements_p2 = stock.list_movements(session, actor, product_id=p2.id, reason=StockMovementReason.INVENTORY_COUNT)
    assert len(movements_p1) == 1
    assert movements_p1[0].direction == StockMovementDirection.OUT
    assert movements_p1[0].quantity == Decimal("3")
    assert movements_p1[0].inventory_count_id == count.id
    assert len(movements_p2) == 0  # sem diferença = sem movimentação


def test_fechar_inventario_ja_fechado_e_conflito(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    products.create_product(session, actor, ProductCreate(name="Produto"))
    count = inventory_counts.open_count(session, actor, branch_id=branch_id)
    for item in inventory_counts.list_items(session, actor, count.id):
        inventory_counts.set_item_count(session, actor, count.id, item.product_id, item.system_quantity)
    inventory_counts.close_count(session, actor, count.id)

    with pytest.raises(ConflictError):
        inventory_counts.close_count(session, actor, count.id)


def test_ajuste_positivo_de_inventario_e_entrada(org_session):
    """Contagem real MAIOR que a do sistema gera IN (achou mais do que
    o sistema achava que tinha), nunca OUT."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product = products.create_product(session, actor, ProductCreate(name="Produto"))
    stock.record_movement(
        session, actor, product_id=product.id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("2"),
    )
    count = inventory_counts.open_count(session, actor, branch_id=branch_id)
    inventory_counts.set_item_count(session, actor, count.id, product.id, Decimal("5"))
    inventory_counts.close_count(session, actor, count.id)

    level = stock_level_repo.get(session, org_id, product.id, branch_id)
    assert level.quantity_on_hand == Decimal("5")
    movements = stock.list_movements(session, actor, product_id=product.id, reason=StockMovementReason.INVENTORY_COUNT)
    assert movements[0].direction == StockMovementDirection.IN
    assert movements[0].quantity == Decimal("3")
