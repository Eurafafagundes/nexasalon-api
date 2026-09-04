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
    """`role_name` opcional — necessário pra testar `require_role`
    (`orders.py::_register_payment`), que checa o NOME do role, não uma
    permission. Os valores reais de sistema são exatamente
    `"OWNER"`/`"ADMIN"`/`"RECEPTIONIST"`/`"PROFESSIONAL"` (maiúsculas,
    migration 0007) — note que a fixture `org_a_actor` (conftest.py)
    usa `role_name="Owner"` (fixture de teste só, não é o valor real de
    produção), por isso não serve pra testar `require_role` diretamente;
    use este helper com `role_name="OWNER"` explícito quando precisar."""
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


def test_role_generico_sem_permissao_nenhuma_e_bloqueado_no_fechamento(client_as, org_a_actor):
    """Checagem básica da dependency `require_role` na rota: um ator
    com role qualquer (nem OWNER nem RECEPTIONIST) e sem nenhuma
    permission — o caso mais simples de bloqueio. Cobertura completa
    da regra (`OWNER`/`RECEPTIONIST` passam, qualquer outro role
    bloqueia mesmo com `orders.manage`/`payments.register`) está no
    bloco de testes de "Decisão de produto" mais abaixo."""
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
# Decisão de produto: confirmar pagamento (fechar comanda) é restrito
# por ROLE, não por permission — só OWNER ("Master") e RECEPTIONIST
# ("Recepcionista"), mesmo que outro perfil tenha `orders.manage`/
# `payments.register` concedidos (ex.: via override customizado em
# Equipe e acessos). Substitui a regra permission-based da rodada
# anterior (que aceitava `orders.manage` como alternativa a
# `payments.register` — reaberta aqui porque permitia exatamente o que
# esta regra proíbe: qualquer perfil customizado com "Comandas →
# Gerenciar" conseguia pagar).
# ---------------------------------------------------------------------


def _open_order_ready_to_close(c):
    appt = _setup_finished_appointment(c)
    register = _open_register_for(c, appt["branch_id"])
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    return order, register


def test_master_role_owner_pode_fechar_comanda_com_pagamento(client_as, org_a_actor):
    """`org_a_actor` (fixture de `conftest.py`) já usa `role_name="OWNER"`
    (maiúsculas, igual à migration 0007 — bug real corrigido: era
    `"Owner"`, inofensivo até agora porque nada checava `role_name` pra
    autorização; ajustado em `conftest.py::seed_organization` junto com
    esta correção, senão TODO teste HTTP que fecha comanda via
    `org_a_actor` diretamente quebraria)."""
    c = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(c)

    resp = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"


def test_recepcionista_role_receptionist_pode_fechar_comanda_com_pagamento(client_as, org_a_actor):
    """O cenário real pedido: role RECEPTIONIST ("Recepcionista") — sem
    nenhuma permission (o gate agora ignora `actor.permissions`
    totalmente pra esta ação) — confirma pagamento normalmente."""
    master_setup = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master_setup)

    recepcionista_actor = _restricted_actor(org_a_actor, permissions=set(), role_name="RECEPTIONIST")
    recepcionista = client_as(recepcionista_actor)

    resp = recepcionista.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"


def test_recepcionista_fecha_consolidado_tambem(client_as, org_a_actor):
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

    recepcionista_actor = _restricted_actor(org_a_actor, permissions=set(), role_name="RECEPTIONIST")
    recepcionista = client_as(recepcionista_actor)

    resp = recepcionista.post(
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


def test_profissional_nao_consegue_registrar_pagamento(client_as, org_a_actor):
    """Role PROFESSIONAL — mesmo com `orders.view`+`orders.manage`
    concedidos (consegue visualizar/gerenciar a comanda normalmente),
    não pode confirmar pagamento."""
    master = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master)

    profissional_actor = _restricted_actor(
        org_a_actor, permissions={"orders.view", "orders.manage"}, role_name="PROFESSIONAL"
    )
    profissional = client_as(profissional_actor)

    resp = profissional.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 403, resp.text
    assert "Apenas Usuário Master e Recepcionista" in resp.json()["error"]["message"]


def test_outro_perfil_customizado_com_orders_manage_e_payments_register_continua_bloqueado(
    client_as, org_a_actor
):
    """Bug real corrigido: um perfil CUSTOMIZADO (não é literalmente
    OWNER nem RECEPTIONIST) com `orders.manage` E `payments.register`
    concedidos — exatamente o que a correção da rodada anterior
    permitia — agora continua bloqueado. O gate é 100% pelo NOME do
    role, nenhuma combinação de permission contorna."""
    master = client_as(org_a_actor)
    order, register = _open_order_ready_to_close(master)

    outro_perfil_actor = _restricted_actor(
        org_a_actor,
        permissions={"orders.view", "orders.manage", "payments.register"},
        role_name="Atendente Personalizado",
    )
    outro_perfil = client_as(outro_perfil_actor)

    resp = outro_perfil.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": order["total"], "cash_register_id": register["id"]}]},
    )
    assert resp.status_code == 403, resp.text


def test_outro_tenant_nao_fecha_comanda_mesmo_sendo_role_owner(client_as, org_a_actor, org_b_actor):
    """Isolamento por Organization é ortogonal ao role — `org_b_actor`
    também é role OWNER (a mesma fixture corrigida), mas de OUTRA
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


def test_orders_manage_nao_concede_acesso_ao_modulo_financeiro(client_as, org_a_actor):
    """`orders.manage` (mesmo sozinho, ou combinado com um role qualquer)
    nunca satisfaz `finance.view`/`finance.manage` (Extrato, Caixa,
    configuração de taxas) — módulos inteiramente independentes desta
    regra."""
    funcionario_actor = _restricted_actor(
        org_a_actor, permissions={"orders.view", "orders.manage"}, role_name="Atendente Personalizado"
    )
    funcionario = client_as(funcionario_actor)

    extrato_resp = funcionario.get("/api/v1/extract")
    assert extrato_resp.status_code == 403, extrato_resp.text

    caixas_resp = funcionario.get("/api/v1/cash-registers")
    assert caixas_resp.status_code == 403, caixas_resp.text

    taxas_resp = funcionario.get("/api/v1/payment-fee-rules")
    assert taxas_resp.status_code == 403, taxas_resp.text
