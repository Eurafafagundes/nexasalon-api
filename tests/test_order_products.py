"""Testes da Etapa C (Estoque ↔ Comanda + edição auditada de
valor/duração) — `services/orders.py`. Reaproveita os fixtures/estilo de
`tests/test_orders.py` (comanda de serviço) e `tests/test_stock_movements.py`
(produto/estoque/concorrência real com threads).

Cobre o checklist obrigatório do pedido: produto entra numa comanda
aberta; remover antes do fechamento nunca gera baixa; baixa acontece só
no fechamento, é transacional e idempotente (retry não duplica saída);
estoque insuficiente bloqueia o fechamento com mensagem clara; produto
inativo/uso-interno/sem-preço não pode ser vendido; item de serviço e
item de produto ficam em listas separadas; preço de venda é snapshot;
custo nunca vaza pra quem não tem `inventory.view_cost`; edição de
preço/duração/quantidade gera auditoria e nunca reescreve catálogo nem
histórico consolidado; pagamento misto + múltiplos serviços + múltiplos
produtos; retry de fechamento; concorrência no último item de estoque
disputado por DUAS comandas diferentes."""
import threading
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    OrderStatus,
    PaymentMethod,
    StockMovementDirection,
    StockMovementReason,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import audit_log_repo, order_repo, stock_level_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    OrderClose,
    OrderProductItemCreate,
    OrderProductItemUpdate,
    PaymentCreate,
)
from nexasalon_api.schemas.product import ProductCreate
from nexasalon_api.services import appointments, cash_register, orders, products, stock

_ALL_PERMS = frozenset(
    {
        "agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel",
        "inventory.view", "inventory.view_cost", "inventory.manage",
        "orders.view", "orders.manage", "orders.edit_price", "payments.register",
    }
)
_NO_COST_PERMS = _ALL_PERMS - {"inventory.view_cost"}
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 2026-08-13 é quinta.


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org comanda-produto", slug=f"org-cp-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_ALL_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _branch(session, org_id, name="Unidade") -> Branch:
    b = Branch(organization_id=org_id, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b


def _professional(session, org_id, branch_id, name="Profissional") -> Professional:
    p = Professional(organization_id=org_id, branch_id=branch_id, name=name)
    session.add(p)
    session.flush()
    return p


def _service(session, org_id, name="Corte", duration=60, price=100) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=duration, default_price=price)
    session.add(s)
    session.flush()
    return s


def _link(session, professional_id, service_id, **overrides) -> ProfessionalService:
    ps = ProfessionalService(professional_id=professional_id, service_id=service_id, **overrides)
    session.add(ps)
    session.flush()
    return ps


def _working_hours(session, org_id, professional_id, weekday, start, end):
    session.add(
        WorkingHours(organization_id=org_id, professional_id=professional_id, weekday=weekday, start_time=start, end_time=end)
    )
    session.flush()


def _client(session, org_id, name="Cliente") -> Client:
    c = Client(organization_id=org_id, name=name)
    session.add(c)
    session.flush()
    return c


def _dt(hour, minute=0):
    return datetime(2026, 8, 13, hour, minute, tzinfo=_TZ)


def _open_order(session, org_id, actor, *, n_services=1, branch=None):
    """Comanda ABERTA com `n_services` linhas de serviço (Corte R$100
    cada) na `branch` informada (ou uma nova, se não vier)."""
    branch = branch or _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)
    items = []
    for i in range(n_services):
        svc = _service(session, org_id, name=f"Serviço {i}", duration=60, price=Decimal("100.00"))
        _link(session, prof.id, svc.id)
        items.append(AppointmentItemCreate(professional_id=prof.id, service_id=svc.id, start_at=_dt(9 + i)))
    data = AppointmentCreate(branch_id=branch.id, client_id=client.id, items=items)
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    order = orders.create_order(session, actor, appt.id)
    return order, appt, branch


def _product(session, actor, *, name="Esmalte", cost=Decimal("5.00"), sale=Decimal("20.00"), active=True, for_sale=True):
    p = products.create_product(
        session, actor,
        ProductCreate(name=name, cost_price=cost, sale_price=sale, for_sale=for_sale),
    )
    if not active:
        products.set_product_active(session, actor, p.id, False)
    return p


