def test_crud_branch(client_as, org_a_actor):
    c = client_as(org_a_actor)

    resp = c.post("/api/v1/branches", json={"name": "Matriz", "slug": "matriz"})
    assert resp.status_code == 201, resp.text
    branch = resp.json()
    assert branch["organization_id"] == str(org_a_actor.organization_id)
    assert branch["is_active"] is True
    branch_id = branch["id"]

    resp = c.get(f"/api/v1/branches/{branch_id}")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "matriz"

    resp = c.get("/api/v1/branches")
    assert resp.status_code == 200
    assert any(b["id"] == branch_id for b in resp.json())

    resp = c.put(f"/api/v1/branches/{branch_id}", json={"name": "Matriz Renomeada", "slug": "matriz"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Matriz Renomeada"


def test_desativar_branch_nao_apaga_historico(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = c.post("/api/v1/branches", json={"name": "Unidade X", "slug": "unidade-x"}).json()["id"]

    resp = c.patch(f"/api/v1/branches/{branch_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # continua existindo e acessível por id — só não aparece na listagem padrão
    resp = c.get(f"/api/v1/branches/{branch_id}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    resp = c.get("/api/v1/branches")
    assert branch_id not in [b["id"] for b in resp.json()]

    resp = c.get("/api/v1/branches?include_inactive=true")
    assert branch_id in [b["id"] for b in resp.json()]

    resp = c.patch(f"/api/v1/branches/{branch_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_branch_nao_encontrada_404(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/api/v1/branches/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"
