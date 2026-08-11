def test_crud_service(client_as, org_a_actor):
    c = client_as(org_a_actor)

    resp = c.post(
        "/api/v1/services",
        json={"name": "Escova", "default_duration_minutes": 45, "default_price": "80.00"},
    )
    assert resp.status_code == 201, resp.text
    service = resp.json()
    assert service["is_active"] is True
    service_id = service["id"]

    resp = c.get(f"/api/v1/services/{service_id}")
    assert resp.status_code == 200

    resp = c.put(
        f"/api/v1/services/{service_id}",
        json={"name": "Escova Modelada", "default_duration_minutes": 60, "default_price": "95.00"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Escova Modelada"

    resp = c.patch(f"/api/v1/services/{service_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # segue existindo — desativação não apaga
    resp = c.get(f"/api/v1/services/{service_id}")
    assert resp.status_code == 200


def test_validacao_duracao_deve_ser_positiva(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/services",
        json={"name": "Corte", "default_duration_minutes": 0, "default_price": "50.00"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "validation_error"


def test_validacao_preco_nao_pode_ser_negativo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/services",
        json={"name": "Corte", "default_duration_minutes": 30, "default_price": "-10.00"},
    )
    assert resp.status_code == 422


def test_preco_zero_e_permitido(client_as, org_a_actor):
    # cortesia/serviço promocional — preço 0 é válido, duração 0 não é.
    c = client_as(org_a_actor)
    resp = c.post(
        "/api/v1/services",
        json={"name": "Retoque cortesia", "default_duration_minutes": 15, "default_price": "0.00"},
    )
    assert resp.status_code == 201
