"""Testes do Extrato (`services/extract.py`, item 17/18/19/20/30 da
rodada "evolução funcional"). Cobre os riscos explicitamente listados
pelo usuário: comanda com múltiplos serviços/pagamento misto não
duplica faturamento; despesa manual não é faturamento; Permuta não
aumenta caixa (coberto em test_cash_register.py, aqui só a parte de
Extrato); filtro de datas; isolamento multiempresa.

Reaproveita os helpers de `test_orders.py` (mesmo padrão: direto no
service layer via `SessionLocal`, sem rota HTTP)."""
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.appointment import Appointment, AppointmentItem
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CardBrand,
    CashMovementType,
    OrderStatus,
    PaymentMethod,
    StockMovementDirection,
    StockMovementReason,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.order import Order
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import client_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    OrderClose,
    OrderProductItemCreate,
    PaymentCreate,
)
from nexasalon_api.schemas.product import ProductCreate
from nexasalon_api.services import (
    appointments,
    cash_register,
    extract,
    orders,
    products,
    stock,
)

_ALL_AGENDA_PERMS = frozenset(
    {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel"}
)
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 2026-08-13 é quinta.


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org extrato", slug=f"org-extrato-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_ALL_AGENDA_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Teste")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _branch(session, org_id) -> Branch:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
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


def _finished_appointment_with_two_services(session, org_id, actor, client_name="Cliente"):
    # Etapa H ("exigir caixa aberto para criar Comanda", padrão ON) —
    # ver docstring equivalente em `test_orders.py`.
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id)
    corte = _service(session, org_id, name="Manutenção", duration=60, price=Decimal("300.00"))
    mechas = _service(session, org_id, name="Mechas", duration=90, price=Decimal("500.00"))
    _link(session, prof.id, corte.id)
    _link(session, prof.id, mechas.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id, name=client_name)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[
            AppointmentItemCreate(professional_id=prof.id, service_id=corte.id, start_at=_dt(9, 0)),
            AppointmentItemCreate(professional_id=prof.id, service_id=mechas.id, start_at=_dt(11, 0)),
        ],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt, branch, prof, client


def _open_register(session, actor, initial_amount=Decimal("0")):
    branch_id = _branch(session, actor.organization_id).id
    return cash_register.open_register(session, actor, branch_id, initial_amount, None)


def _product_with_stock(session, actor, branch_id, *, name="Shampoo", sale=Decimal("40.00"), quantity=Decimal("10")):
    """Produto vendável + estoque disponível — mesmo padrão de
    `test_order_products.py`, pra `close_order` conseguir baixar
    estoque de verdade (não uma anotação) ao fechar a comanda."""
    product = products.create_product(
        session, actor, ProductCreate(name=name, cost_price=Decimal("5.00"), sale_price=sale, for_sale=True)
    )
    stock.record_movement(
        session, actor, product_id=product.id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=quantity,
    )
    return product


def _finished_appointment_one_service(session, org_id, actor, client_name="Cliente", price=Decimal("300.00")):
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id)
    service = _service(session, org_id, name="Corte", duration=60, price=price)
    _link(session, prof.id, service.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id, name=client_name)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(9, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt, branch, prof, client


def _finished_appointment_two_services_two_professionals(session, org_id, actor, client_name="Cliente"):
    """Mesmo espírito de `_finished_appointment_with_two_services`, mas
    com DOIS profissionais distintos — exatamente o cenário do pedido
    (Manutenção/Ianka + Corte/Duda) que `_finished_appointment_with_two_services`
    não cobre (usa o MESMO profissional pros dois serviços)."""
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    ianka = _professional(session, org_id, branch.id, name="Ianka")
    duda = _professional(session, org_id, branch.id, name="Duda")
    manutencao = _service(session, org_id, name="Manutenção Mega Hair 1 Tela", duration=120, price=Decimal("230.00"))
    corte = _service(session, org_id, name="Corte", duration=30, price=Decimal("100.00"))
    _link(session, ianka.id, manutencao.id)
    _link(session, duda.id, corte.id)
    _working_hours(session, org_id, ianka.id, _THURSDAY, time(9, 0), time(20, 0))
    _working_hours(session, org_id, duda.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id, name=client_name)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[
            AppointmentItemCreate(professional_id=ianka.id, service_id=manutencao.id, start_at=_dt(9, 0)),
            AppointmentItemCreate(professional_id=duda.id, service_id=corte.id, start_at=_dt(9, 0)),
        ],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt, branch, client


# ---------------------------------------------------------------------
# Comanda com múltiplos serviços/pagamento misto = UMA linha só
# ---------------------------------------------------------------------


def test_comanda_com_dois_servicos_e_pagamento_misto_vira_uma_linha_so(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor, client_name="Ana Souza")
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    assert total == Decimal("800.00")
    part_a = Decimal("300.00")
    part_b = total - part_a

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=part_a, cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=part_b, card_brand=CardBrand.VISA, cash_register_id=register.id),
        ]),
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)

    assert len(summary.sales) == 1
    assert summary.revenue_total == Decimal("800.00")  # não 1600.00
    row = summary.sales[0]
    assert row.client_id == order.client_id
    assert "Manutenção" in {i.service_name for i in row.items}
    assert "Mechas" in {i.service_name for i in row.items}
    assert {p.method for p in row.payments} == {PaymentMethod.PIX, PaymentMethod.CREDIT}


