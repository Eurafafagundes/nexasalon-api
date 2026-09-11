"""Testes de Taxas de Pagamento (Etapa N3) — `services/payment_fees.py`
+ integração com `services/orders.py::close_order`/`close_orders_consolidated`.

Mesma abordagem de `test_orders.py`/`test_extract.py`: direto no service
layer via `SessionLocal`, sem rota HTTP.

Cobre os riscos explicitamente listados pelo usuário: Pix/Dinheiro nunca
têm taxa; cartão SEM regra correspondente fica "taxa não configurada"
(nunca 0% inventado); taxa é resolvida por Payment individual (nunca
sobre o total da comanda — pagamento dividido); alterar uma regra nunca
recalcula uma venda antiga (snapshot imutável); isolamento entre
organizações; arredondamento com Decimal; fechamento consolidado usa a
MESMA função de domínio que o fechamento simples."""
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError
from nexasalon_api.models.client import Client
from nexasalon_api.models.enums import (
    AppointmentStatus,
    CardBrand,
    PaymentFeeStatus,
    PaymentMethod,
)
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.models.professional import Professional, WorkingHours
from nexasalon_api.models.service import ProfessionalService, Service
from nexasalon_api.schemas.appointment import AppointmentCreate, AppointmentItemCreate
from nexasalon_api.schemas.order import (
    ConsolidatedOrderClose,
    OrderClose,
    PaymentCreate,
)
from nexasalon_api.schemas.payment_fee_rule import (
    PaymentFeeRuleCreate,
    PaymentFeeRuleUpdate,
)
from nexasalon_api.services import (
    appointments,
    cash_register,
    orders,
    payment_fee_rules,
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
        session.add(Organization(id=org_id, name="Org taxas", slug=f"org-taxas-{org_id.hex[:8]}"))
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


def _finished_appointment(session, org_id, actor, *, price=Decimal("1000.00"), client_name="Cliente"):
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch.id, Decimal("0"), None)
    prof = _professional(session, org_id, branch.id)
    service = _service(session, org_id, name="Corte", duration=60, price=price)
    _link(session, prof.id, service.id)
    _working_hours(session, org_id, prof.id, _THURSDAY, time(9, 0), time(20, 0))
    client = _client(session, org_id, name=client_name)
    appt = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof.id, service_id=service.id, start_at=_dt(9, 0))],
        ),
    )
    appt.status = AppointmentStatus.FINISHED
    session.flush()
    return appt, branch, client


def _open_register(session, actor, initial_amount=Decimal("0")):
    branch_id = _branch(session, actor.organization_id).id
    return cash_register.open_register(session, actor, branch_id, initial_amount, None)


def _create_rule(session, org_id, *, method, card_brand, installments=1, fee_percent, actor=None):
    # A maioria dos testes deste arquivo só quer UMA regra cadastrada,
    # sem se importar com quem criou — cria um ator descartável quando
    # o chamador não passa um (evita repetir `_actor(...)` em toda
    # chamada só pra satisfazer o `user_id` do AuditLog).
    actor = actor or _actor(session, org_id)
    return payment_fee_rules.create_rule(
        session, actor,
        PaymentFeeRuleCreate(method=method, card_brand=card_brand, installments=installments, fee_percent=fee_percent),
    )


# ---------------------------------------------------------------------
# Pix / Dinheiro — sem incidência de taxa, sempre.
# ---------------------------------------------------------------------


