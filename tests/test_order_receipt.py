"""Testes de `GET /api/v1/orders/{id}/receipt` — Comprovante de
Atendimento (Etapa D). Cobre o checklist obrigatório do pedido: comanda
com somente serviços; comanda com serviços + produtos; pagamento único;
pagamento misto; preços históricos/snapshot (catálogo muda depois, o
comprovante de uma venda antiga não muda); comprovante sem campos
opcionais (estabelecimento sem CNPJ/logo, pagamento sem bandeira);
comprovante nunca contém observações internas do cliente; só comandas
FECHADAS emitem comprovante; reusa `orders.view` (sem permission nova)."""
import dataclasses
import uuid

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User
from sqlalchemy import text

_START_A = "2026-08-13T14:00:00-03:00"  # quinta-feira


def _restricted(actor: ActorContext, *, permissions) -> ActorContext:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Restrito")
        session.add(user)
        session.commit()
        user_id = user.id
    return dataclasses.replace(
        actor, user_id=user_id, membership_id=uuid.uuid4(), role_id=uuid.uuid4(),
        role_name="Restrito", permissions=frozenset(permissions),
    )


def _setup_finished_appointment(c, *, client_name="Cliente Um", client_notes=None):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "310.00"}
    ).json()
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/services", json={"items": [{"service_id": service["id"]}]}
    ).status_code == 200
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
    ).status_code == 200
    client_payload = {"name": client_name}
    if client_notes is not None:
        client_payload["notes"] = client_notes
    client = c.post("/api/v1/clients", json=client_payload).json()

    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START_A}],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target}).status_code == 200
    return appt, branch, service, client


def _stock_product(c, branch_id, *, sale_price="120.00", quantity="10"):
    product = c.post(
        "/api/v1/products", json={"name": "Shampoo", "cost_price": "40.00", "sale_price": sale_price}
    ).json()
    resp = c.post(
        "/api/v1/stock-movements",
        json={"product_id": product["id"], "branch_id": branch_id, "direction": "in", "reason": "purchase", "quantity": quantity},
    )
    assert resp.status_code == 201, resp.text
    return product


def test_comprovante_indisponivel_para_comanda_aberta(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, _service, _client = _setup_finished_appointment(c)
    c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"})
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    resp = c.get(f"/api/v1/orders/{order['id']}/receipt")
    assert resp.status_code == 422


def test_comprovante_comanda_somente_servicos_pagamento_unico(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, _service, client = _setup_finished_appointment(c)
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "310.00", "cash_register_id": register["id"]}]},
    )
    assert closed.status_code == 200, closed.text

    resp = c.get(f"/api/v1/orders/{order['id']}/receipt")
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    assert receipt["client"]["name"] == client["name"]
    assert len(receipt["items"]) == 1
    assert receipt["items"][0]["kind"] == "service"
    assert receipt["items"][0]["unit_price"] == "310.00"
    assert receipt["total"] == "310.00"
    assert len(receipt["payments"]) == 1
    assert receipt["payments"][0]["method"] == "pix"
    assert receipt["payments"][0]["amount"] == "310.00"


def test_comprovante_comanda_com_servicos_e_produtos_pagamento_misto(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, _service, _client = _setup_finished_appointment(c)
    product = _stock_product(c, branch["id"], sale_price="120.00")
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    added = c.post(f"/api/v1/orders/{order['id']}/products", json={"product_id": product["id"], "quantity": "1"})
    assert added.status_code == 201, added.text
    assert added.json()["total"] == "430.00000"  # 310 serviço + 120 produto (quantity é Numeric(12,3))

    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={
            "payments": [
                {"method": "pix", "amount": "200.00", "cash_register_id": register["id"]},
                {"method": "credit", "amount": "230.00", "cash_register_id": register["id"], "card_brand": "visa"},
            ]
        },
    )
    assert closed.status_code == 200, closed.text

    receipt = c.get(f"/api/v1/orders/{order['id']}/receipt").json()
    kinds = {i["kind"] for i in receipt["items"]}
    assert kinds == {"service", "product"}
    product_item = next(i for i in receipt["items"] if i["kind"] == "product")
    assert product_item["unit_price"] == "120.00"
    assert product_item["professional_name"] is None
    assert receipt["total"] == "430.00000"
    assert len(receipt["payments"]) == 2
    methods = {p["method"] for p in receipt["payments"]}
    assert methods == {"pix", "credit"}
    credit_payment = next(p for p in receipt["payments"] if p["method"] == "credit")
    assert credit_payment["card_brand"] == "visa"


