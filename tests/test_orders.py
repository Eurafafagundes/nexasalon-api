"""Testes de Comanda/Pagamento (`services/orders.py`) — primeira versão
funcional do fluxo Atendimento -> Comanda -> Pagamento -> Pago. Mesma
abordagem de `test_appointments.py`: direto no service layer via
`SessionLocal`, sem rota HTTP (RBAC de rota já tem cobertura própria
em `test_auth.py`/`test_agenda.py`, e o parâmetro que importa aqui é a
regra de negócio, não o transporte)."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from nexasalon_api.models.client import Client
from nexasalon_api.models.identity import User
from nexasalon_api.models.enums import AppointmentStatus, CardBrand, OrderStatus, PaymentMethod
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import OrderClose, OrderItemPriceUpdate, PaymentCreate
from nexasalon_api.services import appointments, orders

_ALL_AGENDA_PERMS = frozenset(
    {"agenda.view_own", "agenda.view_all", "agenda.create", "agenda.edit", "agenda.cancel"}
)
_TZ = timezone(timedelta(hours=-3))
_THURSDAY = 4  # 0=domingo..6=sábado; 2026-08-13 é quinta.


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org orders", slug=f"org-orders-{org_id.hex[:8]}"))
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


def _finished_appointment_with_two_services(session, org_id, actor):
    """Um agendamento com 2 serviços (Corte R$100, Coloração R$280),
    já FINISHED — pronto pra abrir comanda."""
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    corte = _service(session, org_id, name="Corte", duration=60, price=Decimal("100.00"))
    coloracao = _service(session, org_id, name="Coloração", duration=90, price=Decimal("280.00"))
    _link(session, prof.id, corte.id)
    _link(session, prof.id, coloracao.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[
            AppointmentItemCreate(professional_id=prof.id, service_id=corte.id, start_at=_dt(9, 0)),
            AppointmentItemCreate(professional_id=prof.id, service_id=coloracao.id, start_at=_dt(11, 0)),
        ],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt, branch, prof, client


# ---------------------------------------------------------------------
# Criação da comanda (Atendimento -> Comanda)
# ---------------------------------------------------------------------


def test_criar_comanda_copia_itens_do_agendamento_com_total_correto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)

    order = orders.create_order(session, actor, appt.id)

    assert order.status == OrderStatus.OPEN
    assert len(order.items) == 2
    prices = sorted(i.price for i in order.items)
    assert prices == [Decimal("100.00"), Decimal("280.00")]


def test_total_da_comanda_e_a_soma_dos_itens(org_session):
    from nexasalon_api.schemas.order import OrderRead

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)

    read = OrderRead.from_order(order)
    assert read.subtotal == Decimal("380.00")
    assert read.total == Decimal("380.00")


def test_nao_deixa_abrir_duas_comandas_pro_mesmo_agendamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    orders.create_order(session, actor, appt.id)

    with pytest.raises(ConflictError):
        orders.create_order(session, actor, appt.id)


# ---------------------------------------------------------------------
# Editar preço de uma linha (nunca altera o catálogo)
# ---------------------------------------------------------------------


def test_editar_preco_da_linha_nao_altera_o_catalogo_nem_o_item_original(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, prof, client = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    corte_item = next(i for i in order.items if i.price == Decimal("100.00"))
    original_service_id = corte_item.service_id
    original_appointment_item = next(ai for ai in appt.items if ai.service_id == original_service_id)
    original_appointment_item_price = original_appointment_item.price

    updated = orders.update_item_price(session, actor, order.id, corte_item.id, OrderItemPriceUpdate(price=Decimal("80.00")))

    updated_item = next(i for i in updated.items if i.id == corte_item.id)
    assert updated_item.price == Decimal("80.00")

    # Catálogo (Service.default_price) intocado.
    from nexasalon_api.repositories import service_repo

    service = service_repo.get(session, org_id, original_service_id)
    assert service.default_price == Decimal("100.00")

    # Snapshot original do AppointmentItem também intocado.
    session.refresh(original_appointment_item)
    assert original_appointment_item.price == original_appointment_item_price == Decimal("100.00")


def test_editar_preco_registra_auditoria_com_valor_anterior_e_novo(org_session):
    from nexasalon_api.repositories import audit_log_repo

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item = order.items[0]
    old_price = item.price

    orders.update_item_price(session, actor, order.id, item.id, OrderItemPriceUpdate(price=Decimal("50.00")))

    logs = audit_log_repo.list_for_entity(session, org_id, "order_item", item.id)
    assert len(logs) == 1
    assert logs[0].old_values["price"] == str(old_price)
    assert logs[0].new_values["price"] == "50.00"
    assert logs[0].user_id == actor.user_id


def test_nao_edita_preco_de_comanda_ja_fechada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(session, actor, order.id, OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total)]))

    with pytest.raises(ValidationDomainError):
        orders.update_item_price(session, actor, order.id, order.items[0].id, OrderItemPriceUpdate(price=Decimal("1.00")))


# ---------------------------------------------------------------------
# Fechar comanda / pagamento -> Appointment vira `paid` automaticamente
# ---------------------------------------------------------------------


def test_fechar_comanda_com_pix_marca_paga_e_promove_agendamento_pra_paid(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    total = sum((i.price for i in order.items), Decimal("0"))

    closed = orders.close_order(
        session, actor, order.id, OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total)])
    )

    assert closed.status == OrderStatus.CLOSED
    assert closed.closed_at is not None
    assert len(closed.payments) == 1
    assert closed.payments[0].method == PaymentMethod.PIX

    session.refresh(appt)
    assert appt.status == AppointmentStatus.PAID


def test_fechar_comanda_com_credito_exige_bandeira(org_session):
    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("100.00"))  # sem card_brand


def test_fechar_comanda_com_debito_e_bandeira_funciona_e_aceita_parcelas_so_no_credito(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    total = sum((i.price for i in order.items), Decimal("0"))

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.DEBIT, amount=total, card_brand=CardBrand.VISA)]),
    )
    assert closed.payments[0].card_brand == CardBrand.VISA
    assert closed.payments[0].installments is None

    # installments só é aceito com method=credit.
    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.DEBIT, amount=Decimal("10.00"), card_brand=CardBrand.VISA, installments=3)

    # com crédito, funciona.
    payment = PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("10.00"), card_brand=CardBrand.MASTERCARD, installments=3)
    assert payment.installments == 3


def test_fechar_comanda_com_pagamento_misto_pix_mais_credito(org_session):
    """Domínio suporta lista de pagamentos (`Payment[]`) — mesmo a UI da
    primeira versão só criando um lançamento por fechamento."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    total = sum((i.price for i in order.items), Decimal("0"))
    part_a = (total / 2).quantize(Decimal("0.01"))
    part_b = total - part_a

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=part_a),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=part_b, card_brand=CardBrand.ELO, installments=2),
        ]),
    )
    assert len(closed.payments) == 2
    assert closed.status == OrderStatus.CLOSED