def test_pix_taxa_zero_liquido_igual_ao_bruto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("500.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("500.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.NOT_APPLICABLE
    assert payment.fee_percent_snapshot is None
    assert payment.fee_amount_snapshot is None
    assert payment.net_amount_snapshot is None  # NOT_APPLICABLE não grava snapshot numérico — é sempre derivável.


def test_pix_com_regra_ativa_calcula_taxa_e_liquido(org_session):
    """Etapa N3.1 — Pix passa pelo MESMO pipeline canônico que cartão
    (`resolve_fee`): com uma `PaymentFeeRule` ativa correspondente,
    `fee_status=CALCULATED` e os 3 snapshots são preenchidos com o
    valor real, exatamente como débito/crédito."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.PIX, card_brand=None, fee_percent=Decimal("0.99"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("1000.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.CALCULATED
    assert payment.fee_percent_snapshot == Decimal("0.99")
    assert payment.fee_amount_snapshot == Decimal("9.90")
    assert payment.net_amount_snapshot == Decimal("990.10")
    assert payment.payment_fee_rule_id is not None
    assert payment.card_brand is None  # Pix nunca carrega bandeira no Payment, mesmo com regra ativa.


def test_pix_com_regra_inativa_volta_a_ser_nao_aplicavel_nunca_uma_taxa_desconhecida(org_session):
    """Diferente de débito/crédito (regra ausente/inativa = UNCONFIGURED,
    "taxa desconhecida"): Pix sem regra ATIVA correspondente continua
    `NOT_APPLICABLE` — item explícito do pedido "se não existir taxa de
    Pix configurada, o comportamento deve continuar equivalente ao
    atual" (Pix é opt-in, nunca gera um estado "desconhecido")."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    rule = _create_rule(session, org_id, method=PaymentMethod.PIX, card_brand=None, fee_percent=Decimal("0.99"))
    payment_fee_rules.set_rule_active(session, actor, rule.id, False)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("200.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("200.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.NOT_APPLICABLE
    assert payment.fee_amount_snapshot is None
    assert payment.net_amount_snapshot is None


def test_alterar_regra_de_pix_nao_modifica_snapshot_de_venda_antiga(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    rule = _create_rule(session, org_id, method=PaymentMethod.PIX, card_brand=None, fee_percent=Decimal("0.99"))

    appt_old, _b1, _c1 = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"), client_name="Venda Antiga Pix")
    order_old = orders.create_order(session, actor, appt_old.id)
    register_old = _open_register(session, actor)
    orders.close_order(
        session, actor, order_old.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("1000.00"), cash_register_id=register_old.id)]),
    )
    session.refresh(order_old)
    assert order_old.payments[0].fee_percent_snapshot == Decimal("0.99")

    payment_fee_rules.update_rule(session, actor, rule.id, PaymentFeeRuleUpdate(fee_percent=Decimal("1.50")))
    session.refresh(order_old)

    # Venda antiga permanece congelada em 0,99% — nunca recalculada.
    assert order_old.payments[0].fee_percent_snapshot == Decimal("0.99")
    assert order_old.payments[0].fee_amount_snapshot == Decimal("9.90")


def test_pix_rejeita_bandeira_na_regra(org_session):
    with pytest.raises(ValueError, match="não aceita bandeira"):
        PaymentFeeRuleCreate(method=PaymentMethod.PIX, card_brand=CardBrand.VISA, fee_percent=Decimal("0.99"))


def test_pix_rejeita_parcelamento_na_regra(org_session):
    with pytest.raises(ValueError, match="sempre à vista"):
        PaymentFeeRuleCreate(method=PaymentMethod.PIX, card_brand=None, installments=2, fee_percent=Decimal("0.99"))


def test_regra_de_pix_de_uma_organizacao_nunca_e_usada_por_outra():
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()

    with SessionLocal() as session_a:
        session_a.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_id)})
        session_a.add(Organization(id=org_a_id, name="Org A Pix", slug=f"org-a-pix-{org_a_id.hex[:8]}"))
        session_a.flush()
        actor_a = _actor(session_a, org_a_id)
        _create_rule(session_a, org_a_id, method=PaymentMethod.PIX, card_brand=None, fee_percent=Decimal("0.99"), actor=actor_a)
        appt_a, _b, _c = _finished_appointment(session_a, org_a_id, actor_a, price=Decimal("100.00"))
        order_a = orders.create_order(session_a, actor_a, appt_a.id)
        register_a = _open_register(session_a, actor_a)
        orders.close_order(
            session_a, actor_a, order_a.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("100.00"), cash_register_id=register_a.id)]),
        )
        session_a.refresh(order_a)
        assert order_a.payments[0].fee_status == PaymentFeeStatus.CALCULATED
        session_a.commit()  # precisa persistir de verdade — ver raciocínio do teste equivalente de cartão acima.

    with SessionLocal() as session_b:
        session_b.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b_id)})
        session_b.add(Organization(id=org_b_id, name="Org B Pix", slug=f"org-b-pix-{org_b_id.hex[:8]}"))
        session_b.flush()
        actor_b = _actor(session_b, org_b_id)
        # NENHUMA regra de Pix cadastrada na Org B.
        appt_b, _b2, _c2 = _finished_appointment(session_b, org_b_id, actor_b, price=Decimal("100.00"))
        order_b = orders.create_order(session_b, actor_b, appt_b.id)
        register_b = _open_register(session_b, actor_b)
        orders.close_order(
            session_b, actor_b, order_b.id,
            OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("100.00"), cash_register_id=register_b.id)]),
        )
        session_b.refresh(order_b)
        # A regra da Org A NUNCA vaza pra Org B — continua NOT_APPLICABLE (nunca herda taxa alheia).
        assert order_b.payments[0].fee_status == PaymentFeeStatus.NOT_APPLICABLE
        assert order_b.payments[0].fee_amount_snapshot is None
        session_b.rollback()


