from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração da aplicação.

    `environment` e `dev_auth_enabled` existem porque autenticação real
    ainda não foi implementada (Etapa 2C). `dev_auth_enabled=True` liga
    uma dependency DEV ONLY que fabrica um usuário/organização de teste
    — ver `core/dev_auth.py`. O validator abaixo é a primeira das duas
    barreiras que impedem isso de ir parar em produção; a segunda está
    na própria dependency.
    """

    database_url: str = "postgresql+psycopg://nexasalon:nexasalon@localhost:5432/nexasalon"
    environment: Literal["development", "test", "production"] = "development"
    dev_auth_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="NEXASALON_", extra="ignore")

    @model_validator(mode="after")
    def _guard_dev_auth_never_in_production(self) -> "Settings":
        if self.environment == "production" and self.dev_auth_enabled:
            # ValueError (não RuntimeError): dentro de um @model_validator
            # do Pydantic, é isso que vira um ValidationError de verdade
            # pra quem instanciar Settings — RuntimeError passaria direto.
            raise ValueError(
                "Configuração inválida e perigosa: dev_auth_enabled=True com "
                "environment=production. A aplicação recusa iniciar. "
                "DEV ONLY nunca pode rodar em produção."
            )
        return self


settings = Settings()
