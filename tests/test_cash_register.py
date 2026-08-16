"""Testes do Caixa Diário (`services/cash_register.py`) — abertura,
sangria/suprimento, resumo (faturamento x saldo físico) e fechamento.
Mesma abordagem de `test_orders.py`: direto no service layer via
`SessionLocal`."""
import uuid
from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import CashMovementType, CashRegisterStatus, PaymentMethod
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.services import cash_register

_TZ = timezone(timedelta(hours=-3))


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org caixa", slug=f"org-caixa-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, name="Rafael") -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name=name)
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset({"finance.view", "finance.manage"}),
    )


def _branch(session, org_id) -> uuid.UUID:
    b = Branch(organization_id=org_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b.id


# ---------------------------------------------------------------------
# Abertura — responsável é sempre o usuário autenticado
# ---------------------------------------------------------------------


def test_abrir_caixa_usa_usuario_autenticado_como_responsavel(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, name="Rafael")
    branch = _branch(session, org_id)

    register = cash_register.open_register(session, actor, branch, Decimal("100.00"), "Troco inicial")

    assert register.opened_by == actor.user_id
    assert register.opened_by_name == "Rafael"
    assert register.initial_amount == Decimal("100.00")
    assert register.opening_notes == "Troco inicial"
    assert register.status == CashRegisterStatus.OPEN
    assert register.closed_at is None


def test_mesma_unidade_nao_pode_ter_dois_caixas_abertos(org_session):
    """Regra desta rodada: 1 caixa aberto POR UNIDADE (mudou da 0014,
    que era por usuário) — nem o MESMO usuário consegue abrir um
    segundo caixa na mesma unidade."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    cash_register.open_register(session, actor, branch, Decimal("0"), None)

    with pytest.raises(ConflictError):
        cash_register.open_register(session, actor, branch, Decimal("0"), None)


def test_unidades_diferentes_podem_ter_caixas_abertos_ao_mesmo_tempo(org_session):
    """Arquitetura precisa suportar múltiplos caixas simultâneos na
    mesma organização — desde que sejam de UNIDADES diferentes."""
    session, org_id = org_session
    rafael = _actor(session, org_id, name="Rafael")
    clara = _actor(session, org_id, name="Clara")
    branch_a = _branch(session, org_id)
    branch_b = _branch(session, org_id)

    r1 = cash_register.open_register(session, rafael, branch_a, Decimal("0"), None)
    r2 = cash_register.open_register(session, clara, branch_b, Decimal("0"), None)

    assert r1.id != r2.id
    open_registers = cash_register.list_open_registers(session, rafael)
    assert {r.id for r in open_registers} == {r1.id, r2.id}


# ---------------------------------------------------------------------
# Sangria / Suprimento — afetam saldo físico, nunca o faturamento
# ---------------------------------------------------------------------


def test_sangria_reduz_saldo_fisico_mas_nao_afeta_faturamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("100.00"), None)

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.WITHDRAWAL, Decimal("40.00"), "Retirada para depósito"
    )

    summary = cash_register.get_register_summary(session, actor, register.id)
    assert summary.withdrawals_total == Decimal("40.00")
    assert summary.expected_cash_balance == Decimal("60.00")  # 100 - 40
    assert summary.total_revenue == Decimal("0")  # sangria não é faturamento


def test_suprimento_aumenta_saldo_fisico_mas_nao_afeta_faturamento(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("100.00"), None)

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.SUPPLY, Decimal("50.00"), "Troco adicionado"
    )

    summary = cash_register.get_register_summary(session, actor, register.id)
    assert summary.supplies_total == Decimal("50.00")
    assert summary.expected_cash_balance == Decimal("150.00")  # 100 + 50
    assert summary.total_revenue == Decimal("0")


def test_nao_registra_movimentacao_em_caixa_fechado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)
    cash_register.close_register(session, actor, register.id, None, None)

    with pytest.raises(ValidationDomainError):
        cash_register.register_movement(session, actor, register.id, CashMovementType.SUPPLY, Decimal("10.00"), "Teste")


def test_movimentacao_registra_usuario_e_motivo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, name="Clara")
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.WITHDRAWAL, Decimal("500.00"), "Retirada para depósito bancário"
    )

    summary = cash_register.get_register_summary(session, actor, register.id)
    assert len(summary.movements) == 1
    movement = summary.movements[0]
    assert movement.type == CashMovementType.WITHDRAWAL
    assert movement.amount == Decimal("500.00")
    assert movement.description == "Retirada para depósito bancário"
    assert movement.created_by == actor.user_id
    assert movement.created_by_name == "Clara"


# ---------------------------------------------------------------------
# Resumo por forma de pagamento — nunca hardcoded a um subconjunto
# ---------------------------------------------------------------------


def test_resumo_inclui_todo_o_catalogo_de_metodos_mesmo_sem_transacao(org_session):
    """Item 'não deixar métodos de pagamento importantes hardcoded': o
    resumo cobre TODO `PaymentMethod`, não só um subconjunto fixo — um
    método sem nenhum pagamento ainda aparece com total 0 / 0
    transações, nunca simplesmente ausente."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)

    summary = cash_register.get_register_summary(session, actor, register.id)

    assert set(summary.totals_by_method.keys()) == set(PaymentMethod)
    for method in PaymentMethod:
        total, count = summary.totals_by_method[method]
        assert total == Decimal("0")
        assert count == 0


# ---------------------------------------------------------------------
# Fechamento — diferença = contado - esperado
# ---------------------------------------------------------------------


def test_fechar_caixa_com_contagem_fisica_calcula_diferenca(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id, name="Rafael")
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("100.00"), None)
    cash_register.register_movement(session, actor, register.id, CashMovementType.SUPPLY, Decimal("1150.00"), "Fechamento simulado")
    # esperado = 100 + 1150 = 1250

    closed = cash_register.close_register(session, actor, register.id, Decimal("1240.00"), "Fechamento do dia")

    assert closed.status == CashRegisterStatus.CLOSED
    assert closed.closed_by == actor.user_id
    assert closed.closed_by_name == "Rafael"
    assert closed.closing_notes == "Fechamento do dia"
    assert closed.expected_amount == Decimal("1250.00")
    assert closed.counted_amount == Decimal("1240.00")
    assert closed.difference == Decimal("-10.00")