def test_criar_regra_de_pix_duplicada_na_mesma_organizacao_e_recusado(org_session):
    session, org_id = org_session
    _create_rule(session, org_id, method=PaymentMethod.PIX, card_brand=None, fee_percent=Decimal("0.99"))
    with pytest.raises(ConflictError):
        _create_rule(session, org_id, method=PaymentMethod.PIX, card_brand=None, fee_percent=Decimal("1.20"))


def test_debito_e_credito_continuam_inalterados_apos_pix_entrar_no_pipeline(org_session):
    """Regressão: débito e crédito não podem ter mudado de comportamento
    só porque Pix passou a ser um método elegível a taxa."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.DEBIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("1.39"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.DEBIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]
    assert payment.fee_status == PaymentFeeStatus.CALCULATED
    assert payment.fee_percent_snapshot == Decimal("1.39")
    assert payment.fee_amount_snapshot == Decimal("13.90")


def test_dinheiro_taxa_zero_liquido_igual_ao_bruto(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("200.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.CASH, amount=Decimal("200.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.NOT_APPLICABLE
    assert payment.fee_amount_snapshot is None


# ---------------------------------------------------------------------
# Débito/Crédito COM regra — taxa calculada e congelada.
# ---------------------------------------------------------------------


def test_debito_com_regra_calcula_taxa_e_liquido(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.DEBIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("1.39"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.DEBIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.CALCULATED
    assert payment.fee_percent_snapshot == Decimal("1.39")
    assert payment.fee_amount_snapshot == Decimal("13.90")
    assert payment.net_amount_snapshot == Decimal("986.10")
    assert payment.payment_fee_rule_id is not None


def test_credito_1x_com_regra(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.CALCULATED
    assert payment.fee_percent_snapshot == Decimal("2.99")
    assert payment.fee_amount_snapshot == Decimal("29.90")
    assert payment.net_amount_snapshot == Decimal("970.10")


def test_credito_parcelado_usa_a_regra_especifica_das_parcelas(org_session):
    """Visa Crédito 1x = 2,99%, Visa Crédito 2x = 3,49% — duas regras
    DIFERENTES pra parcelamentos diferentes da MESMA bandeira; a venda
    2x tem que usar a regra 2x, nunca a de 1x."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=2, fee_percent=Decimal("3.49"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, installments=2, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_percent_snapshot == Decimal("3.49")
    assert payment.fee_amount_snapshot == Decimal("34.90")


def test_bandeiras_diferentes_podem_ter_taxas_diferentes(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.MASTERCARD, installments=1, fee_percent=Decimal("3.09"))

    appt_a, _b1, _c1 = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"), client_name="Cliente A")
    order_a = orders.create_order(session, actor, appt_a.id)
    register_a = _open_register(session, actor)
    orders.close_order(
        session, actor, order_a.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register_a.id)
        ]),
    )
    session.refresh(order_a)

    appt_b, _b2, _c2 = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"), client_name="Cliente B")
    order_b = orders.create_order(session, actor, appt_b.id)
    register_b = _open_register(session, actor)
    orders.close_order(
        session, actor, order_b.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.MASTERCARD, installments=1, cash_register_id=register_b.id)
        ]),
    )
    session.refresh(order_b)

    assert order_a.payments[0].fee_percent_snapshot == Decimal("2.99")
    assert order_b.payments[0].fee_percent_snapshot == Decimal("3.09")


# ---------------------------------------------------------------------
# Débito/Crédito SEM regra — "taxa não configurada", NUNCA 0%.
# ---------------------------------------------------------------------


