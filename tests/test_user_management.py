"""Testes de `Configurações > Acessos` — "Remover acesso"
(`PATCH /users/{id}/remove-access`, `MembershipStatus.REMOVED`) e do
guard "não deixar a organização sem nenhum Usuário Master". Cobre
soft-delete (nunca apaga `User`/histórico), isolamento entre
organizações, e que o guard é 100% baseado na permission efetiva
`organization.manage` — nunca no NOME do role (funciona pra qualquer
role customizado que receba essa permission via override)."""
import uuid


def _role_id(c, name: str) -> str:
    roles = c.get("/api/v1/roles").json()
    return next(r["id"] for r in roles if r["name"] == name)


def _invite(c, *, role_id: str, name: str = "Funcionário de Teste") -> dict:
    resp = c.post(
        "/api/v1/users",
        json={
            "email": f"{uuid.uuid4().hex[:10]}@nexasalon.local", "name": name,
            "role_id": role_id, "password": "SenhaForte123!",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["membership"]


def test_master_remove_acesso_de_outro_membro(client_as, org_a_actor):
    c = client_as(org_a_actor)
    recep_role = _role_id(c, "RECEPTIONIST")
    membership = _invite(c, role_id=recep_role)
    assert membership["status"] == "active"

    resp = c.patch(f"/api/v1/users/{membership['id']}/remove-access")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "removed"


def test_remover_acesso_bloqueia_mas_preserva_historico(client_as, org_a_actor):
    """Soft-delete: a linha da membership continua existindo (nunca um
    hard delete do `User`) — consultável na listagem com
    `include_inactive=true`, mesmo `user_id`/`user_email` preservados."""
    c = client_as(org_a_actor)
    recep_role = _role_id(c, "RECEPTIONIST")
    membership = _invite(c, role_id=recep_role, name="Marco Recepção")
    c.patch(f"/api/v1/users/{membership['id']}/remove-access")

    listed_active = c.get("/api/v1/users").json()
    assert all(m["id"] != membership["id"] for m in listed_active)

    listed_all = c.get("/api/v1/users", params={"include_inactive": True}).json()
    removed = next(m for m in listed_all if m["id"] == membership["id"])
    assert removed["status"] == "removed"
    assert removed["user_id"] == membership["user_id"]
    assert removed["user_email"] == membership["user_email"]
    assert removed["user_name"] == "Marco Recepção"


def test_remover_acesso_duas_vezes_e_idempotente_no_efeito_final(client_as, org_a_actor):
    c = client_as(org_a_actor)
    recep_role = _role_id(c, "RECEPTIONIST")
    membership = _invite(c, role_id=recep_role)

    first = c.patch(f"/api/v1/users/{membership['id']}/remove-access")
    assert first.status_code == 200
    second = c.patch(f"/api/v1/users/{membership['id']}/remove-access")
    assert second.status_code == 200
    assert second.json()["status"] == "removed"


def test_remover_ultimo_master_e_bloqueado(client_as, org_a_actor):
    """`org_a_actor` (fixture OWNER/Master) é o ÚNICO membro com
    `organization.manage` nesta organização — tentar remover o PRÓPRIO
    acesso precisa ser recusado, nunca deixando a organização sem
    ninguém capaz de geri-la."""
    c = client_as(org_a_actor)
    resp = c.patch(f"/api/v1/users/{org_a_actor.membership_id}/remove-access")
    assert resp.status_code == 422, resp.text
    assert "Master" in resp.json()["error"]["message"]

    # Nada mudou — o Master continua ACTIVE.
    listed = c.get("/api/v1/users", params={"include_inactive": True}).json()
    still_active = next(m for m in listed if m["id"] == str(org_a_actor.membership_id))
    assert still_active["status"] == "active"


def test_remover_um_master_e_permitido_se_houver_outro_master_ativo(client_as, org_a_actor):
    """Com DOIS Masters ativos, remover um deles deve funcionar
    normalmente — a trava é especificamente contra ficar em ZERO, nunca
    contra remover um Master quando sobra outro."""
    c = client_as(org_a_actor)
    owner_role = _role_id(c, "OWNER")
    second_master = _invite(c, role_id=owner_role, name="Segundo Master")

    resp = c.patch(f"/api/v1/users/{second_master['id']}/remove-access")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "removed"

    # O Master original continua intacto e a organização não ficou órfã.
    resp_first = c.patch(f"/api/v1/users/{org_a_actor.membership_id}/remove-access")
    assert resp_first.status_code == 422  # agora ele é o único de novo


def test_custom_role_com_organization_manage_via_override_tambem_conta_como_master(client_as, org_a_actor):
    """O guard nunca checa `role.name == "OWNER"` — um role ADMIN (que
    NÃO tem `organization.manage` de fábrica, migration 0007) com um
    override GRANT nessa permission passa a contar como Master
    igualmente. Prova que a trava é 100% baseada em permissão efetiva,
    nunca no nome do role."""
    c = client_as(org_a_actor)
    admin_role = _role_id(c, "ADMIN")
    admin_membership = _invite(c, role_id=admin_role, name="Admin com Override")

    resp = c.put(
        f"/api/v1/users/{admin_membership['id']}/permission-overrides",
        json={"overrides": [{"permission_key": "organization.manage", "effect": "grant"}]},
    )
    assert resp.status_code == 200, resp.text

    # Agora existem DOIS Masters efetivos (org_a_actor + este ADMIN
    # promovido por override) — remover o ORIGINAL deve funcionar,
    # porque o ADMIN com override cobre a organização.
    resp = c.patch(f"/api/v1/users/{org_a_actor.membership_id}/remove-access")
    assert resp.status_code == 200, resp.text

    # Agora o ADMIN promovido é o ÚNICO Master efetivo — removê-lo
    # também precisa ser bloqueado, mesmo seu `role.name` sendo "ADMIN".
    resp = c.patch(f"/api/v1/users/{admin_membership['id']}/remove-access")
    assert resp.status_code == 422, resp.text


def test_isolamento_multi_tenant_no_remove_access(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    recep_role = _role_id(c_a, "RECEPTIONIST")
    membership = _invite(c_a, role_id=recep_role)

    c_b = client_as(org_b_actor)
    resp = c_b.patch(f"/api/v1/users/{membership['id']}/remove-access")
    assert resp.status_code == 404

    # Continua ativa na Org A, intocada.
    listed = c_a.get("/api/v1/users").json()
    assert any(m["id"] == membership["id"] and m["status"] == "active" for m in listed)
