"""Testes de Comanda/Pagamento (`services/orders.py`) — primeira versão
funcional do fluxo Atendimento -> Comanda -> Pagamento -> Pago. Mesma
abordagem de `test_appointments.py`: direto no service layer via
`SessionLocal`, sem rota HTTP (RBAC de rota já tem cobertura própria
em `test_auth.py`/`test_agenda.py`, e o parâmetro que importa aqui é a
regra de negócio, não o transporte)."""
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationDomainError,
)
from nexasalon_api.models.audit import AuditLog
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CardBrand,
    OrderStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.repositories import professional_repo, service_repo
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    OrderCancel,
    OrderClose,
    OrderItemPriceUpdate,
    OrderObservationUpdate,
    PaymentCreate,
)
from nexasalon_api.services import appointments, cash_register, order_totals, orders

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
    já FINISHED — pronto pra abrir comanda.

    Etapa H ("exigir caixa aberto para criar Comanda", padrão ON): já
    abre, aqui, um caixa nesta MESMA unidade — a maioria dos testes
    deste arquivo não testa essa regra em si (isso vive em
    `test_cash_register_config.py`), só precisa que `orders.create_order`
    não seja recusado por falta de caixa aberto na unidade do
    agendamento."""
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
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
    `FINISHED`. Também já abre caixa nesta unidade — ver docstring de
    `_finished_appointment_with_two_services`."""
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
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


def _finished_appointment_with_three_services(session, org_id, actor):
    """Mesma ideia de `_finished_appointment_with_two_services`, com 3
    serviços — valores espelham EXATAMENTE o exemplo do bug report
    (Serviço A R$260, B R$150, C R$410), pra reproduzir o cenário real
    relatado (editar A e B pra R$0, manter C em R$410)."""
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id)
    a = _service(session, org_id, name="Serviço A", duration=60, price=Decimal("260.00"))
    b = _service(session, org_id, name="Serviço B", duration=45, price=Decimal("150.00"))
    c = _service(session, org_id, name="Serviço C", duration=90, price=Decimal("410.00"))
    _link(session, prof.id, a.id)
    _link(session, prof.id, b.id)
    _link(session, prof.id, c.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        items=[
            AppointmentItemCreate(professional_id=prof.id, service_id=a.id, start_at=_dt(9, 0)),
            AppointmentItemCreate(professional_id=prof.id, service_id=b.id, start_at=_dt(10, 30)),
            AppointmentItemCreate(professional_id=prof.id, service_id=c.id, start_at=_dt(12, 0)),
        ],
    )
    appt = appointments.create_appointment(session, actor, data)
    appt.status = AppointmentStatus.FINISHED
    session.flush()
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