def test_credito_sem_regra_fica_taxa_nao_configurada_nunca_zero(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    # Fechamento tem que continuar PERMITIDO mesmo sem regra cadastrada
    # (item explícito "o pagamento deve continuar sendo permitido").
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.ELO, installments=1, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.UNCONFIGURED
    assert payment.fee_percent_snapshot is None
    assert payment.fee_amount_snapshot is None
    assert payment.net_amount_snapshot is None  # NUNCA amount (nunca finge taxa=0%).
    assert payment.payment_fee_rule_id is None


def test_debito_sem_regra_fica_taxa_nao_configurada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("300.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.DEBIT, amount=Decimal("300.00"), card_brand=CardBrand.HIPERCARD, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert payment.fee_status == PaymentFeeStatus.UNCONFIGURED
    assert payment.fee_amount_snapshot is None


# ---------------------------------------------------------------------
# Pagamento dividido — taxa exclusivamente sobre o Payment do cartão.
# ---------------------------------------------------------------------


def test_pagamento_dividido_pix_mais_credito_taxa_so_no_cartao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("500.00"), cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("500.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register.id),
        ]),
    )
    session.refresh(order)
    by_method = {p.method: p for p in order.payments}

    pix = by_method[PaymentMethod.PIX]
    credito = by_method[PaymentMethod.CREDIT]

    # A taxa NUNCA incide sobre o total da comanda (R$ 1.000) — só sobre
    # o valor do próprio Payment de cartão (R$ 500).
    assert pix.fee_status == PaymentFeeStatus.NOT_APPLICABLE
    assert credito.fee_status == PaymentFeeStatus.CALCULATED
    assert credito.fee_percent_snapshot == Decimal("2.99")
    assert credito.fee_amount_snapshot == Decimal("14.95")  # 500 * 2.99% = 14.95, não 29.90 (1000 * 2.99%).
    assert credito.net_amount_snapshot == Decimal("485.05")