def test_comprovante_usa_snapshot_nunca_preco_atual_do_catalogo(client_as, org_a_actor):
    """Regra financeira explícita do pedido: se o catálogo mudar depois
    do fechamento, um comprovante antigo continua mostrando os valores
    originais da venda."""
    c = client_as(org_a_actor)
    appt, branch, service, _client = _setup_finished_appointment(c)
    product = _stock_product(c, branch["id"], sale_price="120.00")
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(f"/api/v1/orders/{order['id']}/products", json={"product_id": product["id"], "quantity": "1"})
    c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "430.00", "cash_register_id": register["id"]}]},
    )

    # Catálogo muda DEPOIS do fechamento — PUT é full-replace nas duas
    # rotas, então reenvia nome/duração já existentes junto do novo preço.
    resp_service = c.put(
        f"/api/v1/services/{service['id']}",
        json={
            "name": service["name"],
            "default_duration_minutes": service["default_duration_minutes"],
            "default_price": "999.00",
        },
    )
    assert resp_service.status_code == 200, resp_service.text
    resp_product = c.put(
        f"/api/v1/products/{product['id']}", json={"name": product["name"], "sale_price": "1.00"}
    )
    assert resp_product.status_code == 200, resp_product.text

    receipt = c.get(f"/api/v1/orders/{order['id']}/receipt").json()
    service_item = next(i for i in receipt["items"] if i["kind"] == "service")
    product_item = next(i for i in receipt["items"] if i["kind"] == "product")
    assert service_item["unit_price"] == "310.00"
    assert product_item["unit_price"] == "120.00"
    assert receipt["total"] == "430.00000"


def test_comprovante_sem_campos_opcionais_do_estabelecimento(client_as, org_a_actor):
    """Estabelecimento sem CNPJ/logo/endereço (default de uma org nova)
    ainda emite comprovante normalmente."""
    c = client_as(org_a_actor)
    appt, branch, _service, _client = _setup_finished_appointment(c)
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "310.00", "cash_register_id": register["id"]}]},
    )

    receipt = c.get(f"/api/v1/orders/{order['id']}/receipt").json()
    assert receipt["establishment"]["document"] is None
    assert receipt["establishment"]["logo_url"] is None
    assert receipt["establishment"]["city"] is None
    assert receipt["payments"][0]["card_brand"] is None
    assert receipt["payments"][0]["installments"] is None


def test_comprovante_nunca_contem_observacoes_internas_do_cliente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, _service, _client = _setup_finished_appointment(
        c, client_notes="Alérgica a amônia — NUNCA usar produto X."
    )
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "310.00", "cash_register_id": register["id"]}]},
    )

    resp = c.get(f"/api/v1/orders/{order['id']}/receipt")
    assert resp.status_code == 200
    assert "notes" not in resp.json()["client"]
    assert "Alérgica" not in resp.text
    assert "amônia" not in resp.text


def test_comprovante_exige_orders_view(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, _service, _client = _setup_finished_appointment(c)
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "310.00", "cash_register_id": register["id"]}]},
    )

    restricted = _restricted(org_a_actor, permissions=set())
    resp = client_as(restricted).get(f"/api/v1/orders/{order['id']}/receipt")
    assert resp.status_code == 403


def test_comprovante_isolamento_multi_tenant(client_as, org_a_actor, org_b_actor):
    c = client_as(org_a_actor)
    appt, branch, _service, _client = _setup_finished_appointment(c)
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "310.00", "cash_register_id": register["id"]}]},
    )

    c_b = client_as(org_b_actor)
    resp = c_b.get(f"/api/v1/orders/{order['id']}/receipt")
    assert resp.status_code == 404