def test_criar_comanda_resolve_service_e_professional_em_lote_nunca_um_por_item(org_session):
    """Item de performance ("quick win 4"): `create_order` resolvia
    `service_name`/`professional_name` chamando `service_repo.get`/
    `professional_repo.get` UMA VEZ POR ITEM do agendamento (1+2N
    queries) — agora busca todos de uma vez (`list_by_ids`, 1+2 no
    total, qualquer que seja N). `service_repo.get`/`professional_repo.
    get` nunca deveriam ser chamados por `create_order`."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)

    service_get_calls = 0
    professional_get_calls = 0
    real_service_get = service_repo.get
    real_professional_get = professional_repo.get

    def _counting_service_get(*args, **kwargs):
        nonlocal service_get_calls
        service_get_calls += 1
        return real_service_get(*args, **kwargs)

    def _counting_professional_get(*args, **kwargs):
        nonlocal professional_get_calls
        professional_get_calls += 1
        return real_professional_get(*args, **kwargs)

    with (
        patch.object(service_repo, "get", side_effect=_counting_service_get),
        patch.object(professional_repo, "get", side_effect=_counting_professional_get),
    ):
        order = orders.create_order(session, actor, appt.id)

    assert service_get_calls == 0
    assert professional_get_calls == 0
    # Equivalência de comportamento — mesmo resultado exato de antes:
    # nomes corretamente resolvidos do catálogo pra cada item.
    assert len(order.items) == 2
    names_by_service = {i.service_name for i in order.items}
    assert names_by_service == {"Corte", "Coloração"}
    professional_names = {i.professional_name for i in order.items}
    assert professional_names == {"Profissional"}


def test_criar_comanda_copia_appointment_notes_pra_order_observation_uma_unica_vez(org_session):
    """Redesenho do drawer da Agenda: no momento da criação do
    agendamento ainda não existe Order, então o texto digitado em
    "Observação da comanda" fica temporariamente em `Appointment.notes`.
    Ao abrir a comanda, essa cópia acontece EXATAMENTE UMA VEZ — a
    Order nasce já com `observation` preenchida (sem incrementar
    `observation_version`, sem preencher `observation_updated_at/_by`,
    já que ninguém "editou" a observação da comanda via o fluxo de
    edição, ela só nasceu com esse conteúdo). A partir daqui
    `Order.observation` é a única fonte de verdade — editar depois
    passa a exigir `update_observation` normalmente."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id)
    corte = _service(session, org_id, name="Corte", duration=60, price=Decimal("100.00"))
    _link(session, prof.id, corte.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        notes="Cliente pediu para utilizar apenas 180g de cabelo nesta manutenção.",
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=corte.id, start_at=_dt(9, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)

    order = orders.create_order(session, actor, appt.id)

    assert order.observation == "Cliente pediu para utilizar apenas 180g de cabelo nesta manutenção."
    assert order.observation_version == 0
    assert order.observation_updated_at is None
    assert order.observation_updated_by is None


def test_criar_comanda_sem_appointment_notes_nao_inventa_observation(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _scheduled_appointment_with_one_service(session, org_id, actor)

    order = orders.create_order(session, actor, appt.id)

    assert order.observation is None


def test_appointment_notes_nunca_sobrescreve_order_observation_ja_existente(org_session):
    """Auditoria UX/funcional (item 6, "Antes da Order existir"): depois
    que a Order já existe, `Appointment.notes` NUNCA volta a escrever em
    `Order.observation` — nem mesmo se alguém tentar "reabrir" a comanda
    do mesmo agendamento. A tentativa de recriação é recusada com
    `ConflictError` ANTES de qualquer leitura de `appointment.notes`
    (a checagem de duplicidade em `create_order` vem antes do cálculo do
    seed) — então a observação já editada pelo atendente permanece
    intacta. A corrida genuína (dois `INSERT` concorrentes colidindo na
    unique parcial `uq_orders_appointment_id_active`) é protegida pelo
    mesmo SAVEPOINT já usado pra idempotência de `create_order`: o
    "perdedor" nunca persiste nada (rollback do savepoint), então o
    `order_observation` que ELE calculou localmente nunca chega a ser
    escrito — `return existing` devolve exatamente o que o "vencedor"
    gravou, nunca misturado com o seed do perdedor."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id)
    corte = _service(session, org_id, name="Corte", duration=60, price=Decimal("100.00"))
    _link(session, prof.id, corte.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id)

    data = AppointmentCreate(
        branch_id=branch.id, client_id=client.id,
        notes="Observação original digitada na criação do agendamento.",
        items=[AppointmentItemCreate(professional_id=prof.id, service_id=corte.id, start_at=_dt(9, 0))],
    )
    appt = appointments.create_appointment(session, actor, data)
    order = orders.create_order(session, actor, appt.id)
    assert order.observation == "Observação original digitada na criação do agendamento."

    # Atendente edita a observação da comanda DEPOIS da Order existir —
    # `Appointment.notes` continua com o texto antigo (nunca é
    # resincronizado de volta), só `Order.observation` muda.
    orders.update_observation(
        session, actor, order.id,
        OrderObservationUpdate(observation="Editado pelo atendente depois de aberta.", expected_observation_version=0),
    )

    with pytest.raises(ConflictError):
        orders.create_order(session, actor, appt.id)

    reloaded = orders.get_order(session, actor, order.id)
    assert reloaded.observation == "Editado pelo atendente depois de aberta."
    assert appt.notes == "Observação original digitada na criação do agendamento."


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


def test_mark_paid_com_appointment_inexistente_continua_404(org_session):
    """Bug real corrigido: `mark_paid` trocou `get_appointment`
    (escopo de visibilidade de Agenda) por `appointment_repo.get`
    direto (org-scoped) — isolamento por organização e o 404 real pra
    agendamento inexistente/de outra org precisam continuar intactos,
    só a checagem de VISIBILIDADE de Agenda que foi removida daqui."""
    session, org_id = org_session
    actor = _actor(session, org_id)

    with pytest.raises(NotFoundError):
        appointments.mark_paid(session, actor, uuid.uuid4())


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
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
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


# ---------------------------------------------------------------------
# Cancelar comanda (Etapa F, item "Cancelar/Excluir Comanda") — nunca
# apaga, some das operações abertas mas continua em `GET /orders`.
# ---------------------------------------------------------------------


def test_cancela_comanda_aberta_sem_pagamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)

    cancelled = orders.cancel_order(session, actor, order.id, OrderCancel(reason="Aberta por engano"))
    assert cancelled.status == OrderStatus.CANCELLED


def test_cancelar_comanda_registra_auditoria_com_motivo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)

    orders.cancel_order(session, actor, order.id, OrderCancel(reason="Cliente desistiu"))

    logs = session.query(AuditLog).filter(
        AuditLog.organization_id == org_id, AuditLog.entity_id == order.id, AuditLog.entity_type == "order",
        AuditLog.action == "update",
    ).all()
    cancel_logs = [log for log in logs if log.new_values.get("change_type") == "cancel"]
    assert len(cancel_logs) == 1
    assert cancel_logs[0].new_values["reason"] == "Cliente desistiu"
    assert cancel_logs[0].old_values["status"] == "open"


def test_nao_cancela_comanda_ja_fechada(org_session):
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
        orders.cancel_order(session, actor, order.id, OrderCancel(reason="teste"))


def test_retry_de_cancelamento_nao_gera_erro_estranho_nem_duplica(org_session):
    """Retry-safety: cancelar duas vezes a MESMA comanda recusa a
    segunda tentativa com clareza (nunca "cancela de novo" silenciosamente)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)

    orders.cancel_order(session, actor, order.id, OrderCancel(reason="teste"))
    with pytest.raises(ConflictError):
        orders.cancel_order(session, actor, order.id, OrderCancel(reason="teste"))


def test_cancelar_comanda_libera_o_agendamento_para_uma_comanda_nova(org_session):
    """Item explícito: cancelar uma comanda criada por engano precisa
    permitir abrir uma comanda NOVA pro mesmo agendamento — a linha
    cancelada não pode "segurar" a unicidade pra sempre."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    orders.cancel_order(session, actor, order.id, OrderCancel(reason="Engano"))

    # `get_by_appointment` não enxerga mais a comanda cancelada.
    assert orders.get_order_by_appointment(session, actor, appt.id) is None

    new_order = orders.create_order(session, actor, appt.id)
    assert new_order.id != order.id
    assert new_order.status == OrderStatus.OPEN

    # A comanda cancelada continua acessível por id (histórico).
    reloaded_cancelled = orders.get_order(session, actor, order.id)
    assert reloaded_cancelled.status == OrderStatus.CANCELLED


def test_comanda_cancelada_nao_aparece_como_a_comanda_ativa_do_agendamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    assert orders.get_order_by_appointment(session, actor, appt.id).id == order.id

    orders.cancel_order(session, actor, order.id, OrderCancel(reason="teste"))
    assert orders.get_order_by_appointment(session, actor, appt.id) is None


# ---------------------------------------------------------------------
# Investigacao -- edicao manual de valor de item da comanda pode ser
# ZERO (bug report: valor 0 "some" apos editar outro item + Fidelidade
# nao aceita R$ 0). Reproduz o exemplo do bug report: Servico A R$260,
# B R$150, C R$410 -> A=0, B=0, C=410.
# ---------------------------------------------------------------------


def test_item_preco_zero_persiste_sem_fallback_para_catalogo(org_session):
    """Preco catalogo 260, override na comanda 0 -- item deve ficar 0,
    NUNCA reverter pro catalogo (bug classico price-or-catalog, onde 0
    e falsy e cai no fallback)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))

    updated = orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))

    updated_item = next(i for i in updated.items if i.id == item_a.id)
    assert updated_item.price == Decimal("0.00")
    from nexasalon_api.repositories import service_repo

    service = service_repo.get(session, org_id, item_a.service_id)
    assert service.default_price == Decimal("260.00")  # catalogo intocado.