def test_taxa_aplicada_somente_ao_payment_correto_em_tres_lancamentos(org_session):
    """3 pagamentos na mesma comanda (Pix + Débito + Crédito), cada um
    com sua PRÓPRIA regra — nenhuma taxa vaza pro pagamento errado."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.DEBIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("1.39"))
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.MASTERCARD, installments=1, fee_percent=Decimal("3.09"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("900.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("300.00"), cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.DEBIT, amount=Decimal("300.00"), card_brand=CardBrand.VISA, cash_register_id=register.id),
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("300.00"), card_brand=CardBrand.MASTERCARD, installments=1, cash_register_id=register.id),
        ]),
    )
    session.refresh(order)
    by_method = {p.method: p for p in order.payments}

    assert by_method[PaymentMethod.PIX].fee_amount_snapshot is None
    assert by_method[PaymentMethod.DEBIT].fee_percent_snapshot == Decimal("1.39")
    assert by_method[PaymentMethod.CREDIT].fee_percent_snapshot == Decimal("3.09")


# ---------------------------------------------------------------------
# Alteração futura da regra — snapshot antigo NUNCA muda.
# ---------------------------------------------------------------------


def test_alterar_regra_nao_modifica_snapshot_de_venda_antiga(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    rule = _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))

    appt_old, _b1, _c1 = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"), client_name="Venda Antiga")
    order_old = orders.create_order(session, actor, appt_old.id)
    register_old = _open_register(session, actor)
    orders.close_order(
        session, actor, order_old.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register_old.id)
        ]),
    )
    session.refresh(order_old)
    old_fee = order_old.payments[0].fee_percent_snapshot
    assert old_fee == Decimal("2.99")

    # Configuração muda amanhã: 2,99% -> 3,20%.
    payment_fee_rules.update_rule(session, actor, rule.id, PaymentFeeRuleUpdate(fee_percent=Decimal("3.20")))
    session.refresh(order_old)

    # A venda ANTIGA continua com o snapshot de 2,99% — nunca recalculada.
    assert order_old.payments[0].fee_percent_snapshot == Decimal("2.99")
    assert order_old.payments[0].fee_amount_snapshot == Decimal("29.90")


def test_nova_venda_usa_a_nova_taxa_apos_alteracao_da_regra(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    rule = _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    payment_fee_rules.update_rule(session, actor, rule.id, PaymentFeeRuleUpdate(fee_percent=Decimal("3.20")))

    appt_new, _b2, _c2 = _finished_appointment(session, org_id, actor, price=Decimal("1000.00"), client_name="Venda Nova")
    order_new = orders.create_order(session, actor, appt_new.id)
    register_new = _open_register(session, actor)
    orders.close_order(
        session, actor, order_new.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("1000.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register_new.id)
        ]),
    )
    session.refresh(order_new)

    assert order_new.payments[0].fee_percent_snapshot == Decimal("3.20")
    assert order_new.payments[0].fee_amount_snapshot == Decimal("32.00")


# ---------------------------------------------------------------------
# Compatibilidade histórica.
# ---------------------------------------------------------------------


def test_pagamento_historico_sem_snapshot_continua_valido(org_session):
    """Simula um `Payment` criado ANTES da migration 0034 (os 5 campos
    novos nascem `NULL`, nunca backfilled) — a leitura via
    `derive_fee_status`/`breakdown_for_display` precisa continuar
    funcionando, sem erro e sem inventar taxa."""
    from nexasalon_api.services import payment_fees

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("400.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("400.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    # "apaga" os campos novos pra simular dado pré-migration.
    payment.fee_status = None
    payment.fee_percent_snapshot = None
    payment.fee_amount_snapshot = None
    payment.net_amount_snapshot = None
    payment.payment_fee_rule_id = None
    session.flush()

    assert payment_fees.derive_fee_status(payment) == PaymentFeeStatus.UNCONFIGURED  # cartão sem dado = desconhecida, nunca 0%.
    breakdown = payment_fees.breakdown_for_display(payment)
    assert breakdown.fee_amount is None
    assert breakdown.net_amount is None


def test_pagamento_historico_pix_sem_snapshot_e_sempre_sem_incidencia(org_session):
    """Diferente do cartão: Pix histórico SEM snapshot ainda é
    corretamente classificado como "sem incidência" (nunca "desconhecido"
    — Pix nunca teve taxa, com ou sem a coluna nova existindo)."""
    from nexasalon_api.services import payment_fees

    session, org_id = org_session
    actor = _actor(session, org_id)
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("150.00"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)
    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[PaymentCreate(method=PaymentMethod.PIX, amount=Decimal("150.00"), cash_register_id=register.id)]),
    )
    session.refresh(order)
    payment = order.payments[0]
    payment.fee_status = None
    session.flush()

    assert payment_fees.derive_fee_status(payment) == PaymentFeeStatus.NOT_APPLICABLE
    breakdown = payment_fees.breakdown_for_display(payment)
    assert breakdown.fee_amount == Decimal("0")
    assert breakdown.net_amount == payment.amount


# ---------------------------------------------------------------------
# Arredondamento com Decimal.
# ---------------------------------------------------------------------


def test_arredondamento_de_fracao_de_centavo_usa_decimal(org_session):
    """R$ 333,33 * 2,99% = R$ 9,966717 -> arredonda pra R$ 9,97 (2 casas,
    Decimal, nunca float)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    appt, _branch, client = _finished_appointment(session, org_id, actor, price=Decimal("333.33"))
    order = orders.create_order(session, actor, appt.id)
    register = _open_register(session, actor)

    orders.close_order(
        session, actor, order.id,
        OrderClose(payments=[
            PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("333.33"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register.id)
        ]),
    )
    session.refresh(order)
    payment = order.payments[0]

    assert isinstance(payment.fee_amount_snapshot, Decimal)
    assert payment.fee_amount_snapshot == Decimal("9.97")
    assert payment.net_amount_snapshot == Decimal("323.36")


# ---------------------------------------------------------------------
# Isolamento entre organizações.
# ---------------------------------------------------------------------


