"""Etapa I — "Comandas relacionadas e fechamento consolidado".

Cobre os 6 itens do pedido:
  1. Comandas relacionadas (mesma cliente, mesmo dia, profissionais
     diferentes) — `orders.get_related_orders`/`get_order_related`.
  2. Alteração de status em lote — `appointments.update_status(scope=...)`.
  3. Fechamento consolidado transacional com split de pagamento —
     `orders.close_orders_consolidated`.
  4. Produto na comanda entra no total consolidado (reaproveita Etapa C).
  5. `OrderCancel.reason` agora obrigatório.
  6. Faltou cancela SÓ a comanda exclusiva daquele atendimento, nunca as
     relacionadas.
"""
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, ValidationDomainError
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
from nexasalon_api.repositories import stock_level_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    ConsolidatedOrderClose,
    OrderCancel,
    OrderProductItemCreate,
    PaymentCreate,
)
from nexasalon_api.schemas.product import ProductCreate
from nexasalon_api.services import appointments, cash_register, orders, products, stock

_ALL_PERMS = frozenset(
    {
        "agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel",
        "inventory.view", "inventory.view_cost", "inventory.manage",
        "orders.view", "orders.manage", "orders.edit_price", "orders.cancel", "payments.register",
    }
)
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 2026-08-13 é quinta.


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org relacionadas", slug=f"org-rel-{org_id.hex[:8]}"))
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


def _service(session, org_id, name="Corte", duration=60, price=Decimal("100.00")) -> Service:
    s = Service(organization_id=org_id, name=name, default_duration_minutes=duration, default_price=price)
    session.add(s)
    session.flush()
    return s


def _link(session, professional_id, service_id) -> ProfessionalService:
    ps = ProfessionalService(professional_id=professional_id, service_id=service_id)
    session.add(ps)
    session.flush()
    return ps


def _working_hours(session, org_id, professional_id, weekday, start, end):
    session.add(
        WorkingHours(organization_id=org_id, professional_id=professional_id, weekday=weekday, start_time=start, end_time=end)
    )
    session.flush()


def _client(session, org_id, name="Amanda") -> Client:
    c = Client(organization_id=org_id, name=name)
    session.add(c)
    session.flush()
    return c


def _dt(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=_TZ)


def _app_weekday(day: int) -> int:
    """`0=domingo..6=sábado` (convenção do app) a partir do dia do mês
    em agosto/2026 — `datetime.weekday()` é `0=segunda..6=domingo`."""
    py_weekday = datetime(2026, 8, day, tzinfo=_TZ).weekday()
    return (py_weekday + 1) % 7


def _appointment_for_client(session, org_id, actor, branch, client, *, prof_name, service_name, price, start_hour, day=13):
    """Um agendamento com 1 serviço, pra `client`, com um profissional
    NOVO (ex.: Duda/Ianka) — simula o exemplo do pedido (Amanda ->
    Manutenção com Duda; Amanda -> Progressiva com Ianka)."""
    prof = _professional(session, org_id, branch.id, name=prof_name)
    svc = _service(session, org_id, name=service_name, price=price)
    _link(session, prof.id, svc.id)
    _working_hours(session, org_id, prof.id, _app_weekday(day), time(8, 0), time(21, 0))
    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=svc.id, start_at=_dt(day, start_hour))],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt, prof


def _setup_two_related_orders(session, org_id, actor):
    """Amanda com DUAS comandas hoje, profissionais diferentes — o
    exemplo exato do pedido."""
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    client = _client(session, org_id, name="Amanda")
    appt1, duda = _appointment_for_client(
        session, org_id, actor, branch, client, prof_name="Duda", service_name="Manutenção",
        price=Decimal("150.00"), start_hour=9,
    )
    appt2, ianka = _appointment_for_client(
        session, org_id, actor, branch, client, prof_name="Ianka", service_name="Progressiva",
        price=Decimal("300.00"), start_hour=13,
    )
    order1 = orders.create_order(session, actor, appt1.id)
    order2 = orders.create_order(session, actor, appt2.id)
    return branch, client, (appt1, order1, duda), (appt2, order2, ianka)


