"""Testes de "Comanda → Consumo de Estoque" (migration 0039,
`OrderProductItem.item_type`). Reaproveita os fixtures de
`tests/test_order_products.py` — cobre: consumo interno gera
`StockMovementReason.INTERNAL_USE` (nunca `SALE`); produto de uso
interno (`for_sale=False`) pode ser consumido mesmo sem poder ser
vendido; `unit_price` default `0` quando não informado; consumo COBRADO
entra no total da comanda; venda e consumo do mesmo produto na mesma
comanda geram DOIS movimentos distintos; correção pós-fechamento nunca
edita o item/movimento original (ledger append-only)."""
import uuid
from datetime import time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ValidationDomainError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    OrderProductItemKind,
    PaymentMethod,
    StockMovementDirection,
    StockMovementReason,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import cash_register_repo, stock_level_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    OrderClose,
    OrderConsumptionCorrection,
    OrderProductItemCreate,
    PaymentCreate,
)
from nexasalon_api.schemas.product import ProductCreate
from nexasalon_api.services import appointments, cash_register, orders, products, stock

_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4
_ALL_PERMS = frozenset(
    {
        "agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel",
        "inventory.view", "inventory.manage",
        "orders.view", "orders.manage", "orders.edit_price", "payments.register",
    }
)


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org consumo", slug=f"org-consumo-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_ALL_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Ingrid Alves")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _dt(hour, minute=0):
    from datetime import datetime

    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _open_order(session, org_id, actor):
    branch = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(branch)
    session.flush()
    if cash_register_repo.get_open_for_branch(session, org_id, branch.id) is None:
        cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = Professional(organization_id=org_id, branch_id=branch.id, name="Profissional")
    session.add(prof)
    session.flush()
    session.add(
        WorkingHours(organization_id=org_id, professional_id=prof.id, weekday=_THURSDAY, start_time=time(9, 0), end_time=time(20, 0))
    )
    client = Client(organization_id=org_id, name="Amanda")
    session.add(client)
    svc = Service(organization_id=org_id, name="Progressiva", default_duration_minutes=120, default_price=Decimal("300.00"))
    session.add(svc)
    session.flush()
    session.add(ProfessionalService(professional_id=prof.id, service_id=svc.id))
    session.flush()
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=svc.id, start_at=_dt(9))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    order = orders.create_order(session, actor, appt.id)
    return order, branch


def _internal_product(session, actor, *, name="Cabelo Humano Castanho 65cm", cost=Decimal("40.00")):
    """Produto de uso interno — `for_sale=False`, mesmo caso de uso do
    briefing (cabelo consumido, nunca vendido diretamente)."""
    return products.create_product(session, actor, ProductCreate(name=name, cost_price=cost, for_sale=False))


def _stock_in(session, actor, product_id, branch_id, quantity):
    stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=quantity,
    )


def _close(session, actor, order):
    from nexasalon_api.services import order_totals

    total = order_totals.order_total(order)
    register = cash_register_repo.get_open_for_branch(session, actor.organization_id, order.branch_id)
    return orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )


# ---------------------------------------------------------------------
# Adicionar consumo — produto for_sale=False é permitido (nunca em venda)
# ---------------------------------------------------------------------


def test_produto_de_uso_interno_pode_ser_consumido_mesmo_sem_poder_ser_vendido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))

    updated = orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )

    line = updated.product_items[0]
    assert line.item_type == OrderProductItemKind.CONSUMPTION
    assert line.unit_price == Decimal("0")  # sem cobrança, default
    assert line.quantity == Decimal("180")


def test_consumo_sem_cobranca_nao_afeta_o_total_mas_consumo_cobrado_afeta(org_session):
    from nexasalon_api.schemas.order import OrderRead

    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)  # 300 de serviço
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))

    updated = orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )
    assert OrderRead.from_order(updated).total == Decimal("300.00")  # consumo grátis não soma

    charged_product = _internal_product(session, actor, name="Botox Capilar")
    _stock_in(session, actor, charged_product.id, branch.id, Decimal("1000"))
    updated2 = orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(
            product_id=charged_product.id, quantity=Decimal("50"),
            item_type=OrderProductItemKind.CONSUMPTION, unit_price=Decimal("2.00"),
        ),
    )
    assert OrderRead.from_order(updated2).total == Decimal("400.00")  # 300 + 50*2


def test_venda_continua_exigindo_for_sale_true(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))

    with pytest.raises(ValidationDomainError):
        orders.add_product_item(
            session, actor, order.id,
            OrderProductItemCreate(product_id=product.id, quantity=Decimal("1"), item_type=OrderProductItemKind.SALE),
        )


def test_unit_price_no_payload_e_rejeitado_para_item_type_sale():
    with pytest.raises(ValueError):
        OrderProductItemCreate(
            product_id=uuid.uuid4(), quantity=Decimal("1"),
            item_type=OrderProductItemKind.SALE, unit_price=Decimal("10.00"),
        )


# ---------------------------------------------------------------------
# Fechamento — consumo gera INTERNAL_USE, venda continua gerando SALE,
# os dois em UMA MESMA comanda ficam DISTINTOS no ledger
# ---------------------------------------------------------------------


