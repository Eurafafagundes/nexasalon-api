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

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CardBrand,
    CashMovementType,
    OrderStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import OrderClose, PaymentCreate
from nexasalon_api.services import appointments, cash_register, extract, orders

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