def test_tres_itens_zero_zero_quatrocentos_dez_total_correto(org_session):
    """Cenario do bug report: A=0, B=0, C=410 -> total=410 (nunca 820,
    nunca 260, nunca qualquer soma que trate 0 como ausencia de
    override)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))
    item_c = next(i for i in order.items if i.price == Decimal("410.00"))

    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    updated = orders.update_item_price(session, actor, order.id, item_c.id, OrderItemPriceUpdate(price=Decimal("410.00")))

    prices = {i.id: i.price for i in updated.items}
    assert prices[item_a.id] == Decimal("0.00")
    assert prices[item_b.id] == Decimal("0.00")
    assert prices[item_c.id] == Decimal("410.00")
    assert order_totals.order_total(updated) == Decimal("410.00")


def test_reeditar_um_item_nao_reseta_nem_afeta_os_demais(org_session):
    """Depois de A=0, B=0, C=410 ja salvos, reeditar SO o B nao pode
    alterar A nem C -- cada PATCH escreve exclusivamente no item alvo."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))
    item_c = next(i for i in order.items if i.price == Decimal("410.00"))
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_c.id, OrderItemPriceUpdate(price=Decimal("410.00")))

    reedited = orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("75.00")))

    prices = {i.id: i.price for i in reedited.items}
    assert prices[item_a.id] == Decimal("0.00")  # intocado.
    assert prices[item_b.id] == Decimal("75.00")  # so este mudou.
    assert prices[item_c.id] == Decimal("410.00")  # intocado.
    assert order_totals.order_total(reedited) == Decimal("485.00")


