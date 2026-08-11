"""Isolamento entre organizações — usuário/organização A não pode ler
nem escrever recurso de B, em nenhum dos quatro recursos desta etapa.
Cross-org retorna 404 (não 403): não confirma nem a existência do
recurso pra quem não tem acesso."""
import uuid


def test_branch_isolada_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    branch_a = client_as(org_a_actor).post(
        "/api/v1/branches", json={"name": "Unidade A", "slug": "unidade-a"}
    ).json()

    b = client_as(org_b_actor)
    assert b.get(f"/api/v1/branches/{branch_a['id']}").status_code == 404
    assert branch_a["id"] not in [x["id"] for x in b.get("/api/v1/branches").json()]

    resp = b.put(f"/api/v1/branches/{branch_a['id']}", json={"name": "Hackeada", "slug": "unidade-a"})
    assert resp.status_code == 404
    resp = b.patch(f"/api/v1/branches/{branch_a['id']}/deactivate")
    assert resp.status_code == 404


def test_professional_isolado_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    professional_a = client_as(org_a_actor).post(
        "/api/v1/professionals", json={"name": "Profissional da Org A"}
    ).json()

    b = client_as(org_b_actor)
    assert b.get(f"/api/v1/professionals/{professional_a['id']}").status_code == 404
    assert professional_a["id"] not in [x["id"] for x in b.get("/api/v1/professionals").json()]
    assert b.get(f"/api/v1/professionals/{professional_a['id']}/working-hours").status_code == 404
    assert b.get(f"/api/v1/professionals/{professional_a['id']}/services").status_code == 404


def test_service_isolado_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    service_a = client_as(org_a_actor).post(
        "/api/v1/services",
        json={"name": "Serviço da Org A", "default_duration_minutes": 30, "default_price": "50.00"},
    ).json()

    b = client_as(org_b_actor)
    assert b.get(f"/api/v1/services/{service_a['id']}").status_code == 404
    assert service_a["id"] not in [x["id"] for x in b.get("/api/v1/services").json()]


def test_client_isolado_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    client_a = client_as(org_a_actor).post("/api/v1/clients", json={"name": "Cliente da Org A"}).json()

    b = client_as(org_b_actor)
    assert b.get(f"/api/v1/clients/{client_a['id']}").status_code == 404
    assert client_a["id"] not in [x["id"] for x in b.get("/api/v1/clients").json()]
    # busca também não vaza
    assert client_a["id"] not in [x["id"] for x in b.get("/api/v1/clients?search=Cliente").json()]


def test_organization_atual_reflete_o_ator(client_as, org_a_actor, org_b_actor):
    resp_a = client_as(org_a_actor).get("/api/v1/organization")
    resp_b = client_as(org_b_actor).get("/api/v1/organization")
    assert resp_a.json()["id"] == str(org_a_actor.organization_id)
    assert resp_b.json()["id"] == str(org_b_actor.organization_id)
    assert resp_a.json()["id"] != resp_b.json()["id"]


def test_id_aleatorio_nunca_vaza_dado_de_outra_org(client_as, org_a_actor):
    a = client_as(org_a_actor)
    resp = a.get(f"/api/v1/clients/{uuid.uuid4()}")
    assert resp.status_code == 404
