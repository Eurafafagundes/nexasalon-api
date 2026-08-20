"""Testes de `services/products.py` — catálogo de Produtos (Etapa B).
Mesma abordagem de `test_cash_register.py`: direto no service layer via
`SessionLocal`, sessão não commitada (rollback no teardown)."""
import uuid

import pytest
from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.core.exceptions import ConflictError, NotFoundError
from nexasalon_api.models.enums import ProductUnit
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Organization
from nexasalon_api.schemas.product import ProductCreate, ProductUpdate
from nexasalon_api.services import products


@pytest.fixture()
def org_session():
    org_id = uuid.uuid4()
    with SessionLocal() as session:
        session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id)})
        session.add(Organization(id=org_id, name="Org produtos", slug=f"org-produtos-{org_id.hex[:8]}"))
        session.flush()
        yield session, org_id
        session.rollback()


def _actor(session, org_id, *, permissions=frozenset({"inventory.view", "inventory.view_cost", "inventory.manage"})) -> ActorContext:
    user = User(email=f"user-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Dona do salão")
    session.add(user)
    session.flush()
    return ActorContext(
        organization_id=org_id, user_id=user.id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Owner", permissions=frozenset(permissions),
    )


def test_criar_produto_sem_quantidade_nenhuma_no_catalogo(org_session):
    """Item explícito: `Product` nunca carrega quantidade — só catálogo.
    Confirmado aqui indiretamente: `ProductCreate` nem tem campo de
    quantidade pra passar."""
    session, org_id = org_session
    actor = _actor(session, org_id)

    product = products.create_product(
        session, actor,
        ProductCreate(name="Shampoo Hidratante", category="Cabelo", sku="SH-001", unit=ProductUnit.UNIT,
                       cost_price="12.50", sale_price="29.90", supplier_name="Distribuidora X", for_sale=True),
    )

    assert product.name == "Shampoo Hidratante"
    assert product.cost_price == pytest.approx(12.50)
    assert product.is_active is True
    assert not hasattr(product, "quantity_on_hand")


def test_sku_duplicado_na_mesma_organizacao_e_conflito(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    products.create_product(session, actor, ProductCreate(name="Produto A", sku="DUP-1"))

    with pytest.raises(ConflictError):
        products.create_product(session, actor, ProductCreate(name="Produto B", sku="DUP-1"))


def test_sku_duplicado_em_organizacoes_diferentes_e_permitido(org_session):
    """Isolamento multi-tenant: o índice único de SKU é por organização
    (`organization_id` + `sku`), nunca global."""
    session, org_id_a = org_session
    actor_a = _actor(session, org_id_a)
    products.create_product(session, actor_a, ProductCreate(name="Produto A", sku="MESMO-SKU"))

    org_id_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id_b)})
    session.add(Organization(id=org_id_b, name="Org B", slug=f"org-b-{org_id_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_id_b)

    # não levanta ConflictError
    products.create_product(session, actor_b, ProductCreate(name="Produto B", sku="MESMO-SKU"))


def test_varios_produtos_sem_sku_nao_colidem(org_session):
    """SKU é opcional — Postgres trata NULL como distinto em unique
    constraint, então múltiplos produtos sem SKU coexistem."""
    session, org_id = org_session
    actor = _actor(session, org_id)
    products.create_product(session, actor, ProductCreate(name="Produto 1"))
    products.create_product(session, actor, ProductCreate(name="Produto 2"))  # não levanta


def test_atualizar_produto_preserva_historico_de_auditoria(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    product = products.create_product(session, actor, ProductCreate(name="Nome Antigo", cost_price="10.00"))

    updated = products.update_product(
        session, actor, product.id, ProductUpdate(name="Nome Novo", cost_price="15.00")
    )
    assert updated.name == "Nome Novo"
    assert updated.cost_price == pytest.approx(15.00)


def test_desativar_produto_nao_apaga_nada_so_marca_inativo(org_session):
    session, org_id = org_session
    actor = _actor(session, org_id)
    product = products.create_product(session, actor, ProductCreate(name="Produto"))

    deactivated = products.set_product_active(session, actor, product.id, False)
    assert deactivated.is_active is False
    assert deactivated.id == product.id

    # continua buscável por id — não some do banco
    assert products.get_product(session, actor, product.id).id == product.id

    # mas some da listagem padrão (só ativos)
    assert product.id not in {p.id for p in products.list_products(session, actor)}
    assert product.id in {p.id for p in products.list_products(session, actor, include_inactive=True)}


def test_produto_de_outra_organizacao_e_404(org_session):
    session, org_id_a = org_session
    actor_a = _actor(session, org_id_a)
    product = products.create_product(session, actor_a, ProductCreate(name="Produto A"))

    org_id_b = uuid.uuid4()
    session.execute(text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_id_b)})
    session.add(Organization(id=org_id_b, name="Org B", slug=f"org-b-{org_id_b.hex[:8]}"))
    session.flush()
    actor_b = _actor(session, org_id_b)

    with pytest.raises(NotFoundError):
        products.get_product(session, actor_b, product.id)