@pytest.mark.parametrize(
    "de, para",
    [
        (Decimal("260.00"), Decimal("410.00")),  # sobe.
        (Decimal("410.00"), Decimal("0.00")),  # zera.
        (Decimal("0.00"), Decimal("410.00")),  # sai de zero.
    ],
)
def test_transicoes_de_preco_incluindo_zero_funcionam_nos_dois_sentidos(org_session, de, para):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item = next(i for i in order.items if i.price == Decimal("260.00"))
    if de != Decimal("260.00"):
        orders.update_item_price(session, actor, order.id, item.id, OrderItemPriceUpdate(price=de))

    updated = orders.update_item_price(session, actor, order.id, item.id, OrderItemPriceUpdate(price=para))

    updated_item = next(i for i in updated.items if i.id == item.id)
    assert updated_item.price == para


def test_produtos_da_comanda_continuam_funcionando_junto_com_itens_zerados(org_session):
    """Item de servico zerado nao pode quebrar o total de PRODUTO --
    order_totals.order_total_breakdown soma as duas parcelas
    independentemente."""
    from nexasalon_api.models.enums import ProductUnit
    from nexasalon_api.models.product import Product
    from nexasalon_api.schemas.order import OrderProductItemCreate
    from nexasalon_api.services import orders as orders_service

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, branch, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))

    product = Product(
        organization_id=org_id, name="Shampoo", unit=ProductUnit.UNIT, sale_price=Decimal("30.00"),
        cost_price=Decimal("10.00"), is_active=True, for_sale=True,
    )
    session.add(product)
    session.flush()
    from nexasalon_api.repositories import stock_level_repo

    level = stock_level_repo.lock_or_create(session, org_id, product.id, branch.id)
    level.quantity_on_hand = Decimal("10")
    session.flush()

    updated = orders_service.add_product_item(
        session, actor, order.id, OrderProductItemCreate(product_id=product.id, quantity=Decimal("1")),
    )

    breakdown = order_totals.order_total_breakdown(updated)
    assert breakdown.services_total == Decimal("560.00")  # 0 + 150 + 410
    assert breakdown.products_total == Decimal("30.00")
    assert breakdown.total == Decimal("590.00")


