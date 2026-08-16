"""Testes HTTP de `/api/v1/orders` — fluxo ponta a ponta Agendamento ->
Atendimento -> Comanda -> Pagamento -> Pago, e RBAC das 4 permissions
novas (`orders.view`, `orders.manage`, `orders.edit_price`,
`payments.register`). A regra de negócio já está coberta em
`test_orders.py` (service layer); aqui validamos que a mesma coisa
funciona passando pela API real, e que as permissions são de fato
exigidas por rota."""
import uuid

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User

_START_A = "2026-08-13T14:00:00-03:00"  # quinta-feira


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


def _setup_finished_appointment(c):
    """Cria unidade/profissional/serviço/cliente/agendamento via API e
    avança o status até `finished` (transição hop a hop, igual à UI) —
    mesmo padrão de `test_appointment_routes.py::_setup_agenda`."""
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "150.00"}
    ).json()
    resp = c.put(
        f"/api/v1/professionals/{professional['id']}/services",
        json={"items": [{"service_id": service["id"]}]},
    )
    assert resp.status_code == 200, resp.text
    resp = c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
    )
    assert resp.status_code == 200, resp.text
    client = c.post("/api/v1/clients", json={"name": "Cliente Um"}).json()

    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START_A}],
        },
    ).json()

    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        resp = c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target})
        assert resp.status_code == 200, resp.text

    return appt


def test_fluxo_completo_via_api_comanda_ate_pago(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    register = c.post(
        "/api/v1/cash-registers", json={"branch_id": appt["branch_id"], "initial_amount": "0"}
    ).json()

    created = c.post("/api/v1/orders", json={"appointment_id": appt["id"]})
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["status"] == "open"
    assert order["total"] == "150.00"
    item_id = order["items"][0]["id"]

    edited = c.patch(f"/api/v1/orders/{order['id']}/items/{item_id}", json={"price": "120.00"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["total"] == "120.00"

    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "120.00", "cash_register_id": register["id"]}]},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    appt_after = c.get(f"/api/v1/appointments/{appt['id']}").json()
    assert appt_after["status"] == "paid"

    by_appt = c.get(f"/api/v1/orders/by-appointment/{appt['id']}")
    assert by_appt.status_code == 200
    assert by_appt.json()["id"] == order["id"]

    # item "pagamento entra imediatamente no resumo do caixa"
    register_detail = c.get(f"/api/v1/cash-registers/{register['id']}").json()
    assert register_detail["total_revenue"] == "120.00"
    pix_total = next(t for t in register_detail["totals_by_method"] if t["method"] == "pix")
    assert pix_total["total"] == "120.00"
    assert pix_total["count"] == 1


def test_nao_fecha_comanda_sem_nenhum_caixa_aberto(client_as, org_a_actor):
    """Item 'se não existir nenhum caixa aberto: impedir a confirmação
    do pagamento' — via API, sem nenhum `POST /cash-registers` antes,
    o fechamento é recusado (422, cash_register_id não existe/não está
    aberto — nunca cria um caixa sozinho)."""
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    resp = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": str(uuid.uuid4())}]},
    )
    assert resp.status_code in (404, 422)


def test_permissao_orders_manage_e_exigida_para_abrir_comanda(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view"})
    resp = client_as(restricted).post("/api/v1/orders", json={"appointment_id": appt["id"]})
    assert resp.status_code == 403


def test_permissao_orders_edit_price_e_exigida(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    item_id = order["items"][0]["id"]

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view", "orders.manage"})
    resp = client_as(restricted).patch(f"/api/v1/orders/{order['id']}/items/{item_id}", json={"price": "1.00"})
    assert resp.status_code == 403


def test_permissao_payments_register_e_exigida_para_fechar(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view", "orders.manage"})
    resp = client_as(restricted).post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": str(uuid.uuid4())}]},
    )
    assert resp.status_code == 403


def test_isolamento_multi_tenant_comanda_nao_vaza_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    appt = _setup_finished_appointment(c_a)
    order = c_a.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    c_b = client_as(org_b_actor)
    resp = c_b.get(f"/api/v1/orders/{order['id']}")
    assert resp.status_code == 404