def _open_register_other_branch(session, actor):
    branch = _branch(session, actor.organization_id, name="Caixa")
    return cash_register.open_register(session, actor, branch.id, Decimal("0"), None)


# ---------------------------------------------------------------------
# Item 1 — Comandas relacionadas
# ---------------------------------------------------------------------


def test_get_related_orders_mesma_cliente_mesmo_dia_profissionais_diferentes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _branch, _client_obj, (_appt1, order1, _d), (_appt2, order2, _i) = _setup_two_related_orders(session, org_id, actor)

    related = orders.get_related_orders(session, actor, order1)
    assert [o.id for o in related] == [order2.id]

    order, related2, client_name = orders.get_order_related(session, actor, order1.id)
    assert order.id == order1.id
    assert client_name == "Amanda"
    assert [o.id for o in related2] == [order2.id]


def test_related_orders_ignora_cliente_diferente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    bruna = _client(session, org_id, name="Bruna")

    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    appt2, _ = _appointment_for_client(
        session, org_id, actor, branch, bruna, prof_name="Ianka", service_name="Corte",
        price=Decimal("50"), start_hour=10,
    )
    order1 = orders.create_order(session, actor, appt1.id)
    orders.create_order(session, actor, appt2.id)

    assert orders.get_related_orders(session, actor, order1) == []


def test_related_orders_ignora_dia_diferente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")

    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9, day=13,
    )
    appt2, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Ianka", service_name="Corte",
        price=Decimal("50"), start_hour=9, day=14,
    )
    order1 = orders.create_order(session, actor, appt1.id)
    orders.create_order(session, actor, appt2.id)

    assert orders.get_related_orders(session, actor, order1) == []


# ---------------------------------------------------------------------
# Item 2 — Alteração de status em lote
# ---------------------------------------------------------------------


def test_update_status_scope_only_this_nao_toca_relacionados(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    appt2, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Ianka", service_name="Escova",
        price=Decimal("50"), start_hour=13,
    )
    appt1.status = AppointmentStatus.SCHEDULED
    appt2.status = AppointmentStatus.SCHEDULED
    session.flush()

    appointments.update_status(session, actor, appt1.id, AppointmentStatus.CONFIRMED, scope="only_this")

    reloaded1 = appointments.get_appointment(session, actor, appt1.id)
    reloaded2 = appointments.get_appointment(session, actor, appt2.id)
    assert reloaded1.status == AppointmentStatus.CONFIRMED
    assert reloaded2.status == AppointmentStatus.SCHEDULED


def test_update_status_scope_all_related_aplica_a_todos(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    appt2, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Ianka", service_name="Escova",
        price=Decimal("50"), start_hour=13,
    )
    appt1.status = AppointmentStatus.SCHEDULED
    appt2.status = AppointmentStatus.SCHEDULED
    session.flush()

    appointments.update_status(session, actor, appt1.id, AppointmentStatus.CONFIRMED, scope="all_related")

    reloaded1 = appointments.get_appointment(session, actor, appt1.id)
    reloaded2 = appointments.get_appointment(session, actor, appt2.id)
    assert reloaded1.status == AppointmentStatus.CONFIRMED
    assert reloaded2.status == AppointmentStatus.CONFIRMED


