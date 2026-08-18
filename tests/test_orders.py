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
from nexasalon_api.services import appointments, cash_register, orders

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


def _scheduled_appointment_with_one_service(session, org_id, actor):
    """Agendamento com 1 serviço (Corte R$100), ainda no status
    default `SCHEDULED` — item "não condicione a Comanda a ter passado
    por todos os status": abrir/fechar comanda não deve exigir
    `FINISHED`."""
    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    corte = _service(session, org_id, name="Corte", duration=60, price=Decimal("100.00"))
    _link(session, prof.id, corte.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=corte.id, start_at=_dt(9, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    assert appt.status == AppointmentStatus.SCHEDULED
    return appt, branch, prof, client


def _open_register(session, actor, initial_amount=Decimal("0")):
    """Caixa aberto, pronto pra receber pagamento — a maioria dos testes
    de comanda/pagamento não testa o Caixa em si (isso vive em
    `test_cash_register.py`), só precisa de UM caixa aberto válido pra
    poder fechar a comanda (item "pagamento obrigatoriamente vinculado
    ao caixa"). Cria sua PRÓPRIA unidade (regra desta rodada: 1 caixa
    aberto por unidade) — não precisa ser a mesma unidade do
    agendamento/comanda sendo testado."""
    branch_id = _branch(session, actor.organization_id).id
    return cash_register.open_register(session, actor, branch_id, initial_amount, None)


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
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

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
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    assert closed.status == OrderStatus.CLOSED
    assert closed.closed_at is not None
    assert len(closed.payments) == 1
    assert closed.payments[0].method == PaymentMethod.PIX
    assert closed.payments[0].cash_register_id == register.id

    session.refresh(appt)
    assert appt.status == AppointmentStatus.PAID


def test_fechar_comanda_com_credito_exige_bandeira(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    register = _open_register(session, actor)
    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("100.00"), cash_register_id=register.id)  # sem card_brand


def test_fechar_comanda_com_debito_e_bandeira_funciona_e_aceita_parcelas_so_no_credito(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.DEBIT, amount=total, card_brand=CardBrand.VISA, cash_register_id=register.id)
        ]),
    )
    assert closed.payments[0].card_brand == CardBrand.VISA
    assert closed.payments[0].installments is None

    # installments só é aceito com method=credit.
    with pytest.raises(ValueError):
        PaymentCreate(
            method=PaymentMethod.DEBIT, amount=Decimal("10.00"), card_brand=CardBrand.VISA, installments=3,
            cash_register_id=register.id,
        )

    # com crédito, funciona.
    payment = PaymentCreate(
        method=PaymentMethod.CREDIT, amount=Decimal("10.00"), card_brand=CardBrand.MASTERCARD, installments=3,
        cash_register_id=register.id,
    )
    assert payment.installments == 3


def test_fechar_comanda_com_pagamento_misto_pix_mais_credito(org_session):
    """Domínio suporta lista de pagamentos (`Payment[]`) — mesmo a UI da
    primeira versão só criando um lançamento por fechamento."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))
    part_a = (total / 2).quantize(Decimal("0.01"))
    part_b = total - part_a

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=part_a, cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=part_b, card_brand=CardBrand.ELO, installments=2, cash_register_id=register.id),
        ]),
    )
    assert len(closed.payments) == 2
    assert closed.status == OrderStatus.CLOSED


def test_nao_fecha_comanda_com_valor_pago_menor_que_o_total(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("1.00"), cash_register_id=register.id)]),
        )


def test_nao_fecha_comanda_ja_fechada(org_session):
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

    with pytest.raises(ConflictError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )


def test_abre_comanda_com_agendamento_ainda_scheduled(org_session):
    """Item "se ainda não existe comanda -> Abrir Comanda sempre
    disponível": não exige nenhum status prévio do Appointment."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _scheduled_appointment_with_one_service(session, org_id, actor)

    order = orders.create_order(session, actor, appt.id)
    assert order.status == OrderStatus.OPEN
    assert appt.status == AppointmentStatus.SCHEDULED  # abrir comanda não muda o status operacional


