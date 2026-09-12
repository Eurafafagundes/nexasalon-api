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


def _restricted_actor(base_actor: ActorContext, *, permissions, role_name: str = "Restrito") -> ActorContext:
    """`role_name` opcional — só pra dar um nome legível ao ator nos
    testes (autorização de Comandas é 100% por PERMISSION, não checa
    `role_name` — ver `orders.py::_register_payment`). Útil pra deixar
    claro que "Finalizar comandas e pagamentos" funciona mesmo com um
    role qualquer, nome nenhum de sistema é exigido."""
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


def _setup_finished_appointment(c, *, client_id=None, start_at=_START_A):
    """Cria unidade/profissional/serviço/cliente/agendamento via API e
    avança o status até `finished` (transição hop a hop, igual à UI) —
    mesmo padrão de `test_appointment_routes.py::_setup_agenda`.
    `client_id` opcional reaproveita uma cliente já existente (em vez de
    criar uma nova) — necessário pra montar o cenário de fechamento
    CONSOLIDADO (`close-consolidated` exige mesma cliente, mesmo dia)."""
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
    if client_id is None:
        client_id = c.post("/api/v1/clients", json={"name": "Cliente Um"}).json()["id"]

    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client_id,
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": start_at}],
        },
    ).json()

    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        resp = c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target})
        assert resp.status_code == 200, resp.text

    return appt


