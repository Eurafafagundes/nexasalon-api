"""Testes de `POST /orders/{id}/reopen` e do fluxo completo `CLOSED ->
reopen -> OPEN -> cancel -> CANCELLED` (rodada "Reabertura e
Cancelamento de Comandas"). Cobre RBAC (`orders.reopen` separada de
`orders.cancel`), reversão de pagamento/estoque/comissão, as duas
travas financeiras (caixa fechado / comissão já liquidada), cascata
Order -> Appointment no cancelamento, idempotência e isolamento de
tenant."""
import uuid
from decimal import Decimal

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User

_START_A = "2026-08-13T14:00:00-03:00"  # quinta-feira


def _restricted(base_actor: ActorContext, *, permissions: set[str], role_name: str = "Restrito") -> ActorContext:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name=f"Usuário {role_name}")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name=role_name, permissions=frozenset(permissions),
    )


_FULL_REOPEN = {
    "orders.view", "orders.manage", "orders.edit_price", "payments.register", "orders.cancel", "orders.reopen",
}
_NO_REOPEN = {"orders.view", "orders.manage", "orders.edit_price", "payments.register", "orders.cancel"}
# Mesmo conjunto que RECEPTIONIST tem de fábrica pra Comandas (migration
# 0007/0042/0025) — MENOS `orders.reopen` (migration 0045 não concede a
# RECEPTIONIST de propósito).
_RECEPTIONIST_LIKE = _NO_REOPEN


def _setup_finished_appointment(c, *, price="150.00"):
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": price}
    ).json()
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/services", json={"items": [{"service_id": service["id"]}]}
    ).status_code == 200
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
    ).status_code == 200
    client = c.post("/api/v1/clients", json={"name": "Cliente Um"}).json()

    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START_A}],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target}).status_code == 200
    return appt, branch, professional, service, client


def _close_with_payment(c, appt, *, register=None, price="150.00"):
    if register is None:
        register = c.post(
            "/api/v1/cash-registers", json={"branch_id": appt["branch_id"], "initial_amount": "0"}
        ).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": price, "cash_register_id": register["id"]}]},
    )
    assert closed.status_code == 200, closed.text
    return closed.json(), register


# ---------------------------------------------------------------------
# Fluxo feliz: CLOSED -> reopen -> OPEN
# ---------------------------------------------------------------------


def test_reopen_volta_comanda_para_open_e_reverte_pagamento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, register = _close_with_payment(c, appt)
    payment_id = order["payments"][0]["id"]

    resp = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Cliente pediu para trocar o pagamento"})
    assert resp.status_code == 200, resp.text
    reopened = resp.json()
    assert reopened["status"] == "open"
    assert reopened["closed_at"] is None
    assert reopened["closed_by"] is None

    # Pagamento original continua na lista (histórico nunca some), mas
    # marcado como revertido — nunca apagado/editado além do marcador.
    payment = next(p for p in reopened["payments"] if p["id"] == payment_id)
    assert payment["reversed_at"] is not None
    assert payment["amount"] == "150.00"

    # Appointment volta pra `finished` (nunca continua `paid` com a
    # Order `open` de novo).
    appt_after = c.get(f"/api/v1/appointments/{appt['id']}").json()
    assert appt_after["status"] == "finished"

    # Comanda reaberta se comporta como qualquer OPEN — dá pra editar/
    # fechar de novo normalmente.
    assert c.patch(
        f"/api/v1/orders/{reopened['id']}/items/{reopened['items'][0]['id']}", json={"price": "160.00"}
    ).status_code == 200


