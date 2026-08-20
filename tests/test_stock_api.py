"""Testes HTTP de `/api/v1/products`, `/stock-movements`,
`/stock-transfers`, `/stock/overview` e `/inventory-counts` — RBAC
(`inventory.view`/`inventory.view_cost`/`inventory.manage`), a regra
"Ver estoque ≠ Ver custo dos produtos", e isolamento multi-tenant.
Mesmo padrão de `test_cash_registers_api.py`."""
import uuid

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch


def _branch_id(actor: ActorContext) -> str:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(actor.organization_id)}
        )
        branch = Branch(organization_id=actor.organization_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
        session.add(branch)
        session.flush()
        branch_id = str(branch.id)
        session.commit()
        return branch_id


def _restricted_actor(base_actor: ActorContext, *, permissions) -> ActorContext:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Restrito")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Restrito", permissions=frozenset(permissions),
    )


def test_criar_e_listar_produto_via_api(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = c.post("/api/v1/products", json={"name": "Esmalte Vermelho", "cost_price": "3.50", "sale_price": "9.90"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Esmalte Vermelho"
    assert body["cost_price"] == "3.50"  # org_a_actor tem todas as permissions (inclui view_cost)

    listed = c.get("/api/v1/products")
    assert listed.status_code == 200
    assert any(p["id"] == body["id"] for p in listed.json())


def test_ator_sem_view_cost_nunca_recebe_cost_price(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = c.post("/api/v1/products", json={"name": "Produto", "cost_price": "20.00"}).json()

    viewer = _restricted_actor(org_a_actor, permissions={"inventory.view"})
    c_viewer = client_as(viewer)

    detail = c_viewer.get(f"/api/v1/products/{created['id']}")
    assert detail.status_code == 200
    assert "cost_price" not in detail.json()

    listed = c_viewer.get("/api/v1/products")
    assert all("cost_price" not in p for p in listed.json())


def test_ator_com_view_cost_recebe_cost_price(client_as, org_a_actor):
    c = client_as(org_a_actor)
    created = c.post("/api/v1/products", json={"name": "Produto", "cost_price": "20.00"}).json()

    viewer = _restricted_actor(org_a_actor, permissions={"inventory.view", "inventory.view_cost"})
    c_viewer = client_as(viewer)

    detail = c_viewer.get(f"/api/v1/products/{created['id']}")
    assert detail.json()["cost_price"] == "20.00"


def test_ator_so_com_view_nao_pode_criar_produto(client_as, org_a_actor):
    viewer = _restricted_actor(org_a_actor, permissions={"inventory.view"})
    c = client_as(viewer)
    resp = c.post("/api/v1/products", json={"name": "Produto"})
    assert resp.status_code == 403


def test_movimentacao_via_api_e_visao_geral(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = _branch_id(org_a_actor)
    product = c.post("/api/v1/products", json={"name": "Produto Estoque", "cost_price": "2.00"}).json()

    moved = c.post(
        "/api/v1/stock-movements",
        json={
            "product_id": product["id"], "branch_id": branch_id,
            "direction": "in", "reason": "purchase", "quantity": "10",
        },
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["quantity"] == "10.000"

    overview = c.get("/api/v1/stock/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["products_in_stock"] >= 1
    assert body["stock_value"] is not None  # org_a_actor tem inventory.view_cost


def test_visao_geral_sem_view_cost_nao_traz_stock_value(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = _branch_id(org_a_actor)
    product = c.post("/api/v1/products", json={"name": "Produto", "cost_price": "2.00"}).json()
    c.post(
        "/api/v1/stock-movements",
        json={"product_id": product["id"], "branch_id": branch_id, "direction": "in", "reason": "purchase", "quantity": "10"},
    )

    viewer = _restricted_actor(org_a_actor, permissions={"inventory.view"})
    c_viewer = client_as(viewer)
    overview = c_viewer.get("/api/v1/stock/overview")
    assert overview.status_code == 200
    assert overview.json()["stock_value"] is None


def test_transferencia_via_api(client_as, org_a_actor):
    c = client_as(org_a_actor)
    origin = _branch_id(org_a_actor)
    destination = _branch_id(org_a_actor)
    product = c.post("/api/v1/products", json={"name": "Produto"}).json()
    c.post(
        "/api/v1/stock-movements",
        json={"product_id": product["id"], "branch_id": origin, "direction": "in", "reason": "purchase", "quantity": "10"},
    )

    transfer = c.post(
        "/api/v1/stock-transfers",
        json={"product_id": product["id"], "origin_branch_id": origin, "destination_branch_id": destination, "quantity": "4"},
    )
    assert transfer.status_code == 201, transfer.text
    body = transfer.json()
    assert len(body["movements"]) == 2

    levels = c.get(f"/api/v1/products/{product['id']}/stock-levels").json()
    by_branch = {lv["branch_id"]: lv["quantity_on_hand"] for lv in levels}
    assert by_branch[origin] == "6.000"
    assert by_branch[destination] == "4.000"


def test_inventario_completo_via_api(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = _branch_id(org_a_actor)
    product = c.post("/api/v1/products", json={"name": "Produto"}).json()
    c.post(
        "/api/v1/stock-movements",
        json={"product_id": product["id"], "branch_id": branch_id, "direction": "in", "reason": "purchase", "quantity": "10"},
    )

    opened = c.post("/api/v1/inventory-counts", json={"branch_id": branch_id})
    assert opened.status_code == 201, opened.text
    count = opened.json()
    assert count["status"] == "open"
    item = next(i for i in count["items"] if i["product_id"] == product["id"])
    assert item["system_quantity"] == "10.000"

    # tenta fechar sem contar tudo -> 422
    premature_close = c.post(f"/api/v1/inventory-counts/{count['id']}/close")
    assert premature_close.status_code == 422

    counted = c.put(
        f"/api/v1/inventory-counts/{count['id']}/items/{product['id']}", json={"counted_quantity": "8"}
    )
    assert counted.status_code == 200

    closed = c.post(f"/api/v1/inventory-counts/{count['id']}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    levels = c.get(f"/api/v1/products/{product['id']}/stock-levels").json()
    assert levels[0]["quantity_on_hand"] == "8.000"


def test_isolamento_multi_tenant_produtos(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    created = c_a.post("/api/v1/products", json={"name": "Produto da Org A"}).json()

    c_b = client_as(org_b_actor)
    resp = c_b.get(f"/api/v1/products/{created['id']}")
    assert resp.status_code == 404

    listed_b = c_b.get("/api/v1/products")
    assert all(p["id"] != created["id"] for p in listed_b.json())