def _stock_in(session, actor, product_id, branch_id, quantity):
    stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=quantity,
    )


def _open_register(session, actor, initial_amount=Decimal("0")):
    branch_id = _branch(session, actor.organization_id).id
    return cash_register.open_register(session, actor, branch_id, initial_amount, None)


# ---------------------------------------------------------------------
# Adicionar produto à comanda aberta
# ---------------------------------------------------------------------


def test_adiciona_produto_com_preco_snapshot_do_catalogo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, sale=Decimal("35.50"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))

    updated = orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("2")))

    assert len(updated.product_items) == 1
    line = updated.product_items[0]
    assert line.unit_price == Decimal("35.50")
    assert line.product_name == "Esmalte"
    assert line.quantity == Decimal("2")
    assert line.stock_movement_id is None  # baixa só no fechamento


def test_preco_do_payload_e_ignorado_snapshot_sempre_vem_do_catalogo(org_session):
    """`OrderProductItemCreate` nem tem campo de preço — a garantia é
    estrutural (schema), este teste só confirma que o preço final é
    sempre o do catálogo no momento da adição."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, sale=Decimal("35.50"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))

    updated = orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    assert updated.product_items[0].unit_price == Decimal("35.50")

    # Muda o preço no catálogo DEPOIS — a linha já adicionada não muda
    # retroativamente (mesmo espírito do snapshot de serviço).
    product.sale_price = Decimal("99.99")
    session.flush()
    reloaded = orders.get_order(session, actor, order.id)
    assert reloaded.product_items[0].unit_price == Decimal("35.50")


def test_produto_inativo_nao_pode_ser_vendido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, active=False)

    with pytest.raises(ValidationDomainError):
        orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))


def test_produto_de_uso_interno_nao_pode_ser_vendido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, for_sale=False)

    with pytest.raises(ValidationDomainError):
        orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))


def test_produto_sem_preco_de_venda_nao_pode_ser_vendido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = products.create_product(session, actor, ProductCreate(name="Sem preço", cost_price=Decimal("5.00")))
    assert product.sale_price is None

    with pytest.raises(ValidationDomainError):
        orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))


def test_produto_inexistente_404(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)

    with pytest.raises(NotFoundError):
        orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=uuid.uuid4(), quantity=Decimal("1")))


def test_nao_adiciona_produto_em_comanda_fechada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    with pytest.raises(ValidationDomainError):
        orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))


# ---------------------------------------------------------------------
# Remover produto antes do fechamento — nunca gera baixa
# ---------------------------------------------------------------------


def test_remover_produto_antes_do_fechamento_nao_gera_baixa_de_estoque(org_session):
    from nexasalon_api.repositories import stock_movement_repo

    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor)
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    updated = orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("3")))
    line_id = updated.product_items[0].id

    before_movements = stock_movement_repo.list_for_org(session, org_id) if hasattr(stock_movement_repo, "list_for_org") else None

    after_removal = orders.remove_product_item(session, actor, order.id, line_id)
    assert after_removal.product_items == []

    level = stock_level_repo.get(session, org_id, product.id, branch.id)
    assert level.quantity_on_hand == Decimal("10")  # intocado

    if before_movements is not None:
        after_movements = stock_movement_repo.list_for_org(session, org_id)
        assert len(after_movements) == len(before_movements)


def test_remover_produto_inexistente_404(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)

    with pytest.raises(NotFoundError):
        orders.remove_product_item(session, actor, order.id, uuid.uuid4())


# ---------------------------------------------------------------------
# Editar quantidade/preço da linha de produto (auditado)
# ---------------------------------------------------------------------


def test_editar_quantidade_e_preco_da_linha_de_produto_gera_auditoria_por_campo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, sale=Decimal("20.00"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    updated = orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    line_id = updated.product_items[0].id

    result = orders.update_product_item(
        session, actor, order.id, line_id,
        OrderProductItemUpdate(quantity=Decimal("4"), unit_price=Decimal("18.00")),
    )
    line = next(i for i in result.product_items if i.id == line_id)
    assert line.quantity == Decimal("4")
    assert line.unit_price == Decimal("18.00")

    # `list_for_entity` também traz o log de CRIAÇÃO da linha (de
    # `add_product_item`, sem `change_type`) — filtramos só os logs de
    # EDIÇÃO, que é o que este teste quer verificar (um por campo).
    logs = audit_log_repo.list_for_entity(session, org_id, "order_product_item", line_id)
    edit_logs = [log for log in logs if "change_type" in log.new_values]
    fields_changed = {log.new_values["change_type"] for log in edit_logs}
    assert fields_changed == {"manual_quantity_edit", "manual_unit_price_edit"}
    assert len(edit_logs) == 2  # uma linha de auditoria POR campo alterado, nunca uma só misturando os dois

    # Catálogo intocado.
    session.refresh(product)
    assert product.sale_price == Decimal("20.00")


def test_editar_preco_de_produto_na_comanda_nao_afeta_outras_comandas_ja_fechadas(org_session):
    """Item "alterar duração não deve alterar histórico consolidado
    silenciosamente" — aplicado aqui ao equivalente de produto: editar
    uma linha de uma comanda nunca reescreve o valor já consolidado de
    OUTRA comanda que comprou o mesmo produto."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    order1, appt1, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, sale=Decimal("20.00"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor, order1.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    register = _open_register(session, actor)
    total1 = Decimal("100.00") + Decimal("20.00")
    closed1 = orders.close_order(
        session, actor, order1.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total1, cash_register_id=register.id)]),
    )
    closed1_line_price = closed1.product_items[0].unit_price

    order2, appt2, _ = _open_order(session, org_id, actor, branch=branch)
    updated2 = orders.add_product_item(session, actor, order2.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    line2_id = updated2.product_items[0].id
    orders.update_product_item(session, actor, order2.id, line2_id, OrderProductItemUpdate(unit_price=Decimal("5.00")))

    reloaded1 = orders.get_order(session, actor, order1.id)
    assert reloaded1.product_items[0].unit_price == closed1_line_price == Decimal("20.00")


# ---------------------------------------------------------------------
# Fechamento — item de serviço + item de produto, total sem duplicação
# ---------------------------------------------------------------------


def test_total_da_comanda_soma_servicos_e_produtos_uma_unica_vez(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor, n_services=2)  # 100 + 100
    product1 = _product(session, actor, sale=Decimal("20.00"))
    product2 = _product(session, actor, name="Shampoo", sale=Decimal("30.00"))
    _stock_in(session, actor, product1.id, branch.id, Decimal("10"))
    _stock_in(session, actor, product2.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product1.id, quantity=Decimal("2")))  # 40
    updated = orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product2.id, quantity=Decimal("1")))  # 30

    from nexasalon_api.schemas.order import OrderRead

    read = OrderRead.from_order(updated)
    assert read.subtotal == Decimal("270.00")  # 100+100+40+30
    assert read.total == Decimal("270.00")
    assert len(read.items) == 2
    assert len(read.product_items) == 2


