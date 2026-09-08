"""`DELETE /service-categories/{id}` — exclusão de verdade (hard
delete), só permitida quando a categoria não tem NENHUM serviço
vinculado (ativo ou inativo). Nunca apaga em cascata, nunca deixa FK
inválida — ver docstring de `services/service_categories.py::delete_category`."""
import uuid


def _create_category(c, name, **overrides):
    payload = {"name": name, **overrides}
    resp = c.post("/api/v1/service-categories", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_service(c, name, **overrides):
    payload = {"name": name, "default_duration_minutes": 30, "default_price": "50.00", **overrides}
    resp = c.post("/api/v1/services", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_categoria_sem_servicos_pode_ser_excluida(client_as, org_a_actor):
    c = client_as(org_a_actor)
    categoria = _create_category(c, "Sobrancelha")

    resp = c.delete(f"/api/v1/service-categories/{categoria['id']}")

    assert resp.status_code == 204
    listagem = c.get("/api/v1/service-categories", params={"include_inactive": True}).json()
    assert categoria["id"] not in {cat["id"] for cat in listagem}


def test_categoria_com_servico_ativo_nao_e_excluida(client_as, org_a_actor):
    c = client_as(org_a_actor)
    categoria = _create_category(c, "Manutenção de telas")
    _create_service(c, "Manutenção de 1 tela", category_id=categoria["id"])

    resp = c.delete(f"/api/v1/service-categories/{categoria['id']}")

    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert "1 serviço" in body["message"]
    assert "Mova ou remova os serviços antes de excluir a categoria." in body["message"]
    # categoria continua existindo — nada foi apagado por baixo
    assert c.get(f"/api/v1/service-categories/{categoria['id']}").status_code == 200


def test_mensagem_pluraliza_corretamente_com_mais_de_um_servico(client_as, org_a_actor):
    c = client_as(org_a_actor)
    categoria = _create_category(c, "Manutenção com escova")
    _create_service(c, "1 tela com escova", category_id=categoria["id"])
    _create_service(c, "2 telas com escova", category_id=categoria["id"])

    resp = c.delete(f"/api/v1/service-categories/{categoria['id']}")

    assert resp.status_code == 409
    assert "2 serviços" in resp.json()["error"]["message"]


def test_categoria_com_servico_inativo_tambem_bloqueia_exclusao(client_as, org_a_actor):
    """Um serviço desativado ainda referencia a categoria — não é
    "como se não existisse" pra fins desta regra (ele pode ser
    reativado a qualquer momento, ver `service-form-drawer`)."""
    c = client_as(org_a_actor)
    categoria = _create_category(c, "Progressiva")
    servico = _create_service(c, "Progressiva Premium", category_id=categoria["id"])
    assert c.patch(f"/api/v1/services/{servico['id']}/deactivate").status_code == 200

    resp = c.delete(f"/api/v1/service-categories/{categoria['id']}")

    assert resp.status_code == 409
    assert "1 serviço" in resp.json()["error"]["message"]


def test_mover_servico_para_outra_categoria_libera_a_exclusao(client_as, org_a_actor):
    """A arquitetura já suporta mover um serviço de categoria com
    segurança via `PUT /services/{id}` — depois de mover o único
    serviço pra fora, a categoria de origem fica livre pra excluir."""
    c = client_as(org_a_actor)
    origem = _create_category(c, "Categoria origem")
    destino = _create_category(c, "Categoria destino")
    servico = _create_service(c, "Corte", category_id=origem["id"])

    update_payload = {
        "name": servico["name"],
        "category_id": destino["id"],
        "default_duration_minutes": servico["default_duration_minutes"],
        "default_price": servico["default_price"],
    }
    resp_update = c.put(f"/api/v1/services/{servico['id']}", json=update_payload)
    assert resp_update.status_code == 200, resp_update.text
    assert resp_update.json()["category_id"] == destino["id"]

    resp_delete = c.delete(f"/api/v1/service-categories/{origem['id']}")
    assert resp_delete.status_code == 204


def test_excluir_categoria_inexistente_retorna_404(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.delete(f"/api/v1/service-categories/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_excluir_categoria_de_outra_organizacao_retorna_404_sem_vazar_existencia(
    client_as, org_a_actor, org_b_actor
):
    categoria_a = _create_category(client_as(org_a_actor), "Categoria só da A")

    resp = client_as(org_b_actor).delete(f"/api/v1/service-categories/{categoria_a['id']}")

    assert resp.status_code == 404
    # nada foi apagado — a organização A continua enxergando a categoria dela
    assert client_as(org_a_actor).get(f"/api/v1/service-categories/{categoria_a['id']}").status_code == 200