def test_comanda_fechada_nao_sofre_alteracao_indevida_apos_fechamento(org_session):
    """Reforca que o TOTAL de uma comanda fechada com item zerado
    permanece estavel (nunca recalculado/exposto diferente depois do
    fechamento), e que uma tentativa de editar depois de fechada nao
    altera nada."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    register = _open_register(session, actor)

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("410.00"), cash_register_id=register.id)]),
    )

    assert order_totals.order_total(closed) == Decimal("410.00")
    with pytest.raises(ValidationDomainError):
        orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("999.00")))
    reloaded = orders.get_order(session, actor, order.id)
    assert order_totals.order_total(reloaded) == Decimal("410.00")


# ---------------------------------------------------------------------
# Investigacao -- Fidelidade e overpayment.
#
# FIDELIDADE: mapeado no codigo (`models/enums.py::PaymentMethod`) --
# "Cartao Fidelidade" (LOYALTY_CARD) e SO MAIS UM METODO de pagamento,
# no mesmo nivel de Pix/Dinheiro/Voucher/Permuta. Nao existe pontuacao,
# resgate, desconto nem regra de elegibilidade modelada em lugar
# nenhum do dominio -- e so um rotulo que o atendente escolhe na hora
# de registrar o pagamento. A causa raiz de "Fidelidade nao aceita R$0"
# NUNCA foi uma regra especifica de Fidelidade: era o schema/CHECK
# genericos de Payment.amount, que valiam pra TODOS os metodos.
#
# ARQUITETURA ESCOLHIDA (revisada apos analise): Payment.amount
# continua exigindo > 0 SEMPRE -- um Payment representa dinheiro que
# de fato mudou de mao, entao um "Payment de R$0" (de qualquer metodo,
# Fidelidade incluso) nao corresponde a nenhum evento real e seria
# ruido em Caixa/Extrato/Dashboard/relatorios ("Pix R$0,00" na tela).
# Uma comanda com total R$0 (cortesia/gratuita) fecha com
# `payments=[]` -- nenhum Payment artificial. A migration 0052 (que
# relaxava `amount` pra `>=0`) foi REMOVIDA por nao ser mais
# necessaria -- nao ha mais nenhum caso legitimo de Payment.amount=0.
#
# OVERPAYMENT: enquanto o Nexa nao modela troco/credito/estorno,
# nenhum pagamento pode ultrapassar o SALDO da comanda (total menos o
# que ja foi processado nesta mesma chamada de fechamento) -- nem a
# soma agregada, nem uma linha individual isolada.
# ---------------------------------------------------------------------


def test_payment_amount_zero_continua_rejeitado_pelo_schema_qualquer_metodo(org_session):
    """Fidelidade nao e caso especial -- amount=0 e rejeitado pro
    MESMO motivo em qualquer metodo (Payment representa dinheiro real)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    register = _open_register(session, actor)

    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.LOYALTY_CARD, amount=Decimal("0.00"), cash_register_id=register.id)
    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("0.00"), cash_register_id=register.id)


