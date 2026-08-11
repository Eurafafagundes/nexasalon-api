def _create_branch(c, name="Matriz", slug="matriz"):
    return c.post("/api/v1/branches", json={"name": name, "slug": slug}).json()


def _create_service(c, name="Corte", duration=30, price="50.00"):
    return c.post(
        "/api/v1/services",
        json={"name": name, "default_duration_minutes": duration, "default_price": price},
    ).json()


def test_crud_professional(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)

    resp = c.post(
        "/api/v1/professionals",
        json={"name": "Ianka", "branch_id": branch["id"], "agenda_color": "#8B5CF6"},
    )
    assert resp.status_code == 201, resp.text
    professional = resp.json()
    assert professional["is_active"] is True
    assert professional["user_id"] is None  # profissional sem login, de propósito
    professional_id = professional["id"]

    resp = c.get(f"/api/v1/professionals/{professional_id}")
    assert resp.status_code == 200

    resp = c.put(
        f"/api/v1/professionals/{professional_id}",
        json={"name": "Ianka Souza", "branch_id": branch["id"], "agenda_color": "#8B5CF6"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Ianka Souza"


def test_professional_sem_branch_e_permitido(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/professionals", json={"name": "Sem unidade fixa"})
    assert resp.status_code == 201
    assert resp.json()["branch_id"] is None


def test_branch_deve_pertencer_a_mesma_organizacao(client_as, org_a_actor, org_b_actor):
    branch_b = client_as(org_b_actor).post(
        "/api/v1/branches", json={"name": "Unidade da Org B", "slug": "unidade-b"}
    ).json()

    resp = client_as(org_a_actor).post(
        "/api/v1/professionals", json={"name": "Profissional A", "branch_id": branch_b["id"]}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "validation_error"


def test_working_hours_replace_e_validacao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional_id = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()["id"]

    resp = c.put(
        f"/api/v1/professionals/{professional_id}/working-hours",
        json={"items": [
            {"weekday": 2, "start_time": "09:00:00", "end_time": "12:00:00"},
            {"weekday": 2, "start_time": "13:00:00", "end_time": "18:00:00"},
        ]},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2

    resp = c.get(f"/api/v1/professionals/{professional_id}/working-hours")
    assert len(resp.json()) == 2

    # substitui pelo conjunto novo — o antigo desaparece (semântica de PUT)
    resp = c.put(
        f"/api/v1/professionals/{professional_id}/working-hours",
        json={"items": [{"weekday": 3, "start_time": "10:00:00", "end_time": "16:00:00"}]},
    )
    assert len(resp.json()) == 1
    resp = c.get(f"/api/v1/professionals/{professional_id}/working-hours")
    assert len(resp.json()) == 1
    assert resp.json()[0]["weekday"] == 3

    # start >= end deve ser rejeitado
    resp = c.put(
        f"/api/v1/professionals/{professional_id}/working-hours",
        json={"items": [{"weekday": 1, "start_time": "18:00:00", "end_time": "09:00:00"}]},
    )
    assert resp.status_code == 422


def test_professional_service_mesma_organizacao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional_id = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()["id"]
    service = _create_service(c)

    resp = c.put(
        f"/api/v1/professionals/{professional_id}/services",
        json={"items": [{"service_id": service["id"], "price_override": "60.00"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["service_id"] == service["id"]
    assert resp.json()[0]["price_override"] == "60.00"

    resp = c.get(f"/api/v1/services/{service['id']}/professionals")
    assert resp.status_code == 200
    assert any(row["professional_id"] == professional_id for row in resp.json())


def test_professional_so_recebe_servicos_da_mesma_organizacao(client_as, org_a_actor, org_b_actor):
    service_b = client_as(org_b_actor).post(
        "/api/v1/services",
        json={"name": "Serviço da Org B", "default_duration_minutes": 30, "default_price": "40.00"},
    ).json()

    c = client_as(org_a_actor)
    professional_id = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()["id"]

    resp = c.put(
        f"/api/v1/professionals/{professional_id}/services",
        json={"items": [{"service_id": service_b["id"]}]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "validation_error"


def test_desativar_professional_nao_apaga_working_hours_nem_services(client_as, org_a_actor):
    c = client_as(org_a_actor)
    professional_id = c.post("/api/v1/professionals", json={"name": "Ianka"}).json()["id"]
    service = _create_service(c)
    c.put(
        f"/api/v1/professionals/{professional_id}/working-hours",
        json={"items": [{"weekday": 2, "start_time": "09:00:00", "end_time": "18:00:00"}]},
    )
    c.put(
        f"/api/v1/professionals/{professional_id}/services",
        json={"items": [{"service_id": service["id"]}]},
    )

    resp = c.patch(f"/api/v1/professionals/{professional_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    assert len(c.get(f"/api/v1/professionals/{professional_id}/working-hours").json()) == 1
    assert len(c.get(f"/api/v1/professionals/{professional_id}/services").json()) == 1
    assert professional_id not in [p["id"] for p in c.get("/api/v1/professionals").json()]
    assert professional_id in [p["id"] for p in c.get("/api/v1/professionals?include_inactive=true").json()]