def test_update_status_scope_all_related_nunca_toca_relacionado_pago_ou_cancelado(org_session):
    """Item explícito: relacionado terminal (PAID/CANCELLED) nunca é
    forçado — fica de fora silenciosamente, o lote não aborta por causa
    dele."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    appt2, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Ianka", service_name="Escova",
        price=Decimal("50"), start_hour=13,
    )
    appt1.status = AppointmentStatus.SCHEDULED
    appt2.status = AppointmentStatus.CANCELLED
    session.flush()

    appointments.update_status(session, actor, appt1.id, AppointmentStatus.CONFIRMED, scope="all_related")

    reloaded2 = appointments.get_appointment(session, actor, appt2.id)
    assert reloaded2.status == AppointmentStatus.CANCELLED


def test_get_related_appointments_vazio_quando_so_existe_um(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    assert appointments.get_related_appointments(session, actor, appt1.id) == []


# ---------------------------------------------------------------------
# Item 6 — Faltou cancela só a comanda exclusiva
# ---------------------------------------------------------------------


def test_no_show_cancela_comanda_exclusiva_sem_afetar_relacionada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _branch, _client_obj, (appt1, order1, _d), (_appt2, order2, _i) = _setup_two_related_orders(session, org_id, actor)
    appt1.status = AppointmentStatus.SCHEDULED
    session.flush()

    appointments.update_status(session, actor, appt1.id, AppointmentStatus.NO_SHOW)

    reloaded_order1 = orders.get_order(session, actor, order1.id)
    reloaded_order2 = orders.get_order(session, actor, order2.id)
    assert reloaded_order1.status == OrderStatus.CANCELLED
    assert reloaded_order2.status == OrderStatus.OPEN


def test_no_show_sem_comanda_aberta_nao_gera_erro(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    appt, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    appt.status = AppointmentStatus.SCHEDULED
    session.flush()
    reloaded = appointments.update_status(session, actor, appt.id, AppointmentStatus.NO_SHOW)
    assert reloaded.status == AppointmentStatus.NO_SHOW


# ---------------------------------------------------------------------
# Item 5 — Cancelar Comanda exige motivo
# ---------------------------------------------------------------------


def test_order_cancel_exige_motivo():
    with pytest.raises(ValidationError):
        OrderCancel()
    OrderCancel(reason="Aberta por engano")  # não levanta


# ---------------------------------------------------------------------
# Item 3/4 — Fechamento consolidado
# ---------------------------------------------------------------------


def test_close_orders_consolidated_happy_path_com_split_de_pagamento_e_produto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch, _client, (appt1, order1, _d), (appt2, order2, _i) = _setup_two_related_orders(session, org_id, actor)
    # order1 = Manutenção R$150 (Duda); order2 = Progressiva R$300 (Ianka)

    product = products.create_product(
        session, actor, ProductCreate(name="Shampoo", cost_price=Decimal("10"), sale_price=Decimal("50"), for_sale=True),
    )
    stock.record_movement(
        session, actor, product_id=product.id, branch_id=branch.id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("10"),
    )
    orders.add_product_item(session, actor, order1.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))

    # Total consolidado: 150 + 50 (produto) + 300 = 500.
    register = _open_register_other_branch(session, actor)
    payments = [
        PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("300.00"), cash_register_id=register.id),
        PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("200.00"), cash_register_id=register.id),
    ]
    closed = orders.close_orders_consolidated(
        session, actor, order1.id,
        ConsolidatedOrderClose(order_ids=[order1.id, order2.id], payments=payments),
    )

    by_id = {o.id: o for o in closed}
    assert by_id[order1.id].status == OrderStatus.CLOSED
    assert by_id[order2.id].status == OrderStatus.CLOSED

    # order1 (200 total: 150 serviço + 50 produto) fecha primeiro (menor
    # order_number) — consome PIX 200 dos 300 disponíveis.
    order1_paid = sum((p.amount for p in by_id[order1.id].payments), Decimal("0"))
    order2_paid = sum((p.amount for p in by_id[order2.id].payments), Decimal("0"))
    assert order1_paid == Decimal("200.00")
    assert order2_paid == Decimal("300.00")

    # Baixa de estoque aconteceu (produto só na order1).
    level = stock_level_repo.get(session, org_id, product.id, branch.id)
    assert level.quantity_on_hand == Decimal("9")

    # Agendamentos promovidos a PAID.
    assert appointments.get_appointment(session, actor, appt1.id).status == AppointmentStatus.PAID
    assert appointments.get_appointment(session, actor, appt2.id).status == AppointmentStatus.PAID


def test_close_orders_consolidated_e_transacional_estoque_insuficiente_aborta_tudo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _branch, _client, (appt1, order1, _d), (_appt2, order2, _i) = _setup_two_related_orders(session, org_id, actor)

    product = products.create_product(
        session, actor, ProductCreate(name="Óleo raro", cost_price=Decimal("10"), sale_price=Decimal("50"), for_sale=True),
    )
    # Sem estoque nenhum (0 unidades) — a comanda pede 1.
    orders.add_product_item(session, actor, order1.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")))

    register = _open_register_other_branch(session, actor)
    payments = [PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("1000.00"), cash_register_id=register.id)]

    with pytest.raises(ValidationDomainError):
        orders.close_orders_consolidated(
            session, actor, order1.id,
            ConsolidatedOrderClose(order_ids=[order1.id, order2.id], payments=payments),
        )

    # Nenhuma das duas comandas fechou; nenhum pagamento foi criado.
    reloaded1 = orders.get_order(session, actor, order1.id)
    reloaded2 = orders.get_order(session, actor, order2.id)
    assert reloaded1.status == OrderStatus.OPEN
    assert reloaded2.status == OrderStatus.OPEN
    assert reloaded1.payments == []
    assert reloaded2.payments == []
    assert appointments.get_appointment(session, actor, appt1.id).status != AppointmentStatus.PAID


def test_close_orders_consolidated_retry_recusa_segunda_tentativa(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _branch, _client, (_appt1, order1, _d), (_appt2, order2, _i) = _setup_two_related_orders(session, org_id, actor)
    register = _open_register_other_branch(session, actor)
    payments = [PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("450.00"), cash_register_id=register.id)]

    orders.close_orders_consolidated(
        session, actor, order1.id, ConsolidatedOrderClose(order_ids=[order1.id, order2.id], payments=payments),
    )
    with pytest.raises(ConflictError):
        orders.close_orders_consolidated(
            session, actor, order1.id, ConsolidatedOrderClose(order_ids=[order1.id, order2.id], payments=payments),
        )
    # Não duplicou pagamento.
    reloaded1 = orders.get_order(session, actor, order1.id)
    assert len(reloaded1.payments) == 1


def test_close_orders_consolidated_exige_mesma_cliente(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    amanda = _client(session, org_id, name="Amanda")
    bruna = _client(session, org_id, name="Bruna")
    appt1, _ = _appointment_for_client(
        session, org_id, actor, branch, amanda, prof_name="Duda", service_name="Corte",
        price=Decimal("50"), start_hour=9,
    )
    appt2, _ = _appointment_for_client(
        session, org_id, actor, branch, bruna, prof_name="Ianka", service_name="Corte",
        price=Decimal("50"), start_hour=10,
    )
    order1 = orders.create_order(session, actor, appt1.id)
    order2 = orders.create_order(session, actor, appt2.id)
    register = _open_register_other_branch(session, actor)
    payments = [PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("100.00"), cash_register_id=register.id)]

    with pytest.raises(ValidationDomainError):
        orders.close_orders_consolidated(
            session, actor, order1.id, ConsolidatedOrderClose(order_ids=[order1.id, order2.id], payments=payments),
        )


def test_close_orders_consolidated_exige_ao_menos_duas_comandas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _branch, _client, (_appt1, order1, _d), (_appt2, _order2, _i) = _setup_two_related_orders(session, org_id, actor)
    register = _open_register_other_branch(session, actor)
    payments = [PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("150.00"), cash_register_id=register.id)]

    with pytest.raises(ValidationDomainError):
        orders.close_orders_consolidated(
            session, actor, order1.id, ConsolidatedOrderClose(order_ids=[order1.id], payments=payments),
        )