def test_extract_sale_row_resume_servicos_e_pagamentos_sem_duplicar(org_session):
    from nexasalon_api.schemas.extract import ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor, client_name="Ana Souza")
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("300.00"), cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("500.00"), card_brand=CardBrand.VISA, cash_register_id=register.id),
        ]),
    )
    session.refresh(order)

    row = ExtractSaleRow.from_order(order, "Ana Souza")

    assert row.total == Decimal("800.00")
    assert "Manutenção" in row.services_summary and "Mechas" in row.services_summary
    assert row.services_summary.count("Manutenção") == 1
    assert "pix" in row.payment_methods_summary and "credit" in row.payment_methods_summary


# ---------------------------------------------------------------------
# Despesa/entrada manual não é faturamento
# ---------------------------------------------------------------------


def test_despesa_manual_reduz_expense_total_mas_nao_e_faturamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    register = _open_register(session, actor, initial_amount=Decimal("100.00"))

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.WITHDRAWAL, Decimal("50.00"), "Compra de produtos",
        category="Produtos",
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)

    assert summary.revenue_total == Decimal("0")  # nenhuma comanda paga
    assert summary.expense_total == Decimal("50.00")
    assert summary.result == Decimal("-50.00")


def test_entrada_manual_nao_soma_em_revenue_total(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    register = _open_register(session, actor)

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.SUPPLY, Decimal("200.00"), "Aporte inicial extra",
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)

    assert summary.revenue_total == Decimal("0")  # entrada manual != faturamento (item 21)
    assert summary.expense_total == Decimal("0")
    assert len(summary.movements) == 1


# ---------------------------------------------------------------------
# Filtro de data
# ---------------------------------------------------------------------


def test_filtro_de_data_exclui_comanda_fora_do_periodo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    future_from = datetime.now(timezone.utc) + timedelta(days=30)
    summary = extract.get_extract(session, actor, date_from=future_from, date_to=None)

    assert summary.sales == []
    assert summary.revenue_total == Decimal("0")


def test_filtro_de_data_inclui_comanda_dentro_do_periodo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    past_from = datetime.now(timezone.utc) - timedelta(days=1)
    future_to = datetime.now(timezone.utc) + timedelta(days=1)
    summary = extract.get_extract(session, actor, date_from=past_from, date_to=future_to)

    assert len(summary.sales) == 1
    assert summary.revenue_total == total


# ---------------------------------------------------------------------
# Isolamento multiempresa
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Etapa N1 — `row_type` (Todos/Vendas/Entradas/Despesas): mesmo filtro
# usado pela tela E pela exportação Excel (`build_extract_workbook`
# reaproveita `get_extract` integralmente) — nunca duas lógicas.
# ---------------------------------------------------------------------


def _sales_and_movements_org(session, org_id, actor):
    """Uma comanda fechada (venda) + uma entrada manual + uma despesa
    manual, na mesma organização — pra testar que `row_type` recorta
    CADA combinação sem afetar as outras nem os totais."""
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor, client_name="Cliente N1")
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )
    cash_register.register_movement(
        session, actor, register.id, CashMovementType.SUPPLY, Decimal("50.00"), "Aporte",
    )
    cash_register.register_movement(
        session, actor, register.id, CashMovementType.WITHDRAWAL, Decimal("30.00"), "Compra",
    )


