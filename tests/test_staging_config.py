"""Etapa 3C — `environment=staging` deve ter exatamente as mesmas
travas de segurança de `production`. Cada teste isola UM guard por vez
(valores "seguros" explícitos nos outros campos), porque os
`model_validator(mode="after")` do Pydantic param na primeira falha —
sem isolar, um teste poderia "passar" pelo motivo errado."""
import pytest
from pydantic import ValidationError

from nexasalon_api.core.config import Settings, _INSECURE_DEFAULT_JWT_SECRET

_SAFE_OVERRIDES = dict(
    jwt_secret="um-segredo-forte-e-unico-soh-para-teste",
    refresh_cookie_secure=True,
    rate_limit_enabled=True,
    dev_auth_enabled=False,
)


def _settings(environment: str, **overrides) -> Settings:
    kwargs = {**_SAFE_OVERRIDES, **overrides}
    return Settings(environment=environment, **kwargs)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_recusa_dev_auth_ligado(environment):
    with pytest.raises(ValidationError, match="DEV ONLY"):
        _settings(environment, dev_auth_enabled=True)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_recusa_jwt_secret_default(environment):
    with pytest.raises(ValidationError, match="NEXASALON_JWT_SECRET"):
        _settings(environment, jwt_secret=_INSECURE_DEFAULT_JWT_SECRET)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_recusa_cookie_inseguro(environment):
    with pytest.raises(ValidationError, match="REFRESH_COOKIE_SECURE"):
        _settings(environment, refresh_cookie_secure=False)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_recusa_rate_limit_desligado(environment):
    with pytest.raises(ValidationError, match="RATE_LIMIT_ENABLED"):
        _settings(environment, rate_limit_enabled=False)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_configuracao_segura_sobe_normalmente(environment):
    settings = _settings(environment)
    assert settings.environment == environment


@pytest.mark.parametrize("environment", ["development", "test"])
def test_development_e_test_nao_tem_essas_travas(environment):
    # dev_auth ligado, cookie inseguro, secret default, rate limit
    # desligado — tudo isso é exatamente o que development/test
    # legitimamente usam (ver tests/conftest.py). Não pode virar erro.
    settings = Settings(
        environment=environment,
        dev_auth_enabled=True,
        jwt_secret=_INSECURE_DEFAULT_JWT_SECRET,
        refresh_cookie_secure=False,
        rate_limit_enabled=False,
    )
    assert settings.environment == environment


def test_migrations_database_url_e_opcional_e_none_por_default():
    settings = _settings("development")
    assert settings.migrations_database_url is None
