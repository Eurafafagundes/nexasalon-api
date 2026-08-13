"""Etapa 3C — /healthz (liveness) e /readyz (readiness, toca o banco).
Nenhum dos dois pode vazar connection string, stack trace ou qualquer
dado interno na resposta."""


def test_healthz_nao_toca_banco_e_nao_vaza_nada(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_confirma_banco_alcancavel(client_as, org_a_actor):
    c = client_as(org_a_actor)
    resp = c.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    body_text = resp.text.lower()
    assert "postgresql" not in body_text
    assert "password" not in body_text
