"""Testes de `GET /clients` (lista enriquecida) e `GET /clients/{id}/profile`
(Ficha 360°) — Etapa J. Cobre: cálculos derivados de Appointment/Order
(último atendimento, próximo agendamento, profissional mais recente,
faltas/cancelamentos), RBAC (`finance.view` esconde valores, `orders.view`
esconde a aba Comandas) e o bugfix de `total_spent` somando produtos.
Mesmo padrão de fixtures de `test_clients.py`/`test_stock_api.py`."""
import uuid
from decimal import Decimal

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User


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


def _setup_branch_professional_service(c):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:8]}"}).json()
    prof = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    svc = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "100.00"}
    ).json()
    c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
    c.put(
        f"/api/v1/professionals/{prof['id']}/working-hours",
        # Todos os dias da semana abertos — os testes usam datas
        # variadas (passado distante, futuro distante) só pra exercitar
        # "próximo agendamento"/"último atendimento", sem se prender ao
        # dia da semana de cada uma.
        json={
            "items": [
                {"weekday": w, "start_time": "00:00:00", "end_time": "23:59:00"} for w in range(7)
            ]
        },
    )
    return branch, prof, svc


def _create_appointment(c, branch, prof, svc, client_id, start_at):
    return c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"],
            "client_id": client_id,
            "items": [{"professional_id": prof["id"], "service_id": svc["id"], "start_at": start_at}],
        },
    ).json()


def _close_appointment_as_order(c, branch, appt):
    """Passa o agendamento por `finished`, abre e fecha a comanda —
    devolve a comanda fechada."""
    for target in ("confirmed", "finished"):
        c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target})
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    ).json()
    return closed


# ---------------------------------------------------------------------
# Lista de clientes — campos derivados (Etapa J)
# ---------------------------------------------------------------------


def test_lista_traz_ultimo_atendimento_profissional_e_proximo_agendamento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Ana Souza"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)

    past = _create_appointment(c, branch, prof, svc, client_id, "2020-01-10T09:00:00-03:00")
    _close_appointment_as_order(c, branch, past)

    future = _create_appointment(c, branch, prof, svc, client_id, "2099-01-10T09:00:00-03:00")

    entry = next(cl for cl in c.get("/api/v1/clients").json() if cl["id"] == client_id)
    assert entry["last_visit_at"] is not None
    assert entry["last_professional_name"] == "Ianka"
    assert entry["next_appointment_at"] is not None
    assert entry["next_appointment_at"].startswith("2099-01-10")
    assert entry["has_no_show"] is False
    assert future["status"] == "scheduled"