def test_regra_de_uma_organizacao_nunca_e_usada_por_outra():
    """Duas organizações distintas, cada uma com sua própria sessão/RLS
    — a regra da Org A não pode vazar pra uma venda da Org B, mesmo com
    a mesma bandeira/parcelas."""
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()

    with SessionLocal() as session_a:
        session_a.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_id)})
        session_a.add(Organization(id=org_a_id, name="Org A Taxas", slug=f"org-a-taxas-{org_a_id.hex[:8]}"))
        session_a.flush()
        actor_a = _actor(session_a, org_a_id)
        _create_rule(session_a, org_a_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
        appt_a, _b, _c = _finished_appointment(session_a, org_a_id, actor_a, price=Decimal("100.00"))
        order_a = orders.create_order(session_a, actor_a, appt_a.id)
        register_a = _open_register(session_a, actor_a)
        orders.close_order(
            session_a, actor_a, order_a.id,
            OrderClose(payments=[
                PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("100.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register_a.id)
            ]),
        )
        session_a.refresh(order_a)
        assert order_a.payments[0].fee_status == PaymentFeeStatus.CALCULATED
        # COMMIT (não rollback) — a regra da Org A precisa estar
        # persistida de verdade pra este teste provar algo: se ficasse
        # só na transação, a Org B jamais teria a chance de "vazar" pra
        # ela, e o teste passaria mesmo com uma RLS quebrada.
        session_a.commit()

    with SessionLocal() as session_b:
        session_b.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_b_id)})
        session_b.add(Organization(id=org_b_id, name="Org B Taxas", slug=f"org-b-taxas-{org_b_id.hex[:8]}"))
        session_b.flush()
        actor_b = _actor(session_b, org_b_id)
        # NENHUMA regra cadastrada na Org B — mesma bandeira/parcelas da Org A.
        appt_b, _b2, _c2 = _finished_appointment(session_b, org_b_id, actor_b, price=Decimal("100.00"))
        order_b = orders.create_order(session_b, actor_b, appt_b.id)
        register_b = _open_register(session_b, actor_b)
        orders.close_order(
            session_b, actor_b, order_b.id,
            OrderClose(payments=[
                PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("100.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register_b.id)
            ]),
        )
        session_b.refresh(order_b)
        # A regra da Org A NUNCA é enxergada pela Org B (RLS) — fica
        # "taxa não configurada", nunca herda o percentual da outra org.
        assert order_b.payments[0].fee_status == PaymentFeeStatus.UNCONFIGURED
        session_b.rollback()


def test_criar_regra_duplicada_na_mesma_organizacao_e_recusado(org_session):
    session, org_id = org_session
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))
    with pytest.raises(ConflictError):
        _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("3.50"))


# ---------------------------------------------------------------------
# Fechamento consolidado — MESMA função de domínio.
# ---------------------------------------------------------------------


def test_fechamento_consolidado_usa_a_mesma_logica_de_taxa(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    _create_rule(session, org_id, method=PaymentMethod.CREDIT, card_brand=CardBrand.VISA, installments=1, fee_percent=Decimal("2.99"))

    appt_1, branch, client = _finished_appointment(session, org_id, actor, price=Decimal("300.00"), client_name="Cliente Consolidado")
    order_1 = orders.create_order(session, actor, appt_1.id)

    prof2 = _professional(session, org_id, branch.id, name="Profissional 2")
    service2 = _service(session, org_id, name="Escova", duration=30, price=Decimal("200.00"))
    _link(session, prof2.id, service2.id)
    _working_hours(session, org_id, prof2.id, _THURSDAY, time(9, 0), time(20, 0))
    appt_2 = appointments.create_appointment(
        session, actor,
        AppointmentCreate(
            branch_id=branch.id, client_id=client.id,
            items=[AppointmentItemCreate(professional_id=prof2.id, service_id=service2.id, start_at=_dt(11, 0))],
        ),
    )
    appt_2.status = AppointmentStatus.FINISHED
    session.flush()
    order_2 = orders.create_order(session, actor, appt_2.id)

    register = _open_register(session, actor)
    orders.close_orders_consolidated(
        session, actor, order_1.id,
        ConsolidatedOrderClose(
            order_ids=[order_1.id, order_2.id],
            payments=[
                PaymentCreate(method=PaymentMethod.CREDIT, amount=Decimal("500.00"), card_brand=CardBrand.VISA, installments=1, cash_register_id=register.id)
            ],
        ),
    )
    session.refresh(order_1)
    session.refresh(order_2)

    all_payments = list(order_1.payments) + list(order_2.payments)
    assert len(all_payments) == 2  # split em 2 comandas = 2 Payments.
    for payment in all_payments:
        assert payment.fee_status == PaymentFeeStatus.CALCULATED
        assert payment.fee_percent_snapshot == Decimal("2.99")
        # cada Payment resolve a taxa sobre o PRÓPRIO valor pós-split.
        expected_fee = (payment.amount * Decimal("2.99") / Decimal("100")).quantize(Decimal("0.01"))
        assert payment.fee_amount_snapshot == expected_fee