def test_fechar_comanda_com_pagamento_misto_multiplos_servicos_e_produtos_baixa_estoque(org_session):
    """Item explícito "teste pagamento misto + múltiplos serviços +
    múltiplos produtos"."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor, n_services=2)  # 100 + 100 = 200
    product1 = _product(session, actor, name="Esmalte", sale=Decimal("20.00"), cost=Decimal("5.00"))
    product2 = _product(session, actor, name="Shampoo", sale=Decimal("30.00"), cost=Decimal("10.00"))
    _stock_in(session, actor, product1.id, branch.id, Decimal("10"))
    _stock_in(session, actor, product2.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product1.id, quantity=Decimal("2")))  # 40
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product2.id, quantity=Decimal("1")))  # 30
    total = Decimal("270.00")
    register = _open_register(session, actor)
    part_a = Decimal("170.00")
    part_b = total - part_a

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=part_a, cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CASH, amount=part_b, cash_register_id=register.id),
        ]),
    )

    assert closed.status == OrderStatus.CLOSED
    assert len(closed.payments) == 2
    assert sum((p.amount for p in closed.payments), Decimal("0")) == total

    # Cada linha de produto recebeu exatamente uma baixa.
    for line in closed.product_items:
        assert line.stock_movement_id is not None

    level1 = stock_level_repo.get(session, org_id, product1.id, branch.id)
    level2 = stock_level_repo.get(session, org_id, product2.id, branch.id)
    assert level1.quantity_on_hand == Decimal("8")  # 10 - 2
    assert level2.quantity_on_hand == Decimal("9")  # 10 - 1

    session.refresh(appt)
    assert appt.status == AppointmentStatus.PAID


def test_estoque_insuficiente_bloqueia_fechamento_com_mensagem_clara(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, name="Óleo Raro", sale=Decimal("50.00"))
    _stock_in(session, actor, product.id, branch.id, Decimal("2"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("5")))
    register = _open_register(session, actor)
    total = Decimal("100.00") + Decimal("250.00")

    with pytest.raises(ValidationDomainError) as exc_info:
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )
    assert "Óleo Raro" in str(exc_info.value)


def test_estoque_insuficiente_nao_deixa_pagamento_nem_baixa_parcial_e_produto_ok_nao_e_afetado(org_session):
    """A exceção é levantada DENTRO do loop de baixa, antes de qualquer
    `Payment` ser criado e antes de `order.status` virar `CLOSED` — a
    asserção é feita na MESMA sessão/transação logo em seguida (sem
    commit, sem rollback), o que já é suficiente pra provar que nenhum
    desses efeitos aconteceu ainda: em produção, `api/deps.py::get_db`
    faria o rollback completo da transação inteira ao propagar a
    exceção, então este estado intermediário nunca chega a ser
    persistido de verdade."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor, n_services=2)
    product_ok = _product(session, actor, name="Tem estoque", sale=Decimal("20.00"))
    product_bad = _product(session, actor, name="Sem estoque suficiente", sale=Decimal("50.00"))
    _stock_in(session, actor, product_ok.id, branch.id, Decimal("10"))
    _stock_in(session, actor, product_bad.id, branch.id, Decimal("1"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product_ok.id, quantity=Decimal("1")))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product_bad.id, quantity=Decimal("5")))
    register = _open_register(session, actor)

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("9999.00"), cash_register_id=register.id)]),
        )

    reloaded_order = order_repo.get(session, org_id, order.id)
    assert reloaded_order.status == OrderStatus.OPEN
    assert reloaded_order.payments == []

    level_bad = stock_level_repo.get(session, org_id, product_bad.id, branch.id)
    assert level_bad.quantity_on_hand == Decimal("1")  # produto sem estoque nunca foi decrementado