def _open_register_for(c, branch_id, *, initial_amount="0"):
    """Etapa H ('exigir caixa aberto para criar Comanda', padrão ON) —
    `POST /orders` passa a exigir um caixa aberto NESTA unidade; a
    maioria dos testes deste arquivo não testa o Caixa em si, só
    precisa de um caixa válido pra `POST /orders` não ser recusado."""
    resp = c.post("/api/v1/cash-registers", json={"branch_id": branch_id, "initial_amount": initial_amount})
    assert resp.status_code == 201, resp.text
    return resp.json()


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
    do pagamento' — o FECHAMENTO é recusado ao referenciar um caixa que
    não existe/não está aberto (422, nunca cria um caixa sozinho). A
    ABERTURA da comanda em si já exige um caixa aberto na unidade
    desde a Etapa H (padrão ON) — `_open_register_for` só satisfaz
    esse pré-requisito; o alvo deste teste continua sendo o
    fechamento com um `cash_register_id` inválido."""
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    _open_register_for(c, appt["branch_id"])
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
    _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    item_id = order["items"][0]["id"]

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view", "orders.manage"})
    resp = client_as(restricted).patch(f"/api/v1/orders/{order['id']}/items/{item_id}", json={"price": "1.00"})
    assert resp.status_code == 403


def test_beneficio_fidelidade_via_api_reduz_valor_a_cobrar_preserva_preco_economico(client_as, org_a_actor):
    """Cenário completo via HTTP real: aplicar Cartão Fidelidade num
    item não altera `price` (valor econômico), reduz `amount_due`
    (valor a cobrar), e a comanda fecha com `payments=[]`."""
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)  # 1 serviço, preço "150.00" (ver _setup_finished_appointment).
    _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    item_id = order["items"][0]["id"]
    assert order["total"] == "150.00"
    assert order["amount_due"] == "150.00"

    applied = c.patch(
        f"/api/v1/orders/{order['id']}/items/{item_id}/benefit",
        json={"benefit_type": "loyalty", "benefit_amount": "150.00"},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["total"] == "150.00"  # valor econômico intocado.
    assert body["amount_due"] == "0.00"  # nada a cobrar.
    assert body["total_benefit_amount"] == "150.00"
    item = body["items"][0]
    assert item["price"] == "150.00"  # NUNCA zerado.
    assert item["benefit_type"] == "loyalty"
    assert item["benefit_amount"] == "150.00"
    assert item["charged_amount"] == "0.00"

    closed = c.post(f"/api/v1/orders/{order['id']}/close", json={"payments": []})
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert closed.json()["payments"] == []

    register_id = c.get(f"/api/v1/orders/{order['id']}").json()  # sanity: comanda continua consultável fechada.
    assert register_id["status"] == "closed"


def test_permissao_orders_edit_price_e_exigida_para_beneficio(client_as, org_a_actor):
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    item_id = order["items"][0]["id"]

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view", "orders.manage"})
    resp = client_as(restricted).patch(
        f"/api/v1/orders/{order['id']}/items/{item_id}/benefit",
        json={"benefit_type": "loyalty", "benefit_amount": "1.00"},
    )
    assert resp.status_code == 403


def test_role_generico_sem_payments_register_e_bloqueado_no_fechamento(client_as, org_a_actor):
    """Checagem básica da dependency `require_permission("payments.
    register")` na rota — `orders.view` sozinho (sem `payments.
    register`) não fecha. Matriz completa dos 3 níveis (view/edit/
    close) está no bloco de testes de "Decisão de produto" mais
    abaixo."""
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view"})
    resp = client_as(restricted).post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": str(uuid.uuid4())}]},
    )
    assert resp.status_code == 403


def test_cancelar_comanda_via_api(client_as, org_a_actor):
    """Etapa F, item 4 — fluxo feliz de `POST /orders/{id}/cancel` pela
    API real: comanda some do fluxo ativo (status vira `cancelled`) e o
    Appointment volta a poder abrir uma comanda nova em seguida."""
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    resp = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "aberta por engano"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    # Tenta cancelar de novo — já não está mais aberta, recusa (não é 200 nem duplica).
    resp_again = c.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "de novo"})
    assert resp_again.status_code in (409, 422)

    # O agendamento não fica travado — dá pra abrir uma comanda nova.
    reopened = c.post("/api/v1/orders", json={"appointment_id": appt["id"]})
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["id"] != order["id"]


def test_permissao_orders_cancel_e_exigida(client_as, org_a_actor):
    """`orders.cancel` é permission NOVA e separada de `orders.manage` —
    quem só abre/gerencia comanda mas não tem `orders.cancel` recebe 403
    ao tentar cancelar."""
    c = client_as(org_a_actor)
    appt = _setup_finished_appointment(c)
    _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    restricted = _restricted_actor(org_a_actor, permissions={"orders.view", "orders.manage"})
    resp = client_as(restricted).post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "teste"})
    assert resp.status_code == 403


def test_isolamento_multi_tenant_comanda_nao_vaza_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    appt = _setup_finished_appointment(c_a)
    _open_register_for(c_a, appt["branch_id"])
    order = c_a.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()

    c_b = client_as(org_b_actor)
    resp = c_b.get(f"/api/v1/orders/{order['id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# Decisão de produto: Comandas em 3 NÍVEIS PERMISSION-based, cada um
# incluindo o anterior — "Visualizar comandas" (`orders.view`),
# "Criar e editar comandas" (+ `orders.manage` + `orders.edit_price`,
# mas SEM poder registrar pagamento) e "Finalizar comandas e
# pagamentos" (+ `payments.register`, reaproveitada — nenhuma
# permission nova). Substitui a regra ROLE-based da rodada anterior
# (`require_role("OWNER", "RECEPTIONIST")`) — reaberta aqui porque o
# pedido real sempre foi RBAC granular: qualquer perfil, inclusive
# customizado, pode receber "Finalizar" explicitamente em Equipe e
# acessos, não só os dois roles de sistema.
# ---------------------------------------------------------------------

_VIEW_LEVEL = {"orders.view"}
_EDIT_LEVEL = {"orders.view", "orders.manage", "orders.edit_price"}
_CLOSE_LEVEL = {"orders.view", "orders.manage", "orders.edit_price", "payments.register"}


def _open_order_ready_to_close(c):
    appt = _setup_finished_appointment(c)
    register = _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    return order, register


def test_master_pode_fechar_comanda_com_pagamento(client_as, org_a_actor):
    """`org_a_actor` (OWNER de fábrica, todas as permissions) sempre
    consegue finalizar — "Master deve possuir automaticamente todas"."""
    c = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(c)

    resp = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"


def test_nivel_1_visualizar_nao_consegue_gerenciar_nem_finalizar(client_as, org_a_actor):
    """Nível 1 (só `orders.view`) — somente leitura: nem editar item,
    nem adicionar produto, nem finalizar."""
    master = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master)
    item_id = order["items"][0]["id"]

    view_only = client_as(_restricted_actor(org_a_actor, permissions=_VIEW_LEVEL))

    assert view_only.get(f"/api/v1/orders/{order['id']}").status_code == 200

    resp_manage = view_only.post(f"/api/v1/orders/{order['id']}/products", json={"product_id": str(uuid.uuid4()), "quantity": "1"})
    assert resp_manage.status_code == 403, resp_manage.text

    resp_edit = view_only.patch(f"/api/v1/orders/{order['id']}/items/{item_id}", json={"price": "1.00"})
    assert resp_edit.status_code == 403, resp_edit.text

    resp_close = view_only.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp_close.status_code == 403, resp_close.text


def test_nivel_2_criar_e_editar_consegue_gerenciar_e_editar_mas_nao_finalizar(client_as, org_a_actor):
    """Nível 2 (`orders.view`+`orders.manage`+`orders.edit_price`) —
    monta a comanda inteira, mas NÃO registra pagamento nem fecha."""
    master = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master)
    item_id = order["items"][0]["id"]

    editor = client_as(_restricted_actor(org_a_actor, permissions=_EDIT_LEVEL))

    resp_edit = editor.patch(f"/api/v1/orders/{order['id']}/items/{item_id}", json={"price": "90.00"})
    assert resp_edit.status_code == 200, resp_edit.text

    resp_close = editor.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp_close.status_code == 403, resp_close.text


def test_nivel_3_finalizar_consegue_tudo_inclusive_fechar_com_pagamento(client_as, org_a_actor):
    """Nível 3 (+ `payments.register`) — o cenário real pedido: um
    perfil CUSTOMIZADO (nome de role qualquer, não precisa ser OWNER
    nem RECEPTIONIST) que recebeu "Finalizar comandas e pagamentos"
    explicitamente consegue finalizar normalmente. Bug real corrigido
    em relação à rodada anterior: o gate voltou a ser por PERMISSION,
    não por nome de role."""
    master = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master)

    closer = client_as(_restricted_actor(org_a_actor, permissions=_CLOSE_LEVEL, role_name="Atendente Sênior"))

    resp = closer.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"


def test_nivel_3_fecha_consolidado_tambem(client_as, org_a_actor):
    """Mesma regra em `close-consolidated`
    (`orders.py::close_orders_consolidated` usa a mesma dependency)."""
    master = client_as(org_a_actor)
    appt1 = _setup_finished_appointment(master)
    client_id = appt1["client_id"]
    appt2 = _setup_finished_appointment(master, client_id=client_id, start_at="2026-08-13T16:00:00-03:00")
    register1 = _open_register_for(master, appt1["branch_id"])
    _open_register_for(master, appt2["branch_id"])  # satisfaz o pré-requisito da unidade de appt2
    order1 = master.post("/api/v1/orders", json={"appointment_id": appt1["id"]}).json()
    order2 = master.post("/api/v1/orders", json={"appointment_id": appt2["id"]}).json()
    total = str(float(order1["total"]) + float(order2["total"]))

    closer = client_as(_restricted_actor(org_a_actor, permissions=_CLOSE_LEVEL))

    resp = closer.post(
        f"/api/v1/orders/{order1['id']}/close-consolidated",
        json={
            "order_ids": [order1["id"], order2["id"]],
            "payments": [{"method": "pix", "amount": total, "cash_register_id": register1["id"]}],
        },
    )
    assert resp.status_code == 200, resp.text
    statuses = {o["id"]: o["status"] for o in resp.json()}
    assert statuses[order1["id"]] == "closed"
    assert statuses[order2["id"]] == "closed"


def test_usuario_sem_nenhuma_permissao_de_comandas_bloqueado(client_as, org_a_actor):
    master = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master)

    sem_acesso = client_as(_restricted_actor(org_a_actor, permissions={"clients.view", "finance.view"}))

    resp = sem_acesso.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 403, resp.text


def test_outro_tenant_nao_fecha_comanda_mesmo_com_nivel_3(client_as, org_a_actor, org_b_actor):
    """Isolamento por Organization é ortogonal à permissão — `org_b_actor`
    tem TODAS as permissions (nível 3 incluso), mas de OUTRA
    organização, e nunca deve conseguir fechar uma comanda que pertence
    à organização de `org_a_actor`."""
    c_a = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(c_a)

    outro_tenant = client_as(org_b_actor)
    resp = outro_tenant.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code in (403, 404), resp.text


def test_orders_manage_sozinho_nao_concede_acesso_ao_modulo_financeiro(client_as, org_a_actor):
    """Nível 2 (`orders.manage`, sem `payments.register`) não abre
    Extrato/Caixa/Taxas — nem sequer a listagem de caixas (que só o
    nível 3 destrava, ver teste seguinte)."""
    editor = client_as(_restricted_actor(org_a_actor, permissions=_EDIT_LEVEL))

    assert editor.get("/api/v1/extract").status_code == 403
    assert editor.get("/api/v1/cash-registers").status_code == 403
    assert editor.get("/api/v1/payment-fee-rules").status_code == 403


def test_payments_register_libera_so_a_listagem_de_caixas_nunca_o_resto_do_financeiro(client_as, org_a_actor):
    """Bug real corrigido nesta rodada: nível 3 (`payments.register`)
    precisa listar os caixas ABERTOS pra escolher qual recebe o
    pagamento (mesma classe de bug já corrigida em
    `orders.py::mark_paid`) — sem abrir NADA além disso do Financeiro:
    nem o detalhe de um caixa (que leva faturamento/totais), nem abrir/
    fechar caixa, nem Extrato, nem Taxas."""
    master = client_as(org_a_actor)
    appt = _setup_finished_appointment(master)
    register = _open_register_for(master, appt["branch_id"])

    closer = client_as(_restricted_actor(org_a_actor, permissions=_CLOSE_LEVEL))

    list_resp = closer.get("/api/v1/cash-registers", params={"status": "open"})
    assert list_resp.status_code == 200, list_resp.text
    assert any(r["id"] == register["id"] for r in list_resp.json())

    detail_resp = closer.get(f"/api/v1/cash-registers/{register['id']}")
    assert detail_resp.status_code == 403, detail_resp.text

    open_resp = closer.post("/api/v1/cash-registers", json={"branch_id": appt["branch_id"], "initial_amount": "0"})
    assert open_resp.status_code == 403, open_resp.text

    close_register_resp = closer.post(
        f"/api/v1/cash-registers/{register['id']}/close", json={"counted_amount": "0"}
    )
    assert close_register_resp.status_code == 403, close_register_resp.text

    assert closer.get("/api/v1/extract").status_code == 403
    assert closer.get("/api/v1/payment-fee-rules").status_code == 403


# ---------------------------------------------------------------------
# Cadastro de cliente durante o fluxo da Comanda — nível 2 ("Criar e
# editar comandas") precisa conseguir pesquisar/cadastrar o cliente
# necessário pra abrir a comanda, sem ganhar o módulo inteiro de
# Clientes. `POST /clients` e `GET /clients/lookup` já aceitavam a
# permissão granular `clients.create`/`clients.lookup` (migration 0030,
# criada originalmente pro mesmo motivo — Agenda), então a solução é
# reaproveitar essas duas chaves, NUNCA `clients.manage`/`clients.view`
# nem acoplar `orders.manage` a Clientes no backend (ver
# `ManageAccessDrawer::COMANDAS_EDIT_KEYS`, que agora inclui as duas).
# ---------------------------------------------------------------------

_EDIT_LEVEL_WITH_CLIENT_OPS = _EDIT_LEVEL | {"clients.lookup", "clients.create"}
_CLOSE_LEVEL_WITH_CLIENT_OPS = _CLOSE_LEVEL | {"clients.lookup", "clients.create"}


def test_nivel_1_visualizar_nao_cadastra_nem_pesquisa_cliente(client_as, org_a_actor):
    view_only = client_as(_restricted_actor(org_a_actor, permissions=_VIEW_LEVEL))

    create_resp = view_only.post("/api/v1/clients", json={"name": "Cliente Novo", "phone": "61911112222"})
    assert create_resp.status_code == 403, create_resp.text

    lookup_resp = view_only.get("/api/v1/clients/lookup", params={"search": "Cliente"})
    assert lookup_resp.status_code == 403, lookup_resp.text


def test_nivel_2_orders_manage_sozinho_nao_cadastra_cliente(client_as, org_a_actor):
    """`orders.manage`/`orders.edit_price` sozinhos (sem as chaves
    operacionais de Clientes) continuam bloqueados — a permissão
    NUNCA foi acoplada no backend; quem monta um perfil customizado só
    com as chaves de Comandas, sem passar pelo bundle padrão do
    `ManageAccessDrawer`, precisa conceder `clients.lookup`/
    `clients.create` explicitamente."""
    editor_sem_client_ops = client_as(_restricted_actor(org_a_actor, permissions=_EDIT_LEVEL))

    create_resp = editor_sem_client_ops.post(
        "/api/v1/clients", json={"name": "Cliente Novo", "phone": "61911112222"}
    )
    assert create_resp.status_code == 403, create_resp.text


def test_nivel_2_criar_e_editar_cadastra_e_pesquisa_cliente(client_as, org_a_actor):
    """Com o bundle padrão que `ManageAccessDrawer` concede pra "Criar e
    editar comandas" (`_EDIT_LEVEL` + `clients.lookup`/`clients.create`)
    — cadastra cliente novo e pesquisa cliente já existente, sem
    `clients.view`/`clients.manage`."""
    editor = client_as(_restricted_actor(org_a_actor, permissions=_EDIT_LEVEL_WITH_CLIENT_OPS))

    create_resp = editor.post("/api/v1/clients", json={"name": "Cliente Novo", "phone": "61911112222"})
    assert create_resp.status_code == 201, create_resp.text

    lookup_resp = editor.get("/api/v1/clients/lookup", params={"search": "Cliente Novo"})
    assert lookup_resp.status_code == 200, lookup_resp.text
    assert any(item["id"] == create_resp.json()["id"] for item in lookup_resp.json())

    # Nunca abre a Ficha 360°/listagem completa — isso continua exigindo
    # `clients.view`/`clients.manage`, que este ator não tem.
    assert editor.get("/api/v1/clients").status_code == 403
    assert editor.get(f"/api/v1/clients/{create_resp.json()['id']}").status_code == 403


def test_nivel_3_finalizar_tambem_cadastra_cliente(client_as, org_a_actor):
    """Nível 3 inclui o nível 2 por completo — também cadastra cliente."""
    closer = client_as(_restricted_actor(org_a_actor, permissions=_CLOSE_LEVEL_WITH_CLIENT_OPS))

    resp = closer.post("/api/v1/clients", json={"name": "Cliente Nível 3", "phone": "61933334444"})
    assert resp.status_code == 201, resp.text


def test_isolamento_multi_tenant_no_cadastro_de_cliente_via_nivel_2(client_as, org_a_actor, org_b_actor):
    """Cliente cadastrado por um ator de nível 2 nasce sempre na
    organização do próprio ator (derivada do `ActorContext`, nunca de
    um campo enviado pelo frontend) — outro tenant, mesmo com o mesmo
    bundle de permissões, não o enxerga."""
    editor_a = client_as(_restricted_actor(org_a_actor, permissions=_EDIT_LEVEL_WITH_CLIENT_OPS))
    created = editor_a.post(
        "/api/v1/clients", json={"name": "Só da Org A via Comanda", "phone": "61955556666"}
    ).json()

    editor_b = client_as(_restricted_actor(org_b_actor, permissions=_EDIT_LEVEL_WITH_CLIENT_OPS))
    lookup_resp = editor_b.get("/api/v1/clients/lookup", params={"search": "Só da Org A via Comanda"})
    assert lookup_resp.status_code == 200, lookup_resp.text
    assert all(item["id"] != created["id"] for item in lookup_resp.json())
