import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexasalon_api.models.product import StockLevel


def get(session: Session, organization_id: uuid.UUID, product_id: uuid.UUID, branch_id: uuid.UUID) -> StockLevel | None:
    stmt = select(StockLevel).where(
        StockLevel.organization_id == organization_id,
        StockLevel.product_id == product_id,
        StockLevel.branch_id == branch_id,
    )
    return session.scalars(stmt).first()


def list_for_product(session: Session, organization_id: uuid.UUID, product_id: uuid.UUID) -> list[StockLevel]:
    stmt = select(StockLevel).where(
        StockLevel.organization_id == organization_id, StockLevel.product_id == product_id
    ).order_by(StockLevel.branch_id)
    return list(session.scalars(stmt).all())


def list_for_branch(session: Session, organization_id: uuid.UUID, branch_id: uuid.UUID) -> list[StockLevel]:
    stmt = select(StockLevel).where(
        StockLevel.organization_id == organization_id, StockLevel.branch_id == branch_id
    )
    return list(session.scalars(stmt).all())


def list_for_org(session: Session, organization_id: uuid.UUID) -> list[StockLevel]:
    stmt = select(StockLevel).where(StockLevel.organization_id == organization_id)
    return list(session.scalars(stmt).all())


def lock_or_create(
    session: Session, organization_id: uuid.UUID, product_id: uuid.UUID, branch_id: uuid.UUID
) -> StockLevel:
    """Trava (`SELECT ... FOR UPDATE`) a linha de saldo do produto nesta
    unidade — chamado sempre ANTES de aplicar um delta de quantidade
    (ver `services/stock.py::_apply_delta`), pra que duas movimentações
    concorrentes no mesmo (produto, unidade) nunca leiam o mesmo saldo
    "velho" e produzam um resultado incorreto (condição de corrida
    clássica de decremento). Se a linha ainda não existe, cria em zero
    dentro de um SAVEPOINT — se outra transação criar a mesma linha ao
    mesmo tempo (colisão na constraint única `product_id`+`branch_id`),
    o SAVEPOINT dá rollback sozinho e a linha recém-criada pela OUTRA
    transação é relida (com o lock desta vez), em vez de propagar o
    erro pro chamador."""
    stmt = select(StockLevel).where(
        StockLevel.organization_id == organization_id,
        StockLevel.product_id == product_id,
        StockLevel.branch_id == branch_id,
    ).with_for_update()
    level = session.scalars(stmt).first()
    if level is not None:
        return level

    try:
        with session.begin_nested():
            level = StockLevel(
                organization_id=organization_id,
                product_id=product_id,
                branch_id=branch_id,
                quantity_on_hand=Decimal("0"),
                minimum_quantity=Decimal("0"),
            )
            session.add(level)
            session.flush()
        return level
    except IntegrityError:
        level = session.scalars(stmt).first()
        assert level is not None, "linha deveria existir após IntegrityError de criação concorrente"
        return level


def set_minimum_quantity(session: Session, level: StockLevel, minimum_quantity: Decimal) -> StockLevel:
    level.minimum_quantity = minimum_quantity
    session.flush()
    session.refresh(level)  # normaliza `minimum_quantity`/`quantity_on_hand` pra escala da coluna
    return level
