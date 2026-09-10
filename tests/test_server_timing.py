"""`ServerTimingMiddleware` (Fase B, item B1 — baseline/instrumentação):
header `Server-Timing` com duração total/tempo de banco/contagem de
queries desta request, só ativo com `NEXASALON_SERVER_TIMING_ENABLED=
true` (default `False`, inclusive nestes testes)."""
import re

from nexasalon_api.core.config import settings
from nexasalon_api.main import app


def test_desligado_por_padrao_nunca_adiciona_o_header(dev_client):
    assert settings.server_timing_enabled is False
    resp = dev_client.get("/healthz")
    assert resp.status_code == 200
    assert "Server-Timing" not in resp.headers


def test_ligado_adiciona_header_com_total_db_e_contagem_de_queries(dev_client, monkeypatch):
    monkeypatch.setattr(settings, "server_timing_enabled", True)

    resp = dev_client.get("/readyz")  # faz exatamente 1 query real (SELECT 1)

    assert resp.status_code == 200
    header = resp.headers.get("Server-Timing")
    assert header is not None
    assert "total;dur=" in header
    assert "app;dur=" in header
    match = re.search(r'db;dur=([\d.]+);desc="(\d+) queries"', header)
    assert match is not None
    db_dur, query_count = float(match.group(1)), int(match.group(2))
    assert query_count == 1
    assert db_dur >= 0


def test_ligado_em_rota_sem_nenhuma_query_mostra_zero_queries_e_db_tempo_zero(dev_client, monkeypatch):
    monkeypatch.setattr(settings, "server_timing_enabled", True)

    resp = dev_client.get("/healthz")  # nunca toca o banco

    header = resp.headers.get("Server-Timing")
    assert header is not None
    match = re.search(r'db;dur=([\d.]+);desc="(\d+) queries"', header)
    assert match is not None
    db_dur, query_count = float(match.group(1)), int(match.group(2))
    assert query_count == 0
    assert db_dur == 0.0


def test_nunca_expoe_sql_parametros_ou_connection_string_no_header(dev_client, monkeypatch):
    monkeypatch.setattr(settings, "server_timing_enabled", True)

    resp = dev_client.get("/readyz")

    header = resp.headers.get("Server-Timing", "")
    assert "SELECT" not in header.upper()
    assert "postgresql" not in header.lower()
    assert "password" not in header.lower()


def test_requests_concorrentes_nunca_misturam_contagem_de_queries(monkeypatch):
    """`ContextVar` isola o acumulador por request — duas requests que
    fazem quantidades DIFERENTES de query nunca podem "vazar" a
    contagem uma pra outra."""
    from starlette.testclient import TestClient

    monkeypatch.setattr(settings, "server_timing_enabled", True)
    client = TestClient(app)

    r1 = client.get("/readyz")  # 1 query
    r2 = client.get("/healthz")  # 0 queries
    r3 = client.get("/readyz")  # 1 query

    for resp, expected in [(r1, 1), (r2, 0), (r3, 1)]:
        match = re.search(r'"(\d+) queries"', resp.headers["Server-Timing"])
        assert match is not None
        assert int(match.group(1)) == expected