def test_row_type_sales_so_mostra_vendas_mas_nao_muda_os_totais(org_session):
    from nexasalon_api.schemas.extract import ExtractRowType

    session, org_id = org_session
    actor = _actor(session, org_id)
    _sales_and_movements_org(session, org_id, actor)

    all_summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    filtered = extract.get_extract(session, actor, date_from=None, date_to=None, row_type=ExtractRowType.SALES)

    assert len(filtered.sales) == 1
    assert filtered.movements == []
    # Totais (cards de resumo) NUNCA mudam com o filtro de linhas —
    # sempre o período inteiro, igual antes deste filtro existir.
    assert filtered.revenue_total == all_summary.revenue_total
    assert filtered.expense_total == all_summary.expense_total
    assert filtered.result == all_summary.result


def test_row_type_withdrawal_so_mostra_despesas(org_session):
    from nexasalon_api.schemas.extract import ExtractRowType

    session, org_id = org_session
    actor = _actor(session, org_id)
    _sales_and_movements_org(session, org_id, actor)

    filtered = extract.get_extract(session, actor, date_from=None, date_to=None, row_type=ExtractRowType.WITHDRAWAL)

    assert filtered.sales == []
    assert len(filtered.movements) == 1
    assert filtered.movements[0].type == CashMovementType.WITHDRAWAL


def test_row_type_supply_so_mostra_entradas(org_session):
    from nexasalon_api.schemas.extract import ExtractRowType

    session, org_id = org_session
    actor = _actor(session, org_id)
    _sales_and_movements_org(session, org_id, actor)

    filtered = extract.get_extract(session, actor, date_from=None, date_to=None, row_type=ExtractRowType.SUPPLY)

    assert filtered.sales == []
    assert len(filtered.movements) == 1
    assert filtered.movements[0].type == CashMovementType.SUPPLY


def test_row_type_all_e_none_sao_equivalentes_e_mantem_tudo(org_session):
    from nexasalon_api.schemas.extract import ExtractRowType

    session, org_id = org_session
    actor = _actor(session, org_id)
    _sales_and_movements_org(session, org_id, actor)

    without_filter = extract.get_extract(session, actor, date_from=None, date_to=None)
    with_all = extract.get_extract(session, actor, date_from=None, date_to=None, row_type=ExtractRowType.ALL)

    assert len(without_filter.sales) == len(with_all.sales) == 1
    assert len(without_filter.movements) == len(with_all.movements) == 2


def test_extract_sale_row_payment_methods_lista_estruturada_para_traducao(org_session):
    """`payment_methods` (lista) existe ao lado de `payment_methods_summary`
    (string legada) — o frontend usa a lista pra traduzir cada método
    com `PAYMENT_METHOD_LABELS` sem fazer split de string."""
    from nexasalon_api.schemas.extract import ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor, client_name="Ana Souza")
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("300.00"), cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("500.00"), card_brand=CardBrand.VISA, cash_register_id=register.id),
        ]),
    )
    session.refresh(order)

    row = ExtractSaleRow.from_order(order, "Ana Souza")

    assert row.payment_methods == [PaymentMethod.PIX, PaymentMethod.CREDIT]
    assert len(row.payment_methods) == 2  # sem duplicar, mesmo dedup de `payment_methods_summary`


# ---------------------------------------------------------------------
# Etapa N2 — granularidade por serviço/profissional (`ExtractSaleRow.items`,
# `OrderItem` exposto tal como está, nunca vira uma segunda venda).
# ---------------------------------------------------------------------


