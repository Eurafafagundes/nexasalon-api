"""Testes de Taxas de Pagamento (Etapa N3 + N3.1, Pix) na camada de API
— permissão, isolamento entre organizações e AuditLog, que só são
exercitáveis via `TestClient` (a regra de negócio de cálculo/snapshot
já é coberta em `test_payment_fees.py`, direto no service layer)."""
import uuid

from sqlalchemy import text

from nexasalon_api.core.actor import ActorContext
from nexasalon_api.core.db import SessionLocal
from nexasalon_api.models.identity import User


def _restricted(base_actor: ActorContext, *, permissions: set[str], role_name: str = "Restrito") -> ActorContext:
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(base_actor.organization_id)}
        )
        user = User(email=f"restrito-{uuid.uuid4().hex[:8]}@nexasalon.local", name=f"Usuário {role_name}")
        session.add(user)
        session.commit()
        user_id = user.id
    return ActorContext(
        organization_id=base_actor.organization_id, user_id=user_id, membership_id=uuid.uuid4(),
        role_id=uuid.uuid4(), role_name=role_name, permissions=frozenset(permissions),
    )


def test_crud_taxa_de_pix(client_as, org_a_actor):
    c = client_as(org_a_actor)

    resp = c.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "0.99"})
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert rule["method"] == "pix"
    assert rule["card_brand"] is None  # nunca expõe o sentinela `not_applicable`.
    assert rule["installments"] == 1
    assert rule["is_active"] is True
    rule_id = rule["id"]

    resp = c.get("/api/v1/payment-fee-rules")
    assert resp.status_code == 200
    assert any(r["id"] == rule_id and r["card_brand"] is None for r in resp.json())

    # O sentinela interno `not_applicable` nunca pode vazar em NENHUMA
    # resposta que devolve a regra — cada rota abaixo revalida isso,
    # não só a de criação (achado da revisão arquitetural: `update_rule`/
    # `set_rule_active` também serializam `PaymentFeeRuleRead`, mesmo
    # caminho de risco que `create`).
    resp = c.put(f"/api/v1/payment-fee-rules/{rule_id}", json={"fee_percent": "1.20"})
    assert resp.status_code == 200
    assert resp.json()["fee_percent"] == "1.20"
    assert resp.json()["card_brand"] is None

    resp = c.patch(f"/api/v1/payment-fee-rules/{rule_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert resp.json()["card_brand"] is None

    resp = c.patch(f"/api/v1/payment-fee-rules/{rule_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    assert resp.json()["card_brand"] is None

    resp = c.patch(f"/api/v1/payment-fee-rules/{rule_id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_taxa_de_pix_rejeita_bandeira_422(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "pix", "card_brand": "visa", "fee_percent": "0.99"})
    assert resp.status_code == 422, resp.text


def test_taxa_de_pix_rejeita_parcelamento_422(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "pix", "installments": 2, "fee_percent": "0.99"})
    assert resp.status_code == 422, resp.text


def test_debito_e_credito_continuam_exigindo_bandeira(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "credit", "fee_percent": "2.99"})
    assert resp.status_code == 422, resp.text
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "debit", "fee_percent": "1.39"})
    assert resp.status_code == 422, resp.text


def test_taxa_de_pix_duplicada_e_recusada_409(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "0.99"})
    assert resp.status_code == 201, resp.text
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "1.50"})
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------
# Permissões — reaproveita `organization.manage`, nenhuma permission nova.
# ---------------------------------------------------------------------


def test_sem_organization_manage_e_bloqueado_403(client_as, org_a_actor):
    master = client_as(org_a_actor)
    resp = master.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "0.99"})
    assert resp.status_code == 201, resp.text
    rule_id = resp.json()["id"]

    restricted = client_as(_restricted(org_a_actor, permissions={"finance.view", "finance.manage"}))

    assert restricted.get("/api/v1/payment-fee-rules").status_code == 403
    assert restricted.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "1.50"}).status_code == 403
    assert restricted.put(f"/api/v1/payment-fee-rules/{rule_id}", json={"fee_percent": "1.50"}).status_code == 403
    assert restricted.patch(f"/api/v1/payment-fee-rules/{rule_id}/deactivate").status_code == 403


# ---------------------------------------------------------------------
# Isolamento entre organizações.
# ---------------------------------------------------------------------


def test_regra_de_pix_isolada_por_organizacao(client_as, org_a_actor, org_b_actor):
    # Não existe `GET /{id}` neste router (só list/create/put/activate/
    # deactivate) — o isolamento é verificado via `PUT` (que resolve por
    # id + organization_id internamente, `get_rule`) e via `GET` na
    # listagem.
    a = client_as(org_a_actor)
    resp = a.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "0.99"})
    rule_id = resp.json()["id"]

    b = client_as(org_b_actor)
    resp = b.put(f"/api/v1/payment-fee-rules/{rule_id}", json={"fee_percent": "5.00"})
    assert resp.status_code == 404  # nunca vaza a regra de uma organização pra outra.

    resp = b.patch(f"/api/v1/payment-fee-rules/{rule_id}/deactivate")
    assert resp.status_code == 404

    resp = b.get("/api/v1/payment-fee-rules")
    assert resp.status_code == 200
    assert resp.json() == []  # Org B não tem nenhuma regra própria ainda — a de A não aparece.


# ---------------------------------------------------------------------
# AuditLog.
# ---------------------------------------------------------------------


def test_mutacoes_de_taxa_de_pix_geram_audit_log(client_as, org_a_actor):
    from nexasalon_api.repositories import audit_log_repo

    c = client_as(org_a_actor)
    resp = c.post("/api/v1/payment-fee-rules", json={"method": "pix", "fee_percent": "0.99"})
    rule_id = resp.json()["id"]

    c.put(f"/api/v1/payment-fee-rules/{rule_id}", json={"fee_percent": "1.20"})
    c.patch(f"/api/v1/payment-fee-rules/{rule_id}/deactivate")
    c.patch(f"/api/v1/payment-fee-rules/{rule_id}/activate")

    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.current_org_id', :oid, false)"), {"oid": str(org_a_actor.organization_id)}
        )
        logs = audit_log_repo.list_for_entity(
            session, org_a_actor.organization_id, "payment_fee_rule", uuid.UUID(rule_id)
        )

    actions = [log.action.value for log in logs]
    assert actions == ["create", "update", "update", "update"]
    # Nenhum dado sensível (cliente/pagamento) — só configuração da própria regra.
    assert logs[0].new_values == {
        "method": "pix", "card_brand": None, "installments": "1", "fee_percent": "0.99", "is_active": True,
    }
    assert logs[-1].new_values["is_active"] is True
    assert logs[-2].new_values["is_active"] is False