def test_fechamento_consumo_gera_movimento_internal_use_nao_sale(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))
    orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )

    closed = _close(session, actor, order)

    line = closed.product_items[0]
    assert line.stock_movement_id is not None
    movements = stock.list_movements(session, actor, product_id=product.id, branch_id=branch.id)
    movement = next(m for m in movements if m.id == line.stock_movement_id)
    assert movement.reason == StockMovementReason.INTERNAL_USE
    assert movement.direction == StockMovementDirection.OUT
    assert movement.order_id == order.id

    level = stock_level_repo.get(session, org_id, product.id, branch.id)
    assert level.quantity_on_hand == Decimal("820")  # 1000 - 180


def test_venda_e_consumo_do_mesmo_produto_na_mesma_comanda_geram_dois_movimentos_distintos(org_session):
    """Item explícito "produto vendido e consumo interno permanecem
    semanticamente distintos" — mesmo produto, uma linha SALE e uma
    CONSUMPTION, dois `StockMovement` com `reason` diferente."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = products.create_product(
        session, actor, ProductCreate(name="Óleo de Argan", cost_price=Decimal("10.00"), sale_price=Decimal("25.00"), for_sale=True)
    )
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))

    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))  # venda
    orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("30"), item_type=OrderProductItemKind.CONSUMPTION),
    )

    closed = _close(session, actor, order)
    movement_ids = {line.stock_movement_id for line in closed.product_items}
    assert len(movement_ids) == 2  # nunca a mesma movimentação pros dois

    movements = stock.list_movements(session, actor, product_id=product.id, branch_id=branch.id)
    reasons = {m.reason for m in movements if m.id in movement_ids}
    assert reasons == {StockMovementReason.SALE, StockMovementReason.INTERNAL_USE}


def test_consumo_nao_duplica_baixa_em_retry_de_fechamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))
    orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )
    closed = _close(session, actor, order)
    first_movement_id = closed.product_items[0].stock_movement_id

    from nexasalon_api.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        _close(session, actor, order)

    level = stock_level_repo.get(session, org_id, product.id, branch.id)
    assert level.quantity_on_hand == Decimal("820")  # baixou uma única vez
    reloaded = orders.get_order(session, actor, order.id)
    assert reloaded.product_items[0].stock_movement_id == first_movement_id


# ---------------------------------------------------------------------
# Correção pós-fechamento — movimento compensatório, ledger append-only
# ---------------------------------------------------------------------


def test_correcao_de_consumo_gera_movimento_compensatorio_sem_editar_original(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))
    orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )
    closed = _close(session, actor, order)
    item_id = closed.product_items[0].id
    original_movement_id = closed.product_items[0].stock_movement_id
    level_after_close = stock_level_repo.get(session, org_id, product.id, branch.id).quantity_on_hand
    assert level_after_close == Decimal("820")

    # Consumo real foi 20g A MAIS do que o registrado.
    corrected = orders.correct_consumption(
        session, actor, order.id, item_id,
        OrderConsumptionCorrection(quantity_delta=Decimal("20"), reason="Sobrou menos cabelo do que o esperado"),
    )

    # Item/linha original CONGELADOS — quantidade nunca muda.
    line = next(i for i in corrected.product_items if i.id == item_id)
    assert line.quantity == Decimal("180")
    assert line.stock_movement_id == original_movement_id  # movimento original intocado

    level_after_correction = stock_level_repo.get(session, org_id, product.id, branch.id).quantity_on_hand
    assert level_after_correction == Decimal("800")  # 820 - 20 (saída adicional)

    movements = stock.list_movements(session, actor, product_id=product.id, branch_id=branch.id)
    assert len(movements) == 3  # entrada inicial + baixa original + compensatório
    correction_movement = next(m for m in movements if m.order_id == order.id and m.reason == StockMovementReason.ADJUSTMENT)
    assert correction_movement.direction == StockMovementDirection.OUT
    assert correction_movement.quantity == Decimal("20")


def test_correcao_negativa_devolve_estoque(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))
    orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )
    closed = _close(session, actor, order)
    item_id = closed.product_items[0].id

    orders.correct_consumption(
        session, actor, order.id, item_id,
        OrderConsumptionCorrection(quantity_delta=Decimal("-30"), reason="Consumiu menos do que o registrado"),
    )
    level = stock_level_repo.get(session, org_id, product.id, branch.id)
    assert level.quantity_on_hand == Decimal("850")  # 820 + 30 devolvidos


def test_correcao_so_permitida_em_comanda_fechada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = _internal_product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("1000"))
    updated = orders.add_product_item(
        session, actor, order.id,
        OrderProductItemCreate(product_id=product.id, quantity=Decimal("180"), item_type=OrderProductItemKind.CONSUMPTION),
    )
    item_id = updated.product_items[0].id

    with pytest.raises(ValidationDomainError):
        orders.correct_consumption(
            session, actor, order.id, item_id,
            OrderConsumptionCorrection(quantity_delta=Decimal("10"), reason="Comanda ainda aberta"),
        )


def test_correcao_so_permitida_para_item_type_consumption(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, branch = _open_order(session, org_id, actor)
    product = products.create_product(
        session, actor, ProductCreate(name="Shampoo", cost_price=Decimal("5.00"), sale_price=Decimal("30.00"))
    )
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    closed = _close(session, actor, order)
    item_id = closed.product_items[0].id

    with pytest.raises(ValidationDomainError):
        orders.correct_consumption(
            session, actor, order.id, item_id,
            OrderConsumptionCorrection(quantity_delta=Decimal("1"), reason="Item é venda, não consumo"),
        )


def test_quantity_delta_zero_e_rejeitado_pelo_schema():
    with pytest.raises(ValueError):
        OrderConsumptionCorrection(quantity_delta=Decimal("0"), reason="Motivo qualquer")