def test_nao_fecha_comanda_com_valor_pago_menor_que_o_total(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id, OrderClose(payments=[PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("1.00"))])
        )


def test_nao_fecha_comanda_ja_fechada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(session, actor, order.id, OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total)]))

    with pytest.raises(ConflictError):
        orders.close_order(session, actor, order.id, OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total)]))


def test_nao_promove_pra_paid_se_agendamento_nao_estiver_finished(org_session):
    """`close_order` reaproveita `next_status` — um agendamento que
    ainda não passou por `finished` não pode virar `paid` só porque
    alguém tentou fechar a comanda (a comanda nem deveria existir nesse
    caso, mas a trava vale mesmo que exista)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    appt.status = AppointmentStatus.IN_PROGRESS  # comanda foi aberta, mas o atendimento "voltou"
    session.flush()
    total = sum((i.price for i in order.items), Decimal("0"))

    with pytest.raises(ValidationDomainError):
        orders.close_order(session, actor, order.id, OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total)]))

    # E a comanda continua aberta (não fechou parcialmente).
    session.refresh(order)
    assert order.status == OrderStatus.OPEN


# ---------------------------------------------------------------------
# Isolamento multiempresa (RLS)
# ---------------------------------------------------------------------


def test_isolamento_multiempresa_comanda_de_outra_org_nao_aparece(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_actor = _actor(other_session, other_org_id)

        with pytest.raises(NotFoundError):
            orders.get_order(other_session, other_actor, order.id)
        other_session.rollback()