def test_reopen_reverte_baixa_de_estoque_de_produto_vendido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, *_ = _setup_finished_appointment(c)
    product = c.post("/api/v1/products", json={"name": "Shampoo", "cost_price": "10.00", "sale_price": "50.00"}).json()
    c.post(
        "/api/v1/stock-movements",
        json={"product_id": product["id"], "branch_id": branch["id"], "direction": "in", "reason": "purchase", "quantity": "10"},
    )
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(f"/api/v1/orders/{order['id']}/products", json={"product_id": product["id"], "quantity": "2"})

    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "250.00", "cash_register_id": register["id"]}]},
    ).json()
    product_item = closed["product_items"][0]
    assert product_item["stock_movement_id"] is not None

    levels_before = c.get(f"/api/v1/products/{product['id']}/stock-levels").json()
    qty_before = next(lv["quantity_on_hand"] for lv in levels_before if lv["branch_id"] == branch["id"])

    reopened = c.post(f"/api/v1/orders/{closed['id']}/reopen", json={"reason": "Corrigir produto vendido"}).json()
    reopened_item = reopened["product_items"][0]
    # `stock_movement_id` volta pra NULL — comanda reaberta se comporta
    # como qualquer OPEN de verdade (permite remover/editar a linha).
    assert reopened_item["stock_movement_id"] is None

    levels_after = c.get(f"/api/v1/products/{product['id']}/stock-levels").json()
    qty_after = next(lv["quantity_on_hand"] for lv in levels_after if lv["branch_id"] == branch["id"])
    assert float(qty_after) == float(qty_before) + 2  # devolveu ao estoque, nunca editou o movimento original

    # Remover a linha de produto agora funciona normalmente — só é
    # possível numa comanda `OPEN` SEM `stock_movement_id` (mesma trava
    # que já existia pra qualquer comanda aberta).
    assert c.delete(f"/api/v1/orders/{reopened['id']}/products/{reopened_item['id']}").status_code == 200


# ---------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------


def test_reopen_exige_permissao_orders_reopen(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, _register = _close_with_payment(c, appt)

    restricted = client_as(_restricted(org_a_actor, permissions=_NO_REOPEN))
    resp = restricted.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Tentativa sem permissão"})
    assert resp.status_code == 403


def test_receptionist_like_sem_orders_reopen_recebe_403(client_as, org_a_actor):
    """RECEPTIONIST tem `orders.cancel`/`payments.register` de fábrica
    mas NÃO `orders.reopen` (migration 0045, deliberado — reabrir uma
    venda já recebida é decisão administrativa)."""
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, _register = _close_with_payment(c, appt)

    receptionist_like = client_as(_restricted(org_a_actor, permissions=_RECEPTIONIST_LIKE, role_name="RECEPTIONIST"))
    resp = receptionist_like.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Recepção tentando reabrir"})
    assert resp.status_code == 403


def test_reopen_funciona_com_permissao_concedida_a_role_customizado(client_as, org_a_actor):
    """Nenhum nome de role de sistema é exigido — só a permission (mesmo
    espírito de `_restricted_actor` em test_orders_api.py)."""
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, _register = _close_with_payment(c, appt)

    custom = client_as(_restricted(org_a_actor, permissions=_FULL_REOPEN, role_name="Gerente"))
    resp = custom.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Gerente corrigindo comanda"})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------
# Motivo obrigatório
# ---------------------------------------------------------------------


def test_reopen_sem_motivo_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, _register = _close_with_payment(c, appt)

    resp = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": ""})
    assert resp.status_code == 422

    resp2 = c.post(f"/api/v1/orders/{order['id']}/reopen", json={})
    assert resp2.status_code == 422


# ---------------------------------------------------------------------
# Faturamento/taxas deixam de contar
# ---------------------------------------------------------------------


def test_reopen_tira_a_comanda_do_extrato_de_faturamento(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c, price="200.00")
    order, _register = _close_with_payment(c, appt, price="200.00")

    before = c.get("/api/v1/extract").json()
    assert before["revenue_total"] == "200.00"

    c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Corrigir valor"})

    after = c.get("/api/v1/extract").json()
    assert after["revenue_total"] == "0.00"


def test_reopen_caixa_aberto_resumo_deixa_de_contar_pagamento_revertido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c, price="180.00")
    order, register = _close_with_payment(c, appt, price="180.00")

    summary_before = c.get(f"/api/v1/cash-registers/{register['id']}").json()
    assert summary_before["total_revenue"] == "180.00"

    c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Corrigir forma de pagamento"})

    summary_after = c.get(f"/api/v1/cash-registers/{register['id']}").json()
    assert summary_after["total_revenue"] == "0.00"
    # O pagamento continua aparecendo na lista do caixa (histórico
    # consultável), só não soma mais.
    assert len(summary_after["payments"]) == 1
    assert summary_after["payments"][0]["reversed_at"] is not None


# ---------------------------------------------------------------------
# Dashboard — refechamento com outra forma de pagamento não duplica
# Recebido/Formas de pagamento/Comissão (bug real: `received_stmt`,
# `_payment_methods` e `_revenue_fee_summary` em `services/dashboard.py`
# somavam `Payment.amount` só filtrando `Order.status == CLOSED`, sem
# excluir `reversed_at IS NOT NULL` — inofensivo enquanto a comanda
# ficava `OPEN`, mas o Payment revertido antigo voltava a ser somado
# junto do novo assim que a comanda era refechada).
# ---------------------------------------------------------------------

