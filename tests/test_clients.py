def test_crud_client(client_as, org_a_actor):
    c = client_as(org_a_actor)

    resp = c.post(
        "/api/v1/clients",
        json={"name": "Maria Souza", "phone": "11999990000", "email": "maria@example.com"},
    )
    assert resp.status_code == 201, resp.text
    client_obj = resp.json()
    assert client_obj["is_active"] is True
    client_id = client_obj["id"]

    resp = c.get(f"/api/v1/clients/{client_id}")
    assert resp.status_code == 200

    resp = c.put(
        f"/api/v1/clients/{client_id}",
        json={"name": "Maria S. Souza", "phone": "11999990000"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Maria S. Souza"


def test_buscar_por_nome_e_telefone(client_as, org_a_actor):
    c = client_as(org_a_actor)
    c.post("/api/v1/clients", json={"name": "Maria Souza", "phone": "11999990000"})
    c.post("/api/v1/clients", json={"name": "João Pereira", "phone": "11988887777"})

    resp = c.get("/api/v1/clients", params={"search": "Maria"})
    assert resp.status_code == 200
    names = [cl["name"] for cl in resp.json()]
    assert "Maria Souza" in names
    assert "João Pereira" not in names

    resp = c.get("/api/v1/clients", params={"search": "999990000"})
    assert any(cl["phone"] == "11999990000" for cl in resp.json())


def test_desativar_cliente_nao_apaga(client_as, org_a_actor):
    c = client_as(org_a_actor)
    client_id = c.post("/api/v1/clients", json={"name": "Cliente Temporário"}).json()["id"]

    resp = c.patch(f"/api/v1/clients/{client_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = c.get(f"/api/v1/clients/{client_id}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    assert client_id not in [cl["id"] for cl in c.get("/api/v1/clients").json()]
    assert client_id in [cl["id"] for cl in c.get("/api/v1/clients?include_inactive=true").json()]

    resp = c.patch(f"/api/v1/clients/{client_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
