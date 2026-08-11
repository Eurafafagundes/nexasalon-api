"""Garante que o mecanismo DEV ONLY é, de fato, impossível de ligar
acidentalmente em produção — as duas barreiras descritas em
core/dev_auth.py."""
import pytest
from pydantic import ValidationError

from nexasalon_api.core.config import Settings
from nexasalon_api.core import dev_auth


def test_settings_recusa_dev_auth_em_producao():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://x:y@localhost/db",
            environment="production",
            dev_auth_enabled=True,
        )


def test_settings_permite_dev_auth_fora_de_producao():
    s = Settings(
        database_url="postgresql+psycopg://x:y@localhost/db",
        environment="development",
        dev_auth_enabled=True,
    )
    assert s.dev_auth_enabled is True


def test_dependency_bloqueia_mesmo_se_chamada_direta_em_producao(monkeypatch):
    """Segunda barreira: mesmo que alguém burle a validação de Settings
    (ex.: mutação manual do objeto), a própria dependency recusa rodar."""
    monkeypatch.setattr(dev_auth.settings, "environment", "production")
    with pytest.raises(RuntimeError):
        dev_auth.get_current_actor_DEV_ONLY()


def test_dependency_funciona_normalmente_em_dev(dev_client):
    resp = dev_client.get("/api/v1/organization")
    assert resp.status_code == 200
    assert resp.json()["slug"] == dev_auth.DEV_ORG_SLUG