def test_comanda_com_um_servico_gera_um_unico_item(org_session):
    from nexasalon_api.schemas.extract import ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id, name="Duda")
    corte = _service(session, org_id, name="Corte", duration=30, price=Decimal("100.00"))
    _link(session, prof.id, corte.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id, name="Cliente Único Serviço")
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=corte.id, start_at=_dt(9, 0))],
        ),
    )
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("100.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)

    row = ExtractSaleRow.from_order(order, "Cliente Único Serviço")

    assert len(row.items) == 1
    assert row.items[0].service_name == "Corte"
    assert row.items[0].professional_name == "Duda"
    assert row.items[0].price == Decimal("100.00")


def test_comanda_com_dois_servicos_dois_profissionais_gera_dois_itens_corretos(org_session):
    """Cenário exato do pedido: Manutenção Mega Hair 1 Tela/Ianka/R$230
    + Corte/Duda/R$100 — cada item com seu próprio serviço, profissional
    e valor, sem strings concatenadas ("Duda + Ianka")."""
    from nexasalon_api.schemas.extract import ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment_two_services_two_professionals(
        session, org_id, actor, client_name="Maria"
    )
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("330.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)

    row = ExtractSaleRow.from_order(order, "Maria")

    assert len(row.items) == 2
    by_service = {item.service_name: item for item in row.items}
    assert by_service["Manutenção Mega Hair 1 Tela"].professional_name == "Ianka"
    assert by_service["Manutenção Mega Hair 1 Tela"].price == Decimal("230.00")
    assert by_service["Corte"].professional_name == "Duda"
    assert by_service["Corte"].price == Decimal("100.00")
    # cada item referencia service_id/professional_id/order_item_id
    # ESTRUTURADOS (dado relacional), não strings concatenadas.
    assert all(item.service_id and item.professional_id and item.order_item_id for item in row.items)

    # soma dos itens == total da comanda (nunca uma segunda fonte de valor).
    assert sum((item.price for item in row.items), Decimal("0")) == row.total == Decimal("330.00")
    # continua sendo UMA única venda/comanda no Extrato.
    summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    assert len(summary.sales) == 1
    # nenhum Payment duplicado — 1 pagamento registrado, não 2 (um por item).
    assert len(order.payments) == 1


def test_produto_da_comanda_nunca_aparece_misturado_com_servico(org_session):
    """Ajuste pós-review N2 — `product_items` é uma lista SEPARADA de
    `items` (serviços): uma análise de BI que soma `items` nunca inclui
    produto por engano, e vice-versa."""
    from nexasalon_api.models.product import Product
    from nexasalon_api.schemas.extract import ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor, client_name="Ana Souza")
    order = orders.create_order(session, actor, appt.id)

    product = Product(organization_id=org_id, name="Shampoo")
    session.add(product)
    session.flush()
    from nexasalon_api.models.order import OrderProductItem

    session.add(
        OrderProductItem(
            organization_id=org_id, order_id=order.id, product_id=product.id,
            quantity=Decimal("2"), unit_price=Decimal("15.00"), product_name="Shampoo",
        )
    )
    session.flush()

    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("830.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)

    row = ExtractSaleRow.from_order(order, "Ana Souza")

    assert len(row.items) == 2  # só serviços
    assert all(i.service_name != "Shampoo" for i in row.items)
    assert len(row.product_items) == 1  # só produtos, lista separada
    assert row.product_items[0].product_name == "Shampoo"
    assert row.product_items[0].price == Decimal("30.00")


def test_excel_discrimina_item_servico_de_item_produto_por_coluna_estruturada(org_session):
    """A coluna "Item" do Excel (Serviço/Produto) é o discriminador
    ESTRUTURADO — nunca depender só do texto livre de "Serviço/Produto"
    pra saber o tipo numa análise de BI."""
    from nexasalon_api.models.order import OrderProductItem
    from nexasalon_api.models.product import Product
    from nexasalon_api.services.extract import _HEADER, _sale_item_rows

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor, client_name="Ana Souza")
    order = orders.create_order(session, actor, appt.id)

    product = Product(organization_id=org_id, name="Shampoo")
    session.add(product)
    session.flush()
    session.add(
        OrderProductItem(
            organization_id=org_id, order_id=order.id, product_id=product.id,
            quantity=Decimal("1"), unit_price=Decimal("30.00"), product_name="Shampoo",
        )
    )
    session.flush()

    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("830.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)

    rows = _sale_item_rows(order, "Ana Souza", "Matriz")
    item_col = _HEADER.index("Item")
    servico_col = _HEADER.index("Serviço/Produto")

    item_types = {r[servico_col]: r[item_col] for r in rows}
    assert item_types["Manutenção"] == "Serviço"
    assert item_types["Mechas"] == "Serviço"
    assert item_types["Shampoo"] == "Produto"


# ---------------------------------------------------------------------
# Etapa N4.1 — Faturamento (revenue_total/ExtractSaleRow.total) passa a
# incluir PRODUTO (`OrderProductItem`). Corrige um bug: a fórmula
# somava só `OrderItem.price`, excluindo produto vendido — mesmo com
# baixa REAL de estoque no fechamento (venda de verdade). Fórmula
# agora é a canônica compartilhada (`services/order_totals.py`), a
# MESMA usada por `close_order`/`OrderRead`.
# ---------------------------------------------------------------------


def test_revenue_total_somente_servico_sem_produto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, _prof, _client = _finished_appointment_one_service(session, org_id, actor, price=Decimal("300.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("300.00"), cash_register_id=register.id)]),
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    assert summary.revenue_total == Decimal("300.00")


def test_revenue_total_soma_servico_mais_produto(org_session):
    """Serviço R$300 + Produto R$40 numa MESMA comanda de R$340 — o
    Extrato não pode mostrar só R$300 (bug corrigido na N4.1)."""
    from nexasalon_api.schemas.extract import ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, _prof, _client = _finished_appointment_one_service(session, org_id, actor, price=Decimal("300.00"))
    order = orders.create_order(session, actor, appt.id)
    product = _product_with_stock(session, actor, branch.id, sale=Decimal("40.00"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("340.00"), cash_register_id=register.id)]),
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    assert summary.revenue_total == Decimal("340.00")
    row = ExtractSaleRow.from_order(summary.sales[0], "Cliente")
    assert row.total == Decimal("340.00")


def test_revenue_total_comanda_somente_produto_sem_nenhum_servico(org_session):
    """Comanda sem NENHUM `OrderItem` (só produto) — não é um estado
    alcançável hoje pelo fluxo real (`create_order` sempre copia
    serviço(s) do Appointment, e não existe remoção de OrderItem
    depois de criado), mas o Extrato não deve depender disso pra somar
    corretamente: se um dia esse estado existir (ou for construído
    fora do fluxo normal, como aqui), o produto ainda precisa compor
    `revenue_total`."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, _prof, _client = _finished_appointment_one_service(session, org_id, actor, price=Decimal("300.00"))
    order = orders.create_order(session, actor, appt.id)
    # Remove o próprio item de serviço direto no banco (fora do fluxo
    # normal — não existe endpoint pra isso) só pra simular a comanda
    # "só produto" e provar que a fórmula não depende de haver serviço.
    for item in list(order.items):
        session.delete(item)
    session.flush()
    session.expire(order)  # força `order.items` a recarregar vazio (evita a coleção em memória "stale").
    product = _product_with_stock(session, actor, branch.id, sale=Decimal("40.00"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("40.00"), cash_register_id=register.id)]),
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    assert summary.revenue_total == Decimal("40.00")


def test_revenue_total_dois_servicos_mais_produto_nao_duplica_nenhum_item(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, _client = _finished_appointment_two_services_two_professionals(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    product = _product_with_stock(session, actor, branch.id, sale=Decimal("50.00"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("380.00"), cash_register_id=register.id)]),
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    assert len(summary.sales) == 1
    assert summary.revenue_total == Decimal("380.00")  # 230 + 100 + 50, nunca duplicado.


def test_taxa_de_pagamento_continua_vinda_so_do_payment_com_produto_no_total(org_session):
    """Serviço R$300 + Produto R$40 (comanda R$340), paga em crédito
    com taxa calculada sobre o Payment inteiro — incluir produto no
    `total` não muda de onde vem a taxa: `payments_known_fee_total`
    continua vindo exclusivamente do snapshot do `Payment`, nunca de um
    rateio por item de serviço/produto."""
    from nexasalon_api.schemas.extract import ExtractSaleRow
    from nexasalon_api.schemas.payment_fee_rule import PaymentFeeRuleCreate
    from nexasalon_api.services import payment_fee_rules

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, _prof, _client = _finished_appointment_one_service(session, org_id, actor, price=Decimal("300.00"))
    order = orders.create_order(session, actor, appt.id)
    product = _product_with_stock(session, actor, branch.id, sale=Decimal("40.00"))
    orders.add_product_item(session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))
    register = _open_register(session, actor)
    payment_fee_rules.create_rule(
        session, org_id,
        PaymentFeeRuleCreate(method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99")),
    )
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("340.00"), card_brand=CardBrand.VISA, cash_register_id=register.id),
        ]),
    )

    summary = extract.get_extract(session, actor, date_from=None, date_to=None)
    assert summary.revenue_total == Decimal("340.00")
    row = ExtractSaleRow.from_order(summary.sales[0], "Cliente")
    assert row.total == Decimal("340.00")
    # taxa = 340 * 2.99% = 10.166 -> 10.17 (ROUND_HALF_EVEN, 2 casas).
    assert row.payments_known_fee_total == Decimal("10.17")
    assert row.payments_net_total == Decimal("329.83")
    assert row.has_unconfigured_fee is False


def test_row_type_sales_preserva_items_por_comanda(org_session):
    """As linhas de N1 (`row_type`) continuam funcionando com o campo
    `items` novo presente — filtro de tipo e granularidade por item são
    ortogonais, um não quebra o outro."""
    from nexasalon_api.schemas.extract import ExtractRowType, ExtractSaleRow

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment_two_services_two_professionals(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("330.00"), cash_register_id=register.id)]),
    )

    filtered = extract.get_extract(session, actor, date_from=None, date_to=None, row_type=ExtractRowType.SALES)
    assert len(filtered.sales) == 1
    row = ExtractSaleRow.from_order(filtered.sales[0], "Cliente")
    assert len(row.items) == 2


def test_extrato_nao_vaza_entre_organizacoes():
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
        session.add(Organization(id=org_a, name="Org A", slug=f"org-a-{org_a.hex[:8]}"))
        session.flush()
        actor_a = _actor(session, org_a)
        appt, *_ = _finished_appointment_with_two_services(session, org_a, actor_a)
        order = orders.create_order(session, actor_a, appt.id)
        register = _open_register(session, actor_a)
        total = sum((i.price for i in order.items), Decimal("0"))
        orders.close_order(
            session, actor_a, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )
        session.commit()

    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
        session.add(Organization(id=org_b, name="Org B", slug=f"org-b-{org_b.hex[:8]}"))
        session.flush()
        actor_b = _actor(session, org_b)

        summary = extract.get_extract(session, actor_b, date_from=None, date_to=None)

        assert summary.sales == []
        assert summary.revenue_total == Decimal("0")
        assert summary.movements == []


# ---------------------------------------------------------------------
# Item de performance ("quick win 3") — resolver os nomes dos clientes
# do período numa ÚNICA query (`WHERE id IN (...)`), nunca um
# `client_repo.get` por cliente distinto.
# ---------------------------------------------------------------------


def _bare_closed_order(session, org_id, branch_id, client_id, order_number, closed_at):
    """Comanda mínima direto via ORM (sem passar por `orders.create_order`/
    `close_order`) — suficiente pra testar só a resolução de NOME de
    cliente do Extrato, que não depende de item/pagamento nenhum."""
    appt = Appointment(organization_id=org_id, branch_id=branch_id, client_id=client_id, status=AppointmentStatus.PAID)
    session.add(appt)
    session.flush()
    session.add(
        AppointmentItem(
            organization_id=org_id, appointment_id=appt.id,
            service_id=_service(session, org_id, name=f"Serviço {order_number}").id,
            professional_id=_professional(session, org_id, branch_id).id,
            start_at=closed_at - timedelta(hours=1), end_at=closed_at, duration_minutes=60, price=Decimal("100.00"),
        )
    )
    order = Order(
        organization_id=org_id, order_number=order_number, appointment_id=appt.id,
        branch_id=branch_id, client_id=client_id, status=OrderStatus.CLOSED, closed_at=closed_at,
    )
    session.add(order)
    session.flush()
    return order


def test_extrato_resolve_nomes_de_cliente_em_lote_nunca_um_por_um(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    clients = [_client(session, org_id, name=f"Cliente {i}") for i in range(5)]
    for i, client in enumerate(clients):
        _bare_closed_order(session, org_id, branch.id, client.id, 5000 + i, datetime(2026, 8, 10 + i, 12, tzinfo=_TZ))

    call_count = 0
    real_get = client_repo.get

    def _counting_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_get(*args, **kwargs)

    with patch.object(client_repo, "get", side_effect=_counting_get):
        summary = extract.get_extract(session, actor, date_from=None, date_to=None)

    # `client_repo.get` (busca individual) nunca deveria ser chamado
    # pelo Extrato — a resolução de nome usa `list_by_ids` (lote).
    assert call_count == 0
    assert set(summary.client_names.values()) == {c.name for c in clients}
    assert len(summary.client_names) == 5


def test_extrato_client_names_isolamento_de_tenant_preservado():
    """`list_by_ids` precisa continuar filtrando por `organization_id` —
    um cliente de OUTRA organização nunca aparece em `client_names`,
    mesmo que (hipoteticamente) seu id aparecesse no conjunto pedido."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a)})
        session.add(Organization(id=org_a, name="Org A Extrato", slug=f"org-a-ext-{org_a.hex[:8]}"))
        session.flush()
        client_a = _client(session, org_a, name="Cliente da Org A")
        client_a_id = client_a.id
        session.commit()

    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b)})
        session.add(Organization(id=org_b, name="Org B Extrato", slug=f"org-b-ext-{org_b.hex[:8]}"))
        session.flush()
        # Pede explicitamente o id do cliente da Org A, mas filtrando pela Org B.
        result = client_repo.list_by_ids(session, org_b, {client_a_id})

    assert result == []