# Intervalo largo o bastante pra cobrir "agora" (`Order.closed_at` é
# `datetime.now()` no momento do fechamento, nunca a data do
# agendamento) independente de quando o teste roda de verdade — mesmo
# padrão de `test_reopen_bloqueado_quando_comissao_ja_liquidada`.
# `date_from` começa DEPOIS de `_START_A` (2026-08-13) de propósito:
# limitação conhecida e pré-existente do Postgres embarcado do
# `pgserver` usado nos testes (tzdata incompleto — `timezone(...)`
# lança "America/Sao_Paulo not recognized" quando o heatmap do
# Dashboard processa algum `Appointment.starts_at`; nunca acontece com
# `Order.closed_at`, que nunca passa por `timezone()`). Excluir a data
# do agendamento do período consultado evita o heatmap avaliar aquela
# linha, sem esconder nenhum problema do fechamento/reabertura em si.
_WIDE_RANGE = {"date_from": "2026-08-14T00:00:00-03:00", "date_to": "2030-12-31T23:59:59-03:00"}


def test_reopen_refechar_com_outra_forma_de_pagamento_nao_duplica_recebido_nem_comissao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "130.00"}
    ).json()
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/services",
        json={"items": [{"service_id": service["id"], "commission_type": "percentage", "commission_value": "20"}]},
    ).status_code == 200
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
    ).status_code == 200
    client = c.post("/api/v1/clients", json={"name": "Cliente Um"}).json()
    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START_A}],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target}).status_code == 200

    # ESTADO 1 — fechada com Pix R$130, comissão R$26 (20% de 130).
    order, register = _close_with_payment(c, appt, price="130.00")
    pix_payment_id = order["payments"][0]["id"]

    before = c.get("/api/v1/dashboard/overview", params=_WIDE_RANGE).json()
    assert Decimal(before["kpis"]["revenue"]["value"]) == Decimal("130.00")
    assert Decimal(before["financial_summary"]["received"]) == Decimal("130.00")
    assert Decimal(before["financial_summary"]["commissions_calculated"]) == Decimal("26.00")
    pix_before = next(r for r in before["payment_methods"] if r["bucket"] == "pix")
    assert Decimal(pix_before["amount"]) == Decimal("130.00")

    # ESTADO 2 — reabre: Pix antigo fica revertido, comanda volta OPEN.
    reopened = c.post(
        f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Cliente pediu para trocar para Crédito"}
    ).json()
    assert reopened["status"] == "open"
    reopened_payment = next(p for p in reopened["payments"] if p["id"] == pix_payment_id)
    assert reopened_payment["reversed_at"] is not None
    assert reopened_payment["amount"] == "130.00"  # histórico preservado, nunca apagado/editado.

    # Enquanto OPEN: não entra em Recebido, Faturamento, comissão devida
    # nem Formas de pagamento — a comanda inteira some do período.
    during = c.get("/api/v1/dashboard/overview", params=_WIDE_RANGE).json()
    assert Decimal(during["kpis"]["revenue"]["value"]) == Decimal("0")
    assert Decimal(during["financial_summary"]["received"]) == Decimal("0")
    assert Decimal(during["financial_summary"]["commissions_calculated"]) == Decimal("0")
    assert not any(r["bucket"] == "pix" and Decimal(r["amount"]) > 0 for r in during["payment_methods"])

    # ESTADO 3 — refecha com Crédito R$130 (mesmo caixa, ainda aberto).
    reclosed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={
            "payments": [
                {"method": "credit", "amount": "130.00", "cash_register_id": register["id"], "card_brand": "visa", "installments": 1}
            ]
        },
    )
    assert reclosed.status_code == 200, reclosed.text
    reclosed_body = reclosed.json()
    credit_payment_id = next(p["id"] for p in reclosed_body["payments"] if p["method"] == "credit")

    after = c.get("/api/v1/dashboard/overview", params=_WIDE_RANGE).json()
    # Faturamento continua R$130 (nunca 260 — nunca reduzido também).
    assert Decimal(after["kpis"]["revenue"]["value"]) == Decimal("130.00")
    # Recebido = R$130, nunca R$260 (Pix revertido não soma de novo).
    assert Decimal(after["financial_summary"]["received"]) == Decimal("130.00")
    # Comissão válida = R$26, existe uma única vez (nunca 52 = 26+26).
    assert Decimal(after["financial_summary"]["commissions_calculated"]) == Decimal("26.00")

    buckets = {r["bucket"]: Decimal(r["amount"]) for r in after["payment_methods"]}
    assert buckets.get("pix", Decimal("0")) == Decimal("0")  # Pix válido = R$0.
    assert buckets["credit"] == Decimal("130.00")  # Crédito válido = R$130.

    # Taxas de pagamento — considera só o pagamento novo, não revertido
    # (Pix sem regra é NOT_APPLICABLE, nunca contribui; Crédito sem
    # regra cadastrada é UNCONFIGURED — R$130 uma única vez, nunca 260).
    fee_summary = after["revenue_fee_summary"]
    assert Decimal(fee_summary["gross_revenue"]) == Decimal("130.00")
    assert Decimal(fee_summary["known_fee_total"]) == Decimal("0")
    assert Decimal(fee_summary["unconfigured_card_amount"]) == Decimal("130.00")
    assert fee_summary["has_unconfigured_fee"] is True

    # O Pix antigo permanece só como histórico/reversão — nunca some da
    # comanda, nunca reeditado além do marcador de reversão.
    final_order = c.get(f"/api/v1/orders/{order['id']}").json()
    assert final_order["status"] == "closed"
    final_pix = next(p for p in final_order["payments"] if p["id"] == pix_payment_id)
    assert final_pix["reversed_at"] is not None
    assert final_pix["amount"] == "130.00"
    final_credit = next(p for p in final_order["payments"] if p["id"] == credit_payment_id)
    assert final_credit["reversed_at"] is None
    assert final_credit["amount"] == "130.00"