def test_lista_indica_falta_quando_ha_no_show(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Faltante"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id, "2026-08-13T09:00:00-03:00")
    resp = c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": "no_show"})
    assert resp.status_code == 200, resp.text

    entry = next(cl for cl in c.get("/api/v1/clients").json() if cl["id"] == client_id)
    assert entry["has_no_show"] is True
    # NO_SHOW não é status "próximo agendamento" (já resolvido).
    assert entry["next_appointment_at"] is None


def test_lista_sem_nenhum_atendimento_traz_campos_none(client_as, org_a_actor):
    c = client_as(org_a_actor)
    c.post("/api/v1/clients", json={"name": "Cliente Zero"})
    entry = next(cl for cl in c.get("/api/v1/clients").json() if cl["name"] == "Cliente Zero")
    assert entry["last_visit_at"] is None
    assert entry["next_appointment_at"] is None
    assert entry["last_professional_name"] is None
    assert entry["has_no_show"] is False


# ---------------------------------------------------------------------
# Ficha 360° — GET /clients/{id}/profile
# ---------------------------------------------------------------------


def test_profile_resumo_historico_e_comandas(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Beatriz Lima"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)

    # 1 visita fechada.
    finished = _create_appointment(c, branch, prof, svc, client_id, "2020-01-10T09:00:00-03:00")
    _close_appointment_as_order(c, branch, finished)

    # 1 falta.
    no_show = _create_appointment(c, branch, prof, svc, client_id, "2020-02-10T09:00:00-03:00")
    c.patch(f"/api/v1/appointments/{no_show['id']}/status", json={"status": "no_show"})

    # 1 cancelamento.
    cancelled = _create_appointment(c, branch, prof, svc, client_id, "2020-03-10T09:00:00-03:00")
    c.post(f"/api/v1/appointments/{cancelled['id']}/cancel")

    # 1 próximo agendamento futuro.
    future = _create_appointment(c, branch, prof, svc, client_id, "2099-05-10T09:00:00-03:00")

    profile = c.get(f"/api/v1/clients/{client_id}/profile")
    assert profile.status_code == 200, profile.text
    body = profile.json()

    assert body["visits_count"] == 1
    assert body["total_spent"] == "100.00"
    assert body["no_show_count"] == 1
    assert body["cancelled_count"] == 1
    assert body["next_appointment"]["id"] == future["id"]
    assert body["last_visit_at"] is not None
    assert body["can_view_finance"] is True
    assert body["can_view_orders"] is True

    # Histórico: timeline com os 4 agendamentos (qualquer status).
    timeline_ids = {a["id"] for a in body["timeline"]}
    assert timeline_ids == {finished["id"], no_show["id"], cancelled["id"], future["id"]}

    # Comandas: só a 1 comanda fechada (os outros 3 agendamentos nunca
    # tiveram comanda aberta).
    assert len(body["orders"]) == 1
    order = body["orders"][0]
    assert order["status"] == "closed"
    assert order["total"] == "100.00"
    assert order["service_names"] == ["Corte"]
    assert [p["method"] for p in order["payments"]] == ["pix"]
    assert order["payments"][0]["amount"] == "100.00"


def test_profile_esconde_valores_financeiros_sem_finance_view(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Carla Dias"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    finished = _create_appointment(c, branch, prof, svc, client_id, "2020-01-10T09:00:00-03:00")
    _close_appointment_as_order(c, branch, finished)

    restricted = _restricted_actor(org_a_actor, permissions={"clients.view", "orders.view"})
    c_restricted = client_as(restricted)

    profile = c_restricted.get(f"/api/v1/clients/{client_id}/profile")
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["can_view_finance"] is False
    assert body["total_spent"] is None
    assert len(body["orders"]) == 1
    assert body["orders"][0]["total"] is None
    assert body["orders"][0]["payments"][0]["amount"] is None
    # Dado não-financeiro continua visível mesmo sem `finance.view`.
    assert body["orders"][0]["service_names"] == ["Corte"]


def test_profile_esconde_comandas_sem_orders_view(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Debora Alves"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    finished = _create_appointment(c, branch, prof, svc, client_id, "2020-01-10T09:00:00-03:00")
    _close_appointment_as_order(c, branch, finished)

    restricted = _restricted_actor(org_a_actor, permissions={"clients.view", "finance.view"})
    c_restricted = client_as(restricted)

    profile = c_restricted.get(f"/api/v1/clients/{client_id}/profile")
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["can_view_orders"] is False
    assert body["orders"] == []
    # Resumo/Histórico continuam disponíveis — só a aba Comandas é gated.
    assert body["visits_count"] == 1


def test_profile_sem_clients_view_e_negado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Elisa Prado"}).json()["id"]

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view", "finance.view"})
    c_restricted = client_as(restricted)
    resp = c_restricted.get(f"/api/v1/clients/{client_id}/profile")
    assert resp.status_code == 403


# ---------------------------------------------------------------------
# Bugfix: `total_spent` (histórico e ficha) soma serviço + produto
# ---------------------------------------------------------------------


def test_total_gasto_soma_produtos_alem_de_servicos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Fernanda Reis"}).json()["id"]
    branch, prof, svc = _setup_branch_professional_service(c)
    appt = _create_appointment(c, branch, prof, svc, client_id, "2020-01-10T09:00:00-03:00")
    c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": "finished"})

    product = c.post(
        "/api/v1/products", json={"name": "Shampoo", "cost_price": "10.00", "sale_price": "30.00"}
    ).json()
    c.post(
        "/api/v1/stock-movements",
        json={
            "product_id": product["id"],
            "branch_id": branch["id"],
            "direction": "in",
            "reason": "purchase",
            "quantity": "5",
        },
    )

    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(f"/api/v1/orders/{order['id']}/products", json={"product_id": product["id"], "quantity": "1"})
    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "cash", "amount": "130.00", "cash_register_id": register["id"]}]},
    ).json()
    assert Decimal(closed["total"]) == Decimal("130.00")  # 100 serviço + 30 produto

    history = c.get(f"/api/v1/clients/{client_id}/history").json()
    assert Decimal(history["total_spent"]) == Decimal("130.00")

    profile = c.get(f"/api/v1/clients/{client_id}/profile").json()
    assert Decimal(profile["total_spent"]) == Decimal("130.00")