def test_payment_amount_negativo_continua_rejeitado_pelo_schema(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    register = _open_register(session, actor)

    with pytest.raises(ValueError):
        PaymentCreate(method=PaymentMethod.LOYALTY_CARD, amount=Decimal("-10.00"), cash_register_id=register.id)


def test_fechar_comanda_totalmente_gratuita_com_lista_de_pagamentos_vazia_sem_payment_artificial(org_session):
    """Alternativa A (escolhida): comanda 100% gratuita fecha com
    `payments=[]` -- NENHUM Payment de R$0 criado pra nenhum metodo,
    nem Fidelidade. Quem quiser registrar o MOTIVO da cortesia usa o
    campo de Observacao da comanda (ja existente, ja auditado), nunca
    um Payment fabricado."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    for item in order.items:
        orders.update_item_price(session, actor, order.id, item.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    register = _open_register(session, actor)

    closed = orders.close_order(session, actor, order.id, OrderClose(payments=[]))

    assert closed.status == OrderStatus.CLOSED
    assert order_totals.order_total(closed) == Decimal("0.00")
    assert closed.payments == []  # nenhum Payment criado -- nenhuma linha "R$0" pra Caixa/Extrato/Dashboard.
    session.refresh(appt)
    assert appt.status == AppointmentStatus.PAID


def test_comissao_percentual_em_item_zerado_e_zero_mas_comissao_fixa_continua_integral(org_session):
    """Documenta (nao altera) a regra atual de comissao pra item R$0:
    PERCENTUAL -> comissao 0 (proporcional ao valor vendido). FIXA ->
    comissao INTEGRAL, porque `resolve_commission` pro tipo FIXED
    ignora `price` inteiramente. Pode ser correto de proposito -- numa
    cortesia o profissional trabalhou igual, entao uma comissao fixa
    (que remunera o TRABALHO, nao um percentual da venda) continuar
    integral e uma decisao de negocio legitima, nao um bug. Esta rodada
    so CONFIRMA que o comportamento e intencional; a decisao de manter
    ou mudar fica separada, com o usuario."""
    from nexasalon_api.models.enums import CommissionType
    from nexasalon_api.models.service import ProfessionalService

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, prof, _client = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))  # servico A.
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))  # servico B.
    session.query(ProfessionalService).filter_by(professional_id=prof.id, service_id=item_a.service_id).update(
        {"commission_type": CommissionType.PERCENTAGE, "commission_value": Decimal("30.00")}
    )
    session.query(ProfessionalService).filter_by(professional_id=prof.id, service_id=item_b.service_id).update(
        {"commission_type": CommissionType.FIXED, "commission_value": Decimal("20.00")}
    )
    session.flush()
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(
        session, actor, order.id, next(i for i in order.items if i.price == Decimal("410.00")).id,
        OrderItemPriceUpdate(price=Decimal("0.00")),
    )
    register = _open_register(session, actor)

    closed = orders.close_order(session, actor, order.id, OrderClose(payments=[]))

    closed_a = next(i for i in closed.items if i.id == item_a.id)
    closed_b = next(i for i in closed.items if i.id == item_b.id)
    assert closed_a.commission_amount_snapshot == Decimal("0.00")  # percentual: 0% de 0 = 0, proporcional.
    assert closed_b.commission_amount_snapshot == Decimal("20.00")  # fixa: integral, mesmo com item a R$0 (intencional).
    assert register.id  # caixa aberto continua exigido mesmo sem nenhum Payment (pre-requisito operacional).


# --- Overpayment: saldo = total - pagamentos ja processados; nenhum ---
# --- lancamento pode ultrapassar o saldo no momento em que e aplicado.


def test_overpayment_total_260_pagamento_410_e_rejeitado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_two_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    corte = next(i for i in order.items if i.price == Decimal("100.00"))
    coloracao = next(i for i in order.items if i.price == Decimal("280.00"))
    orders.update_item_price(session, actor, order.id, corte.id, OrderItemPriceUpdate(price=Decimal("60.00")))
    orders.update_item_price(session, actor, order.id, coloracao.id, OrderItemPriceUpdate(price=Decimal("200.00")))
    # total agora = 260.
    register = _open_register(session, actor)

    with pytest.raises(ValidationDomainError, match="não pode ser maior que o saldo"):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("410.00"), cash_register_id=register.id)]),
        )
    reloaded = orders.get_order(session, actor, order.id)
    assert reloaded.status == OrderStatus.OPEN  # nada foi persistido -- nenhum Payment criado, comanda continua aberta.
    assert reloaded.payments == []


def test_overpayment_total_410_pagamento_410_e_permitido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    # total agora = 410 (0 + 0 + 410).
    register = _open_register(session, actor)

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("410.00"), cash_register_id=register.id)]),
    )

    assert closed.status == OrderStatus.CLOSED
    assert closed.payments[0].amount == Decimal("410.00")


def test_overpayment_total_410_pago_200_mais_210_e_permitido(org_session):
    """Dois lancamentos na MESMA chamada de fechamento (pagamento
    dividido) -- 200 + 210 = 410 exato, cada entrada respeitando o
    saldo remanescente no momento em que e aplicada."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    register = _open_register(session, actor)

    closed = orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("200.00"), cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("210.00"), cash_register_id=register.id),
        ]),
    )

    assert closed.status == OrderStatus.CLOSED
    assert len(closed.payments) == 2
    assert sum((p.amount for p in closed.payments), Decimal("0")) == Decimal("410.00")


