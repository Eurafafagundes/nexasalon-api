from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração da aplicação. Nenhum valor sensível tem default em produção;
    o default abaixo só existe para facilitar o `alembic upgrade` local."""

    database_url: str = "postgresql+psycopg://nexasalon:nexasalon@localhost:5432/nexasalon"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