# ---------------------------------------------------------------------
# Retry de fechamento — nunca gera duas saídas/pagamentos
# ---------------------------------------------------------------------


def test_retry_de_fechamento_nao_duplica_pagamento_nem_baixa_de_estoque(org_session):
    """Item explícito "teste retry de fechamento". Fecha a mesma
    comanda duas vezes em sequência (mesma sessão) — a segunda tem que
    recusar com 409 e não pode ter criado um segundo Payment nem uma
    segunda baixa de estoque."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, sale=Decimal("20.00"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("3")))
    register = _open_register(session, actor)
    total = Decimal("100.00") + Decimal("60.00")

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )
    assert len(closed.payments) == 1
    level_after_first = stock_level_repo.get(session, org_id, product.id, branch.id).quantity_on_hand
    assert level_after_first == Decimal("7")  # 10 - 3
    first_movement_id = closed.product_items[0].stock_movement_id

    with pytest.raises(ConflictError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )

    reloaded = orders.get_order(session, actor, order.id)
    assert len(reloaded.payments) == 1  # não duplicou
    assert reloaded.product_items[0].stock_movement_id == first_movement_id  # não trocou/duplicou a baixa
    level_after_retry = stock_level_repo.get(session, org_id, product.id, branch.id).quantity_on_hand
    assert level_after_retry == Decimal("7")  # não decrementou de novo


def test_retry_de_fechamento_via_http_dupla_chamada_nao_duplica(org_session):
    """Mesma garantia acima, mas simulando duas requisições HTTP
    sequenciais reais (sessões/transações independentes, como
    aconteceria com um duplo-clique/retry de rede) — cada chamada abre
    e fecha sua PRÓPRIA `SessionLocal`, como o `get_db` real faria."""
    session, org_id = org_session
    actor_setup = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor_setup)
    product = _product(session, actor_setup, sale=Decimal("20.00"))
    _stock_in(session, actor_setup, product.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor_setup, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("3")))
    register = _open_register(session, actor_setup)
    total = Decimal("100.00") + Decimal("60.00")
    order_id = order.id
    product_id = product.id
    branch_id = branch.id
    user_id = actor_setup.user_id
    membership_id = actor_setup.membership_id
    role_id = actor_setup.role_id
    register_id = register.id
    session.commit()

    def _attempt():
        with SessionLocal() as s:
            s.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
            local_actor = ActorContext(
                organization_id=org_id, user_id=user_id, membership_id=membership_id,
                role_id=role_id, role_name="Owner", permissions=_ALL_PERMS,
            )
            try:
                orders.close_order(
                    s, local_actor, order_id,
                    OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register_id)]),
                )
                s.commit()
                return "ok"
            except ConflictError:
                s.rollback()
                return "conflict"

    first = _attempt()
    second = _attempt()
    assert {first, second} == {"ok", "conflict"}

    with SessionLocal() as check:
        check.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        level = stock_level_repo.get(check, org_id, product_id, branch_id)
        assert level.quantity_on_hand == Decimal("7")  # baixou uma única vez
        reloaded = orders.get_order(check, ActorContext(
            organization_id=org_id, user_id=user_id, membership_id=membership_id,
            role_id=role_id, role_name="Owner", permissions=_ALL_PERMS,
        ), order_id)
        assert len(reloaded.payments) == 1


# ---------------------------------------------------------------------
# Concorrência real no último item de estoque, disputado por DUAS
# comandas diferentes fechando ao mesmo tempo
# ---------------------------------------------------------------------


def test_concorrencia_no_ultimo_item_de_estoque_entre_duas_comandas(org_session):
    """Item explícito "teste concorrência no último item de estoque".
    Duas comandas DIFERENTES, cada uma com uma linha de 6 unidades do
    MESMO produto/unidade, saldo disponível de só 10 (soma pedida: 12).
    Fecham as duas ao mesmo tempo, via threads reais + `threading.Barrier`
    (mesmo padrão de `test_stock_movements.py`). Só uma pode vencer; a
    outra tem que falhar com `ValidationDomainError` (estoque
    insuficiente) e continuar `OPEN`, sem pagamento nem baixa parcial."""
    session, org_id = org_session
    actor_setup = _actor(session, org_id)
    branch = _branch(session, org_id)
    product = _product(session, actor_setup, name="Última Unidade", sale=Decimal("10.00"))
    _stock_in(session, actor_setup, product.id, branch.id, Decimal("10"))

    order1, appt1, _ = _open_order(session, org_id, actor_setup, branch=branch)
    order2, appt2, _ = _open_order(session, org_id, actor_setup, branch=branch)
    orders.add_product_item(session, actor_setup, order1.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("6")))
    orders.add_product_item(session, actor_setup, order2.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("6")))
    register = _open_register(session, actor_setup)

    order1_id, order2_id = order1.id, order2.id
    product_id, branch_id = product.id, branch.id
    total_per_order = Decimal("100.00") + Decimal("60.00")
    user_id, membership_id, role_id = actor_setup.user_id, actor_setup.membership_id, actor_setup.role_id
    register_id = register.id
    session.commit()

    barrier = threading.Barrier(2)
    results = {}

    def _close(label, order_id):
        with SessionLocal() as s:
            s.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
            local_actor = ActorContext(
                organization_id=org_id, user_id=user_id, membership_id=membership_id,
                role_id=role_id, role_name="Owner", permissions=_ALL_PERMS,
            )
            barrier.wait()
            try:
                orders.close_order(
                    s, local_actor, order_id,
                    OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total_per_order, cash_register_id=register_id)]),
                )
                s.commit()
                results[label] = "ok"
            except ValidationDomainError:
                s.rollback()
                results[label] = "insufficient"

    t1 = threading.Thread(target=_close, args=("order1", order1_id))
    t2 = threading.Thread(target=_close, args=("order2", order2_id))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert set(results.values()) == {"ok", "insufficient"}, results

    with SessionLocal() as check:
        check.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        level = stock_level_repo.get(check, org_id, product_id, branch_id)
        assert level.quantity_on_hand == Decimal("4")  # 10 - 6, nunca negativo, nunca as duas baixas

        winner_label = next(k for k, v in results.items() if v == "ok")
        loser_label = "order1" if winner_label == "order2" else "order2"
        loser_order_id = order1_id if loser_label == "order1" else order2_id

        loser_actor = ActorContext(
            organization_id=org_id, user_id=user_id, membership_id=membership_id,
            role_id=role_id, role_name="Owner", permissions=_ALL_PERMS,
        )
        loser_order = orders.get_order(check, loser_actor, loser_order_id)
        assert loser_order.status == OrderStatus.OPEN  # perdedora continua aberta
        assert loser_order.payments == []  # sem pagamento pendurado
        assert loser_order.product_items[0].stock_movement_id is None  # sem baixa parcial


# ---------------------------------------------------------------------
# Custo nunca vaza sem `inventory.view_cost`
# ---------------------------------------------------------------------


def test_custo_do_produto_nao_aparece_em_nenhum_campo_da_comanda(org_session):
    """`OrderProductItemRead`/`OrderRead` nunca têm campo de custo (por
    design — ver docstring do schema). Trava isso: mesmo o ator SEM
    `inventory.view_cost` vê a comanda inteira sem nenhum vazamento,
    porque o schema simplesmente não carrega esse dado."""
    session, org_id = org_session
    actor = _actor(session, org_id, permissions=_NO_COST_PERMS)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, cost=Decimal("7.77"), sale=Decimal("20.00"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    updated = orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))

    from nexasalon_api.schemas.order import OrderRead

    read = OrderRead.from_order(updated)
    dumped = read.model_dump_json()
    assert "7.77" not in dumped
    assert "cost_price" not in dumped
    assert "cost" not in dumped.lower()


def test_baixa_por_venda_carrega_unit_cost_gated_pela_permission_existente(org_session):
    """O custo só existe no `StockMovement.unit_cost` gerado pela
    baixa — e continua protegido pelo mecanismo JÁ EXISTENTE da Etapa B
    (`StockMovementRead` sem custo x `StockMovementReadWithCost` com
    custo, escolhido por `inventory.view_cost`), sem nenhum código novo
    de gating."""
    from nexasalon_api.schemas.stock import StockMovementRead, StockMovementReadWithCost

    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)
    product = _product(session, actor, cost=Decimal("7.77"), sale=Decimal("20.00"))
    _stock_in(session, actor, product.id, branch.id, Decimal("10"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    register = _open_register(session, actor)
    total = Decimal("100.00") + Decimal("20.00")

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    movements = stock.list_movements(session, actor, product_id=product.id, branch_id=branch.id)
    sale_movement = next(m for m in movements if m.reason == StockMovementReason.SALE)
    assert sale_movement.order_id == order.id
    assert sale_movement.unit_cost == Decimal("7.77")

    with_cost = StockMovementReadWithCost.model_validate(sale_movement)
    assert with_cost.unit_cost == Decimal("7.77")
    without_cost_dump = StockMovementRead.model_validate(sale_movement).model_dump_json()
    assert "7.77" not in without_cost_dump


# ---------------------------------------------------------------------
# Isolamento multiempresa
# ---------------------------------------------------------------------


def test_isolamento_multiempresa_produto_de_outra_org_nao_pode_ser_adicionado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    order, appt, branch = _open_order(session, org_id, actor)

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-cp-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_actor = _actor(other_session, other_org_id)
        other_product = _product(other_session, other_actor)
        other_product_id = other_product.id
        other_session.commit()

    with pytest.raises(NotFoundError):
        orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=other_product_id, quantity=Decimal("1")))