# ---------------------------------------------------------------------
# Trava 1 — caixa relacionado já fechado
# ---------------------------------------------------------------------


def test_reopen_bloqueado_quando_caixa_relacionado_ja_fechado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, register = _close_with_payment(c, appt)

    closed_register = c.post(f"/api/v1/cash-registers/{register['id']}/close", json={"counted_amount": "150.00"})
    assert closed_register.status_code == 200, closed_register.text

    resp = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Tentando reabrir com caixa fechado"})
    assert resp.status_code == 422, resp.text
    assert "caixa" in resp.json()["error"]["message"].lower()

    # Nada foi revertido — nem a Order, nem o Payment.
    order_after = c.get(f"/api/v1/orders/{order['id']}").json()
    assert order_after["status"] == "closed"
    assert order_after["payments"][0]["reversed_at"] is None


def test_reopen_funciona_normalmente_com_caixa_ainda_aberto(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, register = _close_with_payment(c, appt)

    # Caixa continua ABERTO (nunca fechado neste teste) — reabertura
    # precisa funcionar sem exigir nenhum fluxo administrativo extra.
    resp = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Caixa ainda aberto, correção simples"})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------
# Trava 2 — comissão já liquidada
# ---------------------------------------------------------------------


def test_reopen_bloqueado_quando_comissao_ja_liquidada(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "150.00"}
    ).json()
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/services",
        json={"items": [{"service_id": service["id"], "commission_type": "percentage", "commission_value": "10"}]},
    ).status_code == 200
    assert c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
    ).status_code == 200
    client = c.post("/api/v1/clients", json={"name": "Cliente Um"}).json()
    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START_A}],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target}).status_code == 200

    order, _register = _close_with_payment(c, appt)

    settlement = c.post(
        "/api/v1/commissions/settlements",
        json={
            "professional_id": professional["id"],
            # Competência = `Order.closed_at` (ver `order_item_repo.py::
            # lock_pending_commission_items`), ou seja, o instante em que
            # o teste roda `_close_with_payment` acima — nunca a data do
            # agendamento (`_START_A`). Intervalo bem largo pra cobrir
            # "agora" sem depender da data real de execução do teste.
            "date_from": "2020-01-01T00:00:00-03:00", "date_to": "2030-12-31T23:59:59-03:00",
            "adjustment_ids": [],
        },
    )
    assert settlement.status_code == 201, settlement.text

    resp = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Tentando reabrir comissão já paga"})
    assert resp.status_code == 422, resp.text
    assert "comiss" in resp.json()["error"]["message"].lower()

    order_after = c.get(f"/api/v1/orders/{order['id']}").json()
    assert order_after["status"] == "closed"