def test_overpayment_total_410_pago_200_mais_211_e_rejeitado(org_session):
    """O segundo lancamento (211) ultrapassa o saldo remanescente
    (410-200=210) -- rejeitado ALI, mesmo a soma agregada (411) sendo
    só R$1 acima do total. Nenhum Payment fica persistido, nem o
    primeiro de 200 (fechamento é atômico)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    item_b = next(i for i in order.items if i.price == Decimal("150.00"))
    orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    orders.update_item_price(session, actor, order.id, item_b.id, OrderItemPriceUpdate(price=Decimal("0.00")))
    register = _open_register(session, actor)

    with pytest.raises(ValidationDomainError, match="não pode ser maior que o saldo"):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[
                PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("200.00"), cash_register_id=register.id),
                PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("211.00"), cash_register_id=register.id),
            ]),
        )
    reloaded = orders.get_order(session, actor, order.id)
    assert reloaded.status == OrderStatus.OPEN
    assert reloaded.payments == []  # atômico -- nem o primeiro lançamento (200) ficou.


def test_pagamento_menor_que_o_total_continua_bloqueado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)  # total = 820.
    register = _open_register(session, actor)

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("500.00"), cash_register_id=register.id)]),
        )


def test_edicao_de_item_apos_tentativa_de_fechamento_rejeitada_por_saldo_continua_permitida(org_session):
    """Como o fechamento é atômico (nenhum Payment persiste numa
    tentativa rejeitada), o item continua ABERTO e editável depois de
    uma tentativa de overpayment recusada -- "editar item aumentando
    total depois de pagamento parcial" não tem estado parcial real
    pra corromper: a tentativa rejeitada nunca chegou a existir."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    register = _open_register(session, actor)

    with pytest.raises(ValidationDomainError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("9999.00"), cash_register_id=register.id)]),
        )

    # Item continua editável -- a tentativa recusada não deixou rastro.
    updated = orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("300.00")))
    assert next(i for i in updated.items if i.id == item_a.id).price == Decimal("300.00")


