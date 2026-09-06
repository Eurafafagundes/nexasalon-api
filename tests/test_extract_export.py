"""Testes HTTP de `GET /api/v1/extract/export` (Etapa L, Bloco 12) —
exportação `.xlsx` REAL (nunca CSV disfarçado) do Financeiro > Extrato.
Cobre: o arquivo devolvido é um `.xlsx` de verdade (abre com `openpyxl`)
com as MESMAS vendas que `GET /extract` mostraria; o filtro de período é
respeitado; a mesma permissão (`finance.view`) e o mesmo isolamento
multi-tenant do `GET /extract` original — nunca uma segunda rota com
regra de acesso diferente pro mesmo dado."""
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

from openpyxl import load_workbook

_START = "2026-08-13T14:00:00-03:00"  # quinta-feira


def _setup_closed_order(c):
    """Cria unidade/profissional/serviço/cliente/agendamento/comanda via
    API e fecha com um pagamento — mesmo padrão de
    `test_orders_api.py::_setup_finished_appointment`."""
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    professional = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    service = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 60, "default_price": "150.00"}
    ).json()
    c.put(f"/api/v1/professionals/{professional['id']}/services", json={"items": [{"service_id": service["id"]}]})
    c.put(
        f"/api/v1/professionals/{professional['id']}/working-hours",
        json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
    )
    client = c.post("/api/v1/clients", json={"name": "Cliente Exportação"}).json()
    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [{"professional_id": professional["id"], "service_id": service["id"], "start_at": _START}],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target}).status_code == 200

    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "150.00", "cash_register_id": register["id"]}]},
    )
    assert closed.status_code == 200, closed.text
    return client


