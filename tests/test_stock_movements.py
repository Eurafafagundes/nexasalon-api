"""Testes de `services/stock.py` — Movimentações e Transferência entre
unidades (Etapa B). Cobre o checklist explícito do pedido: toda
mudança de saldo gera movimentação (nunca overwrite silencioso),
motivo tem que combinar com a direção, saída nunca deixa saldo
negativo (mesmo sob concorrência real), e transferência gera o par
ligado de movimentações sem lançamento financeiro."""
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import NotFoundError, ValidationDomainError
from nexasalon_api.models.enums import StockMovementDirection, StockMovementReason
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch, Organization
from nexasalon_api.repositories import stock_level_repo
from nexasalon_api.schemas.product import ProductCreate
from nexasalon_api.services import products, stock

_PERMS = frozenset({"inventory.view", "inventory.view_cost", "inventory.manage"})


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org estoque", slug=f"org-estoque-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=_PERMS) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Responsável")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def _branch(session, org_id, name="Unidade") -> uuid.UUID:
    b = Branch(organization_id=org_id, name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(b)
    session.flush()
    return b.id


def _product(session, actor) -> uuid.UUID:
    return products.create_product(session, actor, ProductCreate(name="Esmalte", cost_price="5.00")).id


# ---------------------------------------------------------------------
# Movimentação manual — entrada/saída, motivo x direção, nunca overwrite
# ---------------------------------------------------------------------


def test_entrada_aumenta_saldo_e_gera_movimentacao(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product_id = _product(session, actor)

    movement = stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("10"),
    )
    assert movement.quantity == Decimal("10")
    level = stock_level_repo.get(session, org_id, product_id, branch_id)
    assert level.quantity_on_hand == Decimal("10")


def test_saida_diminui_saldo_e_nunca_fica_negativa(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product_id = _product(session, actor)

    stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("5"),
    )
    with pytest.raises(ValidationDomainError):
        stock.record_movement(
            session, actor, product_id=product_id, branch_id=branch_id,
            direction=StockMovementDirection.OUT, reason=StockMovementReason.SALE, quantity=Decimal("6"),
        )
    # saldo permanece o mesmo — a tentativa recusada não deixou rastro
    level = stock_level_repo.get(session, org_id, product_id, branch_id)
    assert level.quantity_on_hand == Decimal("5")


def test_cada_movimentacao_gera_uma_linha_no_ledger_nunca_sobrescreve(org_session):
    """Item "nunca overwrite silencioso": duas entradas seguidas do
    mesmo produto/unidade produzem DUAS linhas em `stock_movements`,
    nunca uma atualiza a outra."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product_id = _product(session, actor)

    stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("3"),
    )
    stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_id,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("4"),
    )
    movements = stock.list_movements(session, actor, product_id=product_id)
    assert len(movements) == 2
    level = stock_level_repo.get(session, org_id, product_id, branch_id)
    assert level.quantity_on_hand == Decimal("7")


def test_motivo_incompativel_com_direcao_e_recusado_no_schema():
    """`TRANSFER_IN` nunca pode ser criado manualmente com direção OUT
    (nem com IN, na verdade — é reservado à transferência) — o schema
    Pydantic já recusa antes de chegar no service."""
    from pydantic import ValidationError

    from nexasalon_api.schemas.stock import StockMovementCreate

    with pytest.raises(ValidationError):
        StockMovementCreate(
            product_id=uuid.uuid4(), branch_id=uuid.uuid4(),
            direction=StockMovementDirection.OUT, reason=StockMovementReason.PURCHASE, quantity=Decimal("1"),
        )
    with pytest.raises(ValidationError):
        StockMovementCreate(
            product_id=uuid.uuid4(), branch_id=uuid.uuid4(),
            direction=StockMovementDirection.IN, reason=StockMovementReason.TRANSFER_IN, quantity=Decimal("1"),
        )


def test_motivo_reservado_de_sistema_e_recusado_no_service_mesmo_bypassando_schema(org_session):
    """Defesa em profundidade: mesmo chamando `record_movement`
    diretamente (bypassando o schema), o service recusa
    `TRANSFER_IN`/`TRANSFER_OUT`/`INVENTORY_COUNT` como motivo manual."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product_id = _product(session, actor)

    with pytest.raises(ValidationDomainError):
        stock.record_movement(
            session, actor, product_id=product_id, branch_id=branch_id,
            direction=StockMovementDirection.IN, reason=StockMovementReason.TRANSFER_IN, quantity=Decimal("1"),
        )


def test_movimentacao_de_produto_inexistente_e_404(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)

    with pytest.raises(NotFoundError):
        stock.record_movement(
            session, actor, product_id=uuid.uuid4(), branch_id=branch_id,
            direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("1"),
        )


def test_saldo_e_por_unidade_nunca_global(org_session):
    """Item explícito "nunca misturar global com quantidade por
    filial": entrada numa unidade não afeta o saldo de outra."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_a = _branch(session, org_id, "Matriz")
    branch_b = _branch(session, org_id, "Filial")
    product_id = _product(session, actor)

    stock.record_movement(
        session, actor, product_id=product_id, branch_id=branch_a,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("10"),
    )
    level_a = stock_level_repo.get(session, org_id, product_id, branch_a)
    level_b = stock_level_repo.get(session, org_id, product_id, branch_b)
    assert level_a.quantity_on_hand == Decimal("10")
    assert level_b is None  # nunca criado "de graça" — nem em zero


# ---------------------------------------------------------------------
# Transferência entre unidades
# ---------------------------------------------------------------------


def test_transferencia_gera_par_de_movimentacoes_ligadas(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    origin = _branch(session, org_id, "Origem")
    destination = _branch(session, org_id, "Destino")
    product_id = _product(session, actor)
    stock.record_movement(
        session, actor, product_id=product_id, branch_id=origin,
        direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("10"),
    )

    transfer = stock.create_transfer(
        session, actor, product_id=product_id, origin_branch_id=origin,
        destination_branch_id=destination, quantity=Decimal("4"),
    )

    origin_level = stock_level_repo.get(session, org_id, product_id, origin)
    destination_level = stock_level_repo.get(session, org_id, product_id, destination)
    assert origin_level.quantity_on_hand == Decimal("6")
    assert destination_level.quantity_on_hand == Decimal("4")

    movements = stock.list_movements(session, actor, product_id=product_id)
    transfer_movements = [m for m in movements if m.transfer_id == transfer.id]
    assert len(transfer_movements) == 2
    reasons = {m.reason for m in transfer_movements}
    assert reasons == {StockMovementReason.TRANSFER_OUT, StockMovementReason.TRANSFER_IN}
    # nenhum lançamento financeiro fictício — a movimentação nunca toca
    # CashMovement/Payment (assert estrutural: os únicos módulos
    # importados por services/stock.py são os de estoque, ver imports
    # do próprio arquivo — aqui confirmamos que não há vestígio de
    # "amount"/"cash" na movimentação gerada).
    for m in transfer_movements:
        assert not hasattr(m, "amount")


def test_transferencia_sem_saldo_suficiente_nao_cria_movimentacao_parcial(org_session):
    """Se a origem não tem saldo, a transferência inteira falha — nunca
    fica só a saída sem a entrada correspondente (atomicidade)."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    origin = _branch(session, org_id, "Origem")
    destination = _branch(session, org_id, "Destino")
    product_id = _product(session, actor)

    with pytest.raises(ValidationDomainError):
        stock.create_transfer(
            session, actor, product_id=product_id, origin_branch_id=origin,
            destination_branch_id=destination, quantity=Decimal("1"),
        )

    session.rollback()
    with SessionLocal() as fresh:
        fresh.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        movements = fresh.execute(text("SELECT count(*) FROM stock_movements")).scalar()
        assert movements == 0


def test_transferencia_para_mesma_unidade_e_recusada(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    branch_id = _branch(session, org_id)
    product_id = _product(session, actor)

    with pytest.raises(ValidationDomainError):
        stock.create_transfer(
            session, actor, product_id=product_id, origin_branch_id=branch_id,
            destination_branch_id=branch_id, quantity=Decimal("1"),
        )


# ---------------------------------------------------------------------
# Concorrência — item explícito "nunca estoque negativo"
# ---------------------------------------------------------------------


def test_duas_saidas_concorrentes_nunca_deixam_saldo_negativo():
    """Duas threads, cada uma com sua própria conexão/sessão, tentam
    tirar 6 unidades AO MESMO TEMPO de um saldo de 10 (total pedido:
    12, mais que o disponível). Um `threading.Barrier` maximiza a
    chance de as duas transações colidirem na mesma linha de
    `stock_levels` — o lock (`SELECT ... FOR UPDATE`, ver
    `stock_level_repo.lock_or_create`) precisa serializar as duas,
    nunca deixar as duas lerem "10 disponível" e ambas passarem."""
    org_id = uuid.uuid4()
    with SessionLocal() as setup:
        setup.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        setup.add(Organization(id=org_id, name="Org concorrência", slug=f"org-concorrencia-{org_id.hex[:8]}"))
        setup.flush()
        actor = _actor(setup, org_id)
        branch_id = _branch(setup, org_id)
        product_id = _product(setup, actor)
        stock.record_movement(
            setup, actor, product_id=product_id, branch_id=branch_id,
            direction=StockMovementDirection.IN, reason=StockMovementReason.PURCHASE, quantity=Decimal("10"),
        )
        setup.commit()

    barrier = threading.Barrier(2)
    results = {}

    def _attempt(label: str):
        with SessionLocal() as session:
            session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
            local_actor = ActorContext(
                organization_id=org_id, user_id=actor.user_id, membership_id=actor.membership_id,
                role_id=actor.role_id, role_name="Owner", permissions=_PERMS,
            )
            barrier.wait()
            try:
                stock.record_movement(
                    session, local_actor, product_id=product_id, branch_id=branch_id,
                    direction=StockMovementDirection.OUT, reason=StockMovementReason.SALE, quantity=Decimal("6"),
                )
                session.commit()
                results[label] = "ok"
            except ValidationDomainError:
                session.rollback()
                results[label] = "insufficient"

    t1 = threading.Thread(target=_attempt, args=("t1",))
    t2 = threading.Thread(target=_attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert set(results.values()) == {"ok", "insufficient"}, results

    with SessionLocal() as check:
        check.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        level = stock_level_repo.get(check, org_id, product_id, branch_id)
        assert level.quantity_on_hand == Decimal("4")  # 10 - 6, nunca negativo
