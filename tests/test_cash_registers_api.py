"""Testes HTTP de `/api/v1/cash-registers` — abrir/fechar caixa,
sangria/suprimento, e RBAC de `finance.view`/`finance.manage`
(catálogo já existente, reaproveitado — ver migration 0014)."""
import uuid

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User
from nexasalon_api.models.organization import Branch


def _branch_id(actor: ActorContext) -> str:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(actor.organization_id)}
        )
        branch = Branch(organization_id=actor.organization_id, name="Unidade", slug=f"unidade-{uuid.uuid4().hex[:8]}")
        session.add(branch)
        session.flush()
        branch_id = str(branch.id)  # lido ANTES do commit — commit expira os atributos
        # e um refresh pós-commit (RLS) precisaria do GUC de novo nessa mesma conexão.
        session.commit()
        return branch_id


def _restricted_actor(base_actor: ActorContext, *, permissions) -> ActorContext:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name="Usuário Restrito")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name="Restrito", permissions=frozenset(permissions),
    )


def test_abrir_listar_e_fechar_caixa_via_api(client_as, org_a_actor):
    c = client_as(org_a_actor)
    branch_id = _branch_id(org_a_actor)

    opened = c.post(
        "/api/v1/cash-registers", json={"branch_id": branch_id, "initial_amount": "100.00", "notes": "Troco inicial"}
    )
    assert opened.status_code == 201, opened.text
    register = opened.json()
    assert register["status"] == "open"
    assert register["opened_by_name"]  # resolvido do usuário autenticado, nunca texto livre

    listed = c.get("/api/v1/cash-registers", params={"status": "open"})
    assert listed.status_code == 200
    assert any(r["id"] == register["id"] for r in listed.json())

    detail = c.get(f"/api/v1/cash-registers/{register['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["cash_register"]["id"] == register["id"]
    assert body["expected_cash_balance"] == "100.00"
    # todo o catálogo de métodos aparece, mesmo sem transação (item
    # "não deixar métodos de pagamento importantes hardcoded")
    methods = {t["method"] for t in body["totals_by_method"]}
    assert {"pix", "cash", "debit", "credit", "loyalty_card", "voucher", "barter", "transfer", "bank_slip"} <= methods

    closed = c.post(f"/api/v1/cash-registers/{register['id']}/close", json={"counted_amount": "95.00", "notes": "Fechamento"})
    assert closed.status_code == 200, closed.text
    closed_body = closed.json()["cash_register"]
    assert closed_body["status"] == "closed"
    assert closed_body["difference"] == "-5.00"


def test_sangria_e_suprimento_via_api(client_as, org_a_actor):
    c = client_as(org_a_actor)
    register = c.post(
        "/api/v1/cash-registers", json={"branch_id": _branch_id(org_a_actor), "initial_amount": "100.00"}
    ).json()

    supply = c.post(
        f"/api/v1/cash-registers/{register['id']}/movements",
        json={"type": "supply", "amount": "50.00", "description": "Troco adicionado"},
    )
    assert supply.status_code == 200, supply.text
    assert supply.json()["expected_cash_balance"] == "150.00"

    withdrawal = c.post(
        f"/api/v1/cash-registers/{register['id']}/movements",
        json={"type": "withdrawal", "amount": "30.00", "description": "Retirada para depósito"},
    )
    assert withdrawal.status_code == 200, withdrawal.text
    assert withdrawal.json()["expected_cash_balance"] == "120.00"


def test_nao_registra_estorno_direto_pela_rota_de_movimentacao(client_as, org_a_actor):
    c = client_as(org_a_actor)
    register = c.post(
        "/api/v1/cash-registers", json={"branch_id": _branch_id(org_a_actor), "initial_amount": "0"}
    ).json()

    resp = c.post(
        f"/api/v1/cash-registers/{register['id']}/movements",
        json={"type": "reversal", "amount": "10.00", "description": "Teste"},
    )
    assert resp.status_code == 422


def test_permissao_finance_manage_e_exigida_para_abrir_caixa(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"finance.view"})
    resp = client_as(restricted).post(
        "/api/v1/cash-registers", json={"branch_id": _branch_id(org_a_actor), "initial_amount": "0"}
    )
    assert resp.status_code == 403


def test_permissao_finance_view_e_exigida_para_listar(client_as, org_a_actor):
    restricted = _restricted_actor(org_a_actor, permissions={"finance.manage"})
    # sem finance.view mesmo tendo finance.manage — permissions são
    # independentes, uma não implica a outra.
    resp = client_as(restricted).get("/api/v1/cash-registers")
    assert resp.status_code == 403


def test_receptionist_tem_finance_view_e_finance_manage(client_as, org_a_actor):
    """Item da migration 0014: RECEPTIONIST ganha `finance.view` (pra
    conseguir selecionar um caixa ao registrar pagamento). Etapa H
    (`Financeiro > Caixa > Configurações do Caixa`, migration 0027)
    muda a regra que vinha da 0014: o pedido explícito agora é
    "permitir Recepção abrir/fechar caixa" (com um toggle próprio,
    `allow_receptionist_open_close`, padrão ON, pra quem quiser
    restringir de novo sem mexer em RBAC) — RECEPTIONIST passa a ter
    também `finance.manage` por padrão."""
    receptionist = _restricted_actor(org_a_actor, permissions={"clients.view"})
    # simula RECEPTIONIST real via seed de permissions do papel, não
    # via lista manual — usamos as permissions exatas concedidas pela
    # migration 0007+0011+0014+0027 pra esse role.
    from nexasalon_api.repositories import rbac_repo

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"),
            {"oid": str(org_a_actor.organization_id)},
        )
        role = rbac_repo.get_system_role_by_name(session, "RECEPTIONIST")
        assert role is not None
        keys = rbac_repo.list_role_permission_keys(session, role.id)

    assert "finance.view" in keys
    assert "finance.manage" in keys


def test_isolamento_multi_tenant_caixa_nao_vaza_entre_organizacoes(client_as, org_a_actor, org_b_actor):
    c_a = client_as(org_a_actor)
    register = c_a.post(
        "/api/v1/cash-registers", json={"branch_id": _branch_id(org_a_actor), "initial_amount": "0"}
    ).json()

    c_b = client_as(org_b_actor)
    resp = c_b.get(f"/api/v1/cash-registers/{register['id']}")
    assert resp.status_code == 404
