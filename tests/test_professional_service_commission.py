"""Testes da Etapa C1 — Comissão por Profissional/Serviço
(`ProfessionalService.commission_type`/`commission_value`, campos que
já existiam desde a migration 0002, sem migration nova nesta etapa).

Cobre: ativar serviço com comissão percentual/fixa; editar percentual;
trocar percentual -> fixo; desativar sem perder a configuração
(`is_active=false` preserva a linha inteira); reativar recuperando a
comissão anterior; validações (percentual >100, valor <=0, tipo/valor
não podem vir um sem o outro); duas profissionais com comissão
DIFERENTE pelo MESMO serviço (a regra pertence ao vínculo, nunca a uma
tabela global).

`is_active=false` já era, antes desta etapa, a forma de "profissional
não presta mais este serviço" reconhecida em todo o resto do domínio
(ver `test_dynamic_catalog.py::test_professional_service_inativo_impede_agendamento`)
— a C1 só corrige o FRONTEND, que hoje remove a linha inteira em vez de
usar essa flag já suportada pelo backend."""
import uuid


def _create_branch(c, name="Matriz"):
    return c.post("/api/v1/branches", json={"name": name, "slug": f"matriz-{uuid.uuid4().hex[:6]}"}).json()


def _create_service(c, name, **overrides):
    payload = {"name": name, "default_duration_minutes": 30, "default_price": "50.00", **overrides}
    resp = c.post("/api/v1/services", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_professional(c, name, branch_id, **overrides):
    payload = {"name": name, "branch_id": branch_id, **overrides}
    resp = c.post("/api/v1/professionals", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _replace_services(c, professional_id, items):
    resp = c.put(f"/api/v1/professionals/{professional_id}/services", json={"items": items})
    return resp


def _link_one(c, professional_id, service_id, **overrides):
    resp = _replace_services(c, professional_id, [{"service_id": service_id, **overrides}])
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------
# Ativar com comissão
# ---------------------------------------------------------------------


def test_ativa_servico_com_comissao_percentual(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")

    rows = _link_one(
        c, prof["id"], servico["id"],
        is_active=True, commission_type="percentage", commission_value="20.00",
    )
    assert len(rows) == 1
    assert rows[0]["is_active"] is True
    assert rows[0]["commission_type"] == "percentage"
    assert rows[0]["commission_value"] == "20.00"


def test_ativa_servico_com_comissao_fixa(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Mega Hair")

    rows = _link_one(
        c, prof["id"], servico["id"],
        is_active=True, commission_type="fixed", commission_value="100.00",
    )
    assert rows[0]["commission_type"] == "fixed"
    assert rows[0]["commission_value"] == "100.00"


def test_ativa_servico_sem_comissao_configurada(client_as, org_a_actor):
    """Continua válido não configurar nenhuma comissão — o serviço fica
    ativo, `commission_type`/`commission_value` ficam `None`."""
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Corte")

    rows = _link_one(c, prof["id"], servico["id"], is_active=True)
    assert rows[0]["commission_type"] is None
    assert rows[0]["commission_value"] is None


# ---------------------------------------------------------------------
# Editar
# ---------------------------------------------------------------------


def test_editar_percentual_existente(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")
    _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="20.00")

    rows = _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="25.00")
    assert rows[0]["commission_value"] == "25.00"


def test_trocar_percentual_para_fixo(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")
    _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="20.00")

    rows = _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="fixed", commission_value="100.00")
    assert rows[0]["commission_type"] == "fixed"
    assert rows[0]["commission_value"] == "100.00"


# ---------------------------------------------------------------------
# Desativar sem perder / reativar recuperando
# ---------------------------------------------------------------------


def test_desativar_servico_preserva_comissao_configurada(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")
    _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="20.00")

    # Desativa mantendo os campos de comissão no payload (é isso que o
    # frontend corrigido na C1 passa a fazer — nunca omite a linha).
    rows = _link_one(c, prof["id"], servico["id"], is_active=False, commission_type="percentage", commission_value="20.00")
    assert rows[0]["is_active"] is False
    assert rows[0]["commission_type"] == "percentage"
    assert rows[0]["commission_value"] == "20.00"

    # GET confirma que a linha persiste no banco (nunca foi apagada).
    get_resp = c.get(f"/api/v1/professionals/{prof['id']}/services")
    assert get_resp.status_code == 200
    persisted = get_resp.json()
    assert len(persisted) == 1
    assert persisted[0]["is_active"] is False
    assert persisted[0]["commission_value"] == "20.00"


def test_reativar_servico_recupera_comissao_anterior(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")
    _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="20.00")
    _link_one(c, prof["id"], servico["id"], is_active=False, commission_type="percentage", commission_value="20.00")

    rows = _link_one(c, prof["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="20.00")
    assert rows[0]["is_active"] is True
    assert rows[0]["commission_value"] == "20.00"


# ---------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------


def test_percentual_acima_de_100_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")

    resp = _replace_services(
        c, prof["id"],
        [{"service_id": servico["id"], "is_active": True, "commission_type": "percentage", "commission_value": "150.00"}],
    )
    assert resp.status_code == 422, resp.text


def test_valor_zero_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")

    resp = _replace_services(
        c, prof["id"],
        [{"service_id": servico["id"], "is_active": True, "commission_type": "fixed", "commission_value": "0"}],
    )
    assert resp.status_code == 422, resp.text


def test_tipo_sem_valor_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")

    resp = _replace_services(
        c, prof["id"],
        [{"service_id": servico["id"], "is_active": True, "commission_type": "percentage"}],
    )
    assert resp.status_code == 422, resp.text


def test_valor_sem_tipo_e_rejeitado(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    prof = _create_professional(c, "Ianka", branch["id"])
    servico = _create_service(c, "Manutenção")

    resp = _replace_services(
        c, prof["id"],
        [{"service_id": servico["id"], "is_active": True, "commission_value": "20.00"}],
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------
# Regra pertence ao vínculo, não a uma tabela global
# ---------------------------------------------------------------------


def test_profissionais_diferentes_tem_comissao_diferente_no_mesmo_servico(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch = _create_branch(c)
    servico = _create_service(c, "Manutenção")
    ianka = _create_professional(c, "Ianka", branch["id"])
    duda = _create_professional(c, "Duda", branch["id"])

    _link_one(c, ianka["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="20.00")
    _link_one(c, duda["id"], servico["id"], is_active=True, commission_type="percentage", commission_value="25.00")

    ianka_rows = c.get(f"/api/v1/professionals/{ianka['id']}/services").json()
    duda_rows = c.get(f"/api/v1/professionals/{duda['id']}/services").json()
    assert ianka_rows[0]["commission_value"] == "20.00"
    assert duda_rows[0]["commission_value"] == "25.00"