def test_fechar_caixa_sem_contagem_fisica_deixa_diferenca_nula(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)

    closed = cash_register.close_register(session, actor, register.id, None, None)

    assert closed.counted_amount is None
    assert closed.difference is None
    assert closed.expected_amount == Decimal("0")


def test_nao_fecha_caixa_ja_fechado(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)
    cash_register.close_register(session, actor, register.id, None, None)

    with pytest.raises(ConflictError):
        cash_register.close_register(session, actor, register.id, None, None)


# ---------------------------------------------------------------------
# Histórico / filtros
# ---------------------------------------------------------------------


def test_historico_filtra_por_status_e_responsavel(org_session):
    session, org_id = org_session
    rafael = _actor(session, org_id, name="Rafael")
    clara = _actor(session, org_id, name="Clara")
    branch_a = _branch(session, org_id)
    branch_b = _branch(session, org_id)
    r1 = cash_register.open_register(session, rafael, branch_a, Decimal("0"), None)
    cash_register.close_register(session, rafael, r1.id, None, None)
    r2 = cash_register.open_register(session, clara, branch_b, Decimal("0"), None)

    open_only = cash_register.list_registers(session, rafael, status=CashRegisterStatus.OPEN)
    assert {r.id for r in open_only} == {r2.id}

    closed_only = cash_register.list_registers(session, rafael, status=CashRegisterStatus.CLOSED)
    assert {r.id for r in closed_only} == {r1.id}

    from_rafael = cash_register.list_registers(session, rafael, opened_by=rafael.user_id)
    assert {r.id for r in from_rafael} == {r1.id}


# ---------------------------------------------------------------------
# Isolamento multiempresa (RLS)
# ---------------------------------------------------------------------


def test_isolamento_multiempresa_caixa_de_outra_org_nao_aparece(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)

    other_org_id = uuid.uuid4()
    with SessionLocal() as other_session:
        other_session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(other_org_id)})
        other_session.add(Organization(id=other_org_id, name="Outra org", slug=f"outra-{other_org_id.hex[:8]}"))
        other_session.flush()
        other_actor = _actor(other_session, other_org_id)

        with pytest.raises(NotFoundError):
            cash_register.get_register(other_session, other_actor, register.id)
        assert cash_register.list_open_registers(other_session, other_actor) == []
        other_session.rollback()


# ---------------------------------------------------------------------
# Entrada/Despesa com método não-dinheiro (item 8/21/22)
# ---------------------------------------------------------------------