# ---------------------------------------------------------------------
# OPEN -> CANCELLED (cascata pro Appointment)
# ---------------------------------------------------------------------


def test_cancel_order_aberta_cancela_appointment_vinculado_e_libera_horario(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, branch, professional, service, client = _setup_finished_appointment(c)
    c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"})
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    resp = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "Comanda criada por engano"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    appt_after = c.get(f"/api/v1/appointments/{appt['id']}").json()
    assert appt_after["status"] == "cancelled"

    # Horário livre de novo — um novo agendamento no MESMO profissional/
    # horário precisa funcionar sem conflito.
    new_appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START_A}],
        },
    )
    assert new_appt.status_code == 201, new_appt.text

    # Some da Agenda operacional (backend devolve tudo — filtro é
    # client-side, ver agenda-grid.tsx — mas o status precisa estar
    # correto pra esse filtro funcionar).
    agenda = c.get("/api/v1/agenda", params={"date": "2026-08-13", "branch_id": branch["id"]}).json()
    cancelled_item = next(i for i in agenda if i["appointment_id"] == appt["id"])
    assert cancelled_item["status"] == "cancelled"


def test_cancel_order_sem_appointment_vinculado_nao_gera_erro():
    """Sanidade: `cancel_appointment_for_order_cancel` nunca falha por
    causa do Appointment — coberto indiretamente pelo teste acima (toda
    Order sempre tem `appointment_id` obrigatório no schema atual), mas
    o comportamento defensivo (`appointment is None: return`) existe
    pra robustez caso isso mude no futuro. Sem asserção de HTTP aqui —
    é só documentação viva da função, o teste de verdade é o de cima."""


def test_cancelled_nao_pode_ser_fechada_nem_editada(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    register = c.post("/api/v1/cash-registers", json={"branch_id": appt["branch_id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "Engano"})

    close_resp = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "150.00", "cash_register_id": register["id"]}]},
    )
    assert close_resp.status_code == 409

    edit_resp = c.patch(f"/api/v1/orders/{order['id']}/items/{order['items'][0]['id']}", json={"price": "1.00"})
    assert edit_resp.status_code in (404, 422, 409)


def test_cancelar_comanda_ja_fechada_e_recusado_precisa_reabrir_primeiro(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, _register = _close_with_payment(c, appt)

    resp = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "Tentando cancelar direto"})
    assert resp.status_code == 409

    # Depois de reabrir, cancelar já funciona normalmente (fluxo
    # completo CLOSED -> reopen -> OPEN -> cancel -> CANCELLED).
    reopened = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Preciso cancelar de vez"}).json()
    assert reopened["status"] == "open"
    cancelled = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "Cancelando após reabrir"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


# ---------------------------------------------------------------------
# Idempotência / dupla reabertura / duplo cancelamento
# ---------------------------------------------------------------------


def test_segunda_reabertura_nao_duplica_efeitos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    order, _register = _close_with_payment(c, appt)

    first = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Primeira reabertura"})
    assert first.status_code == 200, first.text

    second = c.post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Segunda tentativa"})
    assert second.status_code == 409, second.text


def test_duplo_cancelamento_nao_duplica_efeitos(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c)
    c.post("/api/v1/cash-registers", json={"branch_id": appt["branch_id"], "initial_amount": "0"})
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    first = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "Primeiro cancelamento"})
    assert first.status_code == 200, first.text

    second = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "Segunda tentativa"})
    assert second.status_code == 409, second.text

    # Appointment continua CANCELLED (não gerou um segundo evento/erro).
    appt_after = c.get(f"/api/v1/appointments/{appt['id']}").json()
    assert appt_after["status"] == "cancelled"


# ---------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------


def test_isolamento_multi_tenant_no_reopen(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    appt, *_ = _setup_finished_appointment(c_a)
    order, _register = _close_with_payment(c_a, appt)

    # org_b_actor tem `orders.reopen` na PRÓPRIA organização (Master),
    # mas isso nunca importa aqui — isolamento de tenant bloqueia antes.
    resp = client_as(org_b_actor).post(f"/api/v1/orders/{order['id']}/reopen", json={"reason": "Tentando de outra org"})
    assert resp.status_code == 404

    order_after = client_as(org_a_actor).get(f"/api/v1/orders/{order['id']}").json()
    assert order_after["status"] == "closed"