def test_export_gera_xlsx_real_com_a_mesma_venda_do_extrato(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client = _setup_closed_order(c)

    resp = c.get("/api/v1/extract/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.xlsx"')

    workbook = load_workbook(BytesIO(resp.content))
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    header, *data_rows = rows
    assert header[0] == "Data"
    assert "Valor" in header

    client_col = header.index("Cliente")
    value_col = header.index("Valor")
    matching = [r for r in data_rows if r[client_col] == client["name"]]
    assert len(matching) == 1
    assert matching[0][value_col] == 150.0


def test_export_respeita_filtro_de_periodo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _setup_closed_order(c)

    far_future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    resp = c.get("/api/v1/extract/export", params={"date_from": far_future})
    assert resp.status_code == 200, resp.text
    workbook = load_workbook(BytesIO(resp.content))
    rows = list(workbook.active.iter_rows(values_only=True))
    assert len(rows) == 1  # só o header — a venda de hoje fica fora da janela futura

    resp_all = c.get("/api/v1/extract/export")
    rows_all = list(load_workbook(BytesIO(resp_all.content)).active.iter_rows(values_only=True))
    assert len(rows_all) == 2  # header + a venda


def test_export_exige_finance_view(client_as, org_a_actor):
    import dataclasses

    restricted = dataclasses.replace(org_a_actor, permissions=org_a_actor.permissions - {"finance.view"})
    resp = client_as(restricted).get("/api/v1/extract/export")
    assert resp.status_code == 403


def _setup_closed_order_two_items(c):
    """Mesmo padrão de `_setup_closed_order`, mas com 2 serviços e 2
    profissionais na mesma comanda — pra testar a granularidade por
    item (Etapa N2) na exportação."""
    branch = c.post("/api/v1/branches", json={"name": "Matriz", "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()
    ianka = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()
    duda = c.post("/api/v1/professionals", json={"name": "Duda"}).json()
    manutencao = c.post(
        "/api/v1/services",
        json={"name": "Manutenção Mega Hair 1 Tela", "default_duration_minutes": 120, "default_price": "230.00"},
    ).json()
    corte = c.post(
        "/api/v1/services", json={"name": "Corte", "default_duration_minutes": 30, "default_price": "100.00"}
    ).json()
    for prof, svc in ((ianka, manutencao), (duda, corte)):
        c.put(f"/api/v1/professionals/{prof['id']}/services", json={"items": [{"service_id": svc["id"]}]})
        c.put(
            f"/api/v1/professionals/{prof['id']}/working-hours",
            json={"items": [{"weekday": 4, "start_time": "09:00:00", "end_time": "20:00:00"}]},
        )
    client = c.post("/api/v1/clients", json={"name": "Maria"}).json()
    appt = c.post(
        "/api/v1/appointments",
        json={
            "branch_id": branch["id"], "client_id": client["id"],
            "items": [
                {"professional_id": ianka["id"], "service_id": manutencao["id"], "start_at": _START},
                {"professional_id": duda["id"], "service_id": corte["id"], "start_at": _START},
            ],
        },
    ).json()
    for target in ["confirmed", "waiting", "in_progress", "finished"]:
        assert c.patch(f"/api/v1/appointments/{appt['id']}/status", json={"status": target}).status_code == 200

    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    order = c.post("/api/v1/orders", json={"appointment_id": appt["id"]}).json()
    closed = c.post(
        f"/api/v1/orders/{order['id']}/close",
        json={"payments": [{"method": "pix", "amount": "330.00", "cash_register_id": register["id"]}]},
    )
    assert closed.status_code == 200, closed.text
    return client, order


def test_export_gera_uma_linha_por_servico_mantendo_a_mesma_comanda(client_as, org_a_actor):
    """Etapa N2 — item explícito do pedido: "o Excel deve gerar uma
    linha por OrderItem", preservando a referência da mesma comanda,
    sem repetir o valor total em cada linha nem inflar o faturamento."""
    c = client_as(org_a_actor)
    client, order = _setup_closed_order_two_items(c)

    resp = c.get("/api/v1/extract/export", params={"type": "sales"})
    assert resp.status_code == 200, resp.text
    rows = list(load_workbook(BytesIO(resp.content)).active.iter_rows(values_only=True))
    header, *data_rows = rows

    comanda_col = header.index("Comanda")
    servico_col = header.index("Serviço/Produto")
    profissional_col = header.index("Profissional")
    valor_col = header.index("Valor")

    matching = [r for r in data_rows if r[comanda_col] == f"#{order['order_number']}"]
    # UMA linha por serviço — 2 serviços na comanda = 2 linhas.
    assert len(matching) == 2
    # as duas linhas mantêm o MESMO identificador de comanda.
    assert {r[comanda_col] for r in matching} == {f"#{order['order_number']}"}

    services = {r[servico_col]: r[profissional_col] for r in matching}
    assert services["Manutenção Mega Hair 1 Tela"] == "Ianka"
    assert services["Corte"] == "Duda"

    values = {r[servico_col]: r[valor_col] for r in matching}
    assert values["Manutenção Mega Hair 1 Tela"] == 230.0
    assert values["Corte"] == 100.0

    # a soma das linhas desta comanda continua batendo com o total real
    # (R$ 330) — nunca R$ 660 (item explícito do pedido: não inflar
    # faturamento por causa da granularidade por serviço).
    assert sum(r[valor_col] for r in matching) == 330.0


def test_export_com_type_sales_nunca_inclui_despesas(client_as, org_a_actor):
    """Etapa N1 — regressão real reportada: filtrar "Vendas" na tela e
    exportar não podia incluir despesas. `type=sales` tem que produzir
    uma planilha só com a venda, mesmo havendo uma despesa manual no
    mesmo período/organização."""
    c = client_as(org_a_actor)
    _setup_closed_order(c)
    branch = c.post("/api/v1/branches", json={"name": "Filial", "slug": f"filial-{uuid.uuid4().hex[:6]}"}).json()
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    withdrawal = c.post(
        f"/api/v1/cash-registers/{register['id']}/movements",
        json={"type": "withdrawal", "amount": "40.00", "description": "Compra de produtos"},
    )
    assert withdrawal.status_code == 200, withdrawal.text

    resp = c.get("/api/v1/extract/export", params={"type": "sales"})
    assert resp.status_code == 200, resp.text
    rows = list(load_workbook(BytesIO(resp.content)).active.iter_rows(values_only=True))
    header, *data_rows = rows
    tipo_col = header.index("Tipo")
    assert all(r[tipo_col] == "Venda" for r in data_rows)
    assert len(data_rows) == 1  # só a venda — a despesa NÃO aparece


def test_export_com_type_withdrawal_nunca_inclui_vendas(client_as, org_a_actor):
    c = client_as(org_a_actor)
    _setup_closed_order(c)
    branch = c.post("/api/v1/branches", json={"name": "Filial", "slug": f"filial-{uuid.uuid4().hex[:6]}"}).json()
    register = c.post("/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}).json()
    withdrawal = c.post(
        f"/api/v1/cash-registers/{register['id']}/movements",
        json={"type": "withdrawal", "amount": "40.00", "description": "Compra de produtos"},
    )
    assert withdrawal.status_code == 200, withdrawal.text

    resp = c.get("/api/v1/extract/export", params={"type": "withdrawal"})
    assert resp.status_code == 200, resp.text
    rows = list(load_workbook(BytesIO(resp.content)).active.iter_rows(values_only=True))
    header, *data_rows = rows
    tipo_col = header.index("Tipo")
    assert len(data_rows) == 1
    assert data_rows[0][tipo_col] == "Despesa"


def test_listagem_get_extract_com_type_respeita_o_mesmo_filtro_da_exportacao(client_as, org_a_actor):
    """Mesmo contrato (`type`) pras duas rotas — "o que está
    visualizado é o que será exportado"."""
    c = client_as(org_a_actor)
    _setup_closed_order(c)

    resp = c.get("/api/v1/extract", params={"type": "sales"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["sales"]) == 1
    assert body["movements"] == []


def test_export_nao_vaza_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    client_a = _setup_closed_order(c_a)

    c_b = client_as(org_b_actor)
    resp = c_b.get("/api/v1/extract/export")
    assert resp.status_code == 200, resp.text
    workbook = load_workbook(BytesIO(resp.content))
    rows = list(workbook.active.iter_rows(values_only=True))
    header, *data_rows = rows
    client_col = header.index("Cliente")
    assert all(r[client_col] != client_a["name"] for r in data_rows)


def test_export_busca_multiplos_caixas_em_uma_query_sem_mudar_conteudo(client_as, org_a_actor):
    from sqlalchemy import event

    from nexasalon_api.core.db import engine as db_engine

    c = client_as(org_a_actor)
    descriptions = []
    for index in range(3):
        branch = c.post(
            "/api/v1/branches", json={"name": f"Filial lote {index}", "slug": f"filial-lote-{uuid.uuid4().hex[:6]}"}
        ).json()
        register = c.post(
            "/api/v1/cash-registers", json={"branch_id": branch["id"], "initial_amount": "0"}
        ).json()
        description = f"Despesa lote {index}"
        descriptions.append(description)
        movement = c.post(
            f"/api/v1/cash-registers/{register['id']}/movements",
            json={"type": "withdrawal", "amount": "10.00", "description": description},
        )
        assert movement.status_code == 200, movement.text

    statements: list[str] = []

    def _counter(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT") and "cash_registers" in statement.lower():
            statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", _counter)
    try:
        response = c.get("/api/v1/extract/export", params={"type": "withdrawal"})
    finally:
        event.remove(db_engine, "before_cursor_execute", _counter)

    assert response.status_code == 200, response.text
    rows = list(load_workbook(BytesIO(response.content)).active.iter_rows(values_only=True))
    header, *data_rows = rows
    description_column = header.index("DescriÃ§Ã£o")
    exported_descriptions = {row[description_column] for row in data_rows}
    assert set(descriptions) <= exported_descriptions
    assert len(statements) == 1, statements