def test_despesa_paga_em_pix_nao_afeta_saldo_fisico_mas_conta_como_saida(org_session):
    """Item 8 'faturamento não é a mesma coisa que dinheiro', aplicado
    também a Entrada/Despesa manual: uma despesa paga em Pix reduz
    "Saídas" (totais gerais) mas NÃO mexe no saldo físico esperado —
    só despesa em DINHEIRO faz isso."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("100.00"), None)

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.WITHDRAWAL, Decimal("50.00"), "Compra de produtos",
        category="Produtos", method=PaymentMethod.PIX,
    )

    summary = cash_register.get_register_summary(session, actor, register.id)
    assert summary.withdrawals_total == Decimal("50.00")  # conta como saída "geral"
    assert summary.expected_cash_balance == Decimal("100.00")  # saldo físico intacto (não foi em dinheiro)
    assert summary.movements[0].category == "Produtos"
    assert summary.movements[0].method == PaymentMethod.PIX


def test_entrada_em_dinheiro_aumenta_saldo_fisico_entrada_em_pix_nao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)

    cash_register.register_movement(
        session, actor, register.id, CashMovementType.SUPPLY, Decimal("100.00"), "Aporte em dinheiro",
        method=PaymentMethod.CASH,
    )
    cash_register.register_movement(
        session, actor, register.id, CashMovementType.SUPPLY, Decimal("200.00"), "Aporte via Pix",
        method=PaymentMethod.PIX,
    )

    summary = cash_register.get_register_summary(session, actor, register.id)
    assert summary.supplies_total == Decimal("300.00")  # entradas "gerais" (qualquer método)
    assert summary.expected_cash_balance == Decimal("100.00")  # só a parte em dinheiro
    assert summary.total_entries == summary.total_revenue + Decimal("300.00")


# ---------------------------------------------------------------------
# Ticket médio (item 30) — por COMANDA paga, nunca por pagamento/item
# ---------------------------------------------------------------------


def test_ticket_medio_conta_por_comanda_paga_nao_por_pagamento(org_session):
    """Uma comanda com pagamento misto (2 lançamentos de Payment) conta
    UMA vez só no ticket médio, nunca duas."""
    from nexasalon_api.models.client import Client
    from nexasalon_api.repositories import order_repo, order_item_repo, payment_repo

    session, org_id = org_session
    actor = _actor(session, org_id)
    branch = _branch(session, org_id)
    register = cash_register.open_register(session, actor, branch, Decimal("0"), None)

    from nexasalon_api.models.appointment import Appointment
    from nexasalon_api.models.enums import AppointmentStatus, AppointmentSource
    from nexasalon_api.models.professional import Professional
    from nexasalon_api.models.service import Service

    client = Client(organization_id=org_id, name="Cliente Ticket")
    session.add(client)
    prof = Professional(organization_id=org_id, branch_id=branch, name="Profissional")
    session.add(prof)
    service = Service(organization_id=org_id, name="Serviço", default_duration_minutes=60, default_price=Decimal("100"))
    session.add(service)
    session.flush()

    appt = Appointment(
        organization_id=org_id, branch_id=branch, client_id=client.id, source=AppointmentSource.INTERNAL,
        status=AppointmentStatus.FINISHED,
    )
    session.add(appt)
    session.flush()

    order = order_repo.create(session, org_id, appointment_id=appt.id, branch_id=branch, client_id=client.id, created_by=actor.user_id)
    order_item_repo.create(
        session, org_id, order_id=order.id, appointment_item_id=None, service_id=service.id,
        professional_id=prof.id, duration_minutes=60, price=Decimal("800.00"),
        service_name="Serviço", professional_name="Profissional",
    )
    session.flush()
    payment_repo.create(
        session, org_id, order_id=order.id, cash_register_id=register.id, method=PaymentMethod.PIX,
        card_brand=None, installments=None, amount=Decimal("300.00"), created_by=actor.user_id, created_by_name="Rafael",
    )
    payment_repo.create(
        session, org_id, order_id=order.id, cash_register_id=register.id, method=PaymentMethod.CREDIT,
        card_brand=None, installments=None, amount=Decimal("500.00"), created_by=actor.user_id, created_by_name="Rafael",
    )
    session.flush()

    summary = cash_register.get_register_summary(session, actor, register.id)
    assert summary.orders_count == 1  # não 2, mesmo com 2 pagamentos
    assert summary.total_revenue == Decimal("800.00")
    assert summary.average_ticket == Decimal("800.00")