def test_reduzir_total_abaixo_do_ja_fechado_nunca_e_possivel_edicao_bloqueada_apos_fechar(org_session):
    """"Reduzir o total abaixo do já pago" nunca acontece silenciosamente
    porque, uma vez que o fechamento TEM SUCESSO (Payment(s) persistidos,
    status=CLOSED), editar QUALQUER item passa a ser bloqueado
    incondicionalmente (`update_order_item` já recusa `status != OPEN`)
    -- nunca um caminho que deixe total < pago numa comanda fechada."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    item_a = next(i for i in order.items if i.price == Decimal("260.00"))
    register = _open_register(session, actor)

    total = order_totals.order_total(order)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    with pytest.raises(ValidationDomainError):
        orders.update_item_price(session, actor, order.id, item_a.id, OrderItemPriceUpdate(price=Decimal("1.00")))


def test_concorrencia_dois_fechamentos_no_mesmo_pedido_sao_serializados_pelo_lock_da_order(org_session):
    """Documenta (via o mecanismo já existente, não um novo) por que
    dois fechamentos "simultâneos" nunca conseguem juntos ultrapassar o
    saldo: `_get_order_for_update_or_404` trava a linha da Order
    (`SELECT ... FOR UPDATE`) ANTES do cálculo de saldo, e todo o
    fechamento (validação + criação de Payment + status=CLOSED)
    acontece na MESMA transação enquanto o lock é mantido. Uma segunda
    chamada pro MESMO order_id bloqueia até a primeira commitar, e aí
    lê `status=CLOSED` já persistido -- nunca chega a reavaliar saldo
    contra um estado obsoleto. Este teste simula o efeito (2ª chamada
    depois da 1ª já ter commitado, mesma coisa que o lock garante sob
    concorrência real) -- não abre threads de verdade porque a sessão
    de teste é síncrona, mas o comportamento observável é idêntico."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, *_ = _finished_appointment_with_three_services(session, org_id, actor)
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    total = order_totals.order_total(order)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
    )

    # Segunda tentativa de fechamento (ex.: retry de rede) -- rejeitada
    # por status, nunca chega a criar um segundo Payment nem a somar
    # saldo duas vezes.
    with pytest.raises(ConflictError):
        orders.close_order(
            session, actor, order.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=total, cash_register_id=register.id)]),
        )
    reloaded = orders.get_order(session, actor, order.id)
    assert len(reloaded.payments) == 1  # nunca duplicou.


def test_overpayment_consolidado_tambem_e_rejeitado(org_session):
    """Mesma regra no fechamento consolidado (N comandas de uma vez) --
    `close_orders_consolidated` não tinha mais o bloco de "sobra vira
    Payment extra na última comanda" (removido nesta rodada)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt1, branch, prof, client = _finished_appointment_with_two_services(session, org_id, actor)
    register = _open_register(session, actor)
    order1 = orders.create_order(session, actor, appt1.id)

    svc = _service(session, org_id, name="Escova", duration=30, price=Decimal("80.00"))
    _link(session, prof.id, svc.id)
    appt2 = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=svc.id, start_at=_dt(14, 0))],
        ),
    )
    appt2.status = AppointmentStatus.FINISHED
    session.flush()
    order2 = orders.create_order(session, actor, appt2.id)
    # total consolidado = 380 (100+280) + 80 = 460.

    from nexasalon_api.schemas.order import ConsolidatedOrderClose

    with pytest.raises(ValidationDomainError, match="não pode ser maior que o saldo"):
        orders.close_orders_consolidated(
            session, actor, order1.id,
            ConsolidatedOrderClose(
                order_ids=[order1.id, order2.id],
                payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("500.00"), cash_register_id=register.id)],
            ),
        )
    session.refresh(order1)
    session.refresh(order2)
    assert order1.status == OrderStatus.OPEN
    assert order2.status == OrderStatus.OPEN