def test_finaliza_comanda_sem_passar_pelos_outros_status(org_session):
    """Exemplo do pedido: Agendado -> Abrir Comanda -> Finalizar
    pagamento -> Pago, sem precisar passar por Confirmado/Aguardando/
    Em Atendimento/Finalizado."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _scheduled_appointment_with_one_service(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )
    assert closed.status == OrderStatus.CLOSED

    session.refresh(appt)
    assert appt.status == AppointmentStatus.PAID


def test_nao_fecha_comanda_de_agendamento_cancelado(org_session):
    """Trava de segurança: um agendamento cancelado não vira `paid`
    mesmo que a comanda (aberta antes do cancelamento) continue aberta
    — `appointments_service.mark_paid` recusa `current == CANCELLED`."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _scheduled_appointment_with_one_service(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    appt.status = AppointmentStatus.CANCELLED
    session.flush()
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )

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


# ---------------------------------------------------------------------
# Pagamento obrigatoriamente vinculado a um Caixa aberto
# ---------------------------------------------------------------------


def test_nao_fecha_comanda_sem_caixa_selecionado(org_session):
    """`cash_register_id` é campo obrigatório do schema — nem chega a
    existir um `PaymentCreate` válido sem ele (a UI não deveria nem
    conseguir montar a requisição)."""
    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("10.00"))


def test_nao_fecha_comanda_com_caixa_fechado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    cash_register.close_register(session, actor, register.id, None, None)
    total = sum((i.price for i in order.items), Decimal("0"))

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )


def test_nao_fecha_comanda_com_caixa_de_outra_organizacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    total = sum((i.price for i in order.items), Decimal("0"))

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_actor = _actor(other_session, other_org_id)
        other_register = _open_register(other_session, other_actor)
        other_register_id = other_register.id
        other_session.commit()

    with pytest.raises(NotFoundError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=other_register_id)]),
        )


def test_pagamento_registra_caixa_e_nome_de_quem_registrou(org_session):
    """Item 'auditoria dos pagamentos': cada Payment guarda o caixa
    (`cash_register_id`) e um snapshot do nome de quem de fato
    registrou o pagamento (`created_by_name`) — preservado mesmo que o
    usuário troque de nome depois."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = sum((i.price for i in order.items), Decimal("0"))

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    payment = closed.payments[0]
    assert payment.cash_register_id == register.id
    assert payment.created_by == actor.user_id
    assert payment.created_by_name is not None and len(payment.created_by_name) > 0


def test_pagamento_entra_imediatamente_no_resumo_do_caixa(org_session):
    """Item 'integração com comandas': fechar a comanda faz o pagamento
    aparecer no resumo do caixa sem nenhuma etapa extra."""
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

    summary = cash_register.get_register_summary(session, actor, register.id)
    pix_total, pix_count = summary.totals_by_method[PaymentMethod.PIX]
    assert pix_total == total
    assert pix_count == 1
    assert summary.total_revenue == total


def test_order_number_e_sequencial_por_organizacao(org_session):
    """Item 'número da comanda' — sequencial POR ORG, começa em 1,
    incrementa a cada nova comanda aberta na mesma organização."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt1, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order1 = orders.create_order(session, actor, appt1.id)

    branch = _branch(session, org_id)
    prof = _professional(session, org_id, branch.id)
    svc = _service(session, org_id, name="Escova", duration=30, price=Decimal("80.00"))
    _link(session, prof.id, svc.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client2 = _client(session, org_id, name="Outro Cliente")
    data2 = AppointmentCreate(
        branch_id=branch.id, client_id=client2.id,
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=svc.id, start_at=_dt(14, 0))],
    )
    appt2 = appointments.create_appointment(session, actor, data2)
    appt2.status = AppointmentStatus.FINISHED
    session.flush()
    order2 = orders.create_order(session, actor, appt2.id)

    assert order2.order_number == order1.order_number + 1


def test_snapshot_de_nome_do_item_nao_muda_se_servico_for_renomeado(org_session):
    """Item 16 'snapshot histórico' — renomear o serviço DEPOIS de a
    comanda existir não pode mudar como a venda antiga aparece."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, prof, client = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    original_names = {item.service_name for item in order.items}
    assert original_names == {"Corte", "Coloração"}

    # renomeia o serviço no catálogo DEPOIS de a comanda já existir
    from nexasalon_api.repositories import service_repo

    service = service_repo.get(session, org_id, order.items[0].service_id)
    service.name = "Nome Novo Do Catálogo"
    session.flush()

    reloaded = orders.get_order(session, actor, order.id)
    assert {item.service_name for item in reloaded.items} == original_names
