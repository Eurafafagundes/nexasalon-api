from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from nexasalon_api.core.config import settings
from nexasalon_api.models import Base

config = context.config
# Etapa 3C: migrations preferem uma conexão ADMINISTRATIVA separada
# (dono do schema), nunca o role restrito `nexasalon_app` que a API usa
# em runtime — RLS não se aplica ao dono da tabela, então rodar DDL como
# `nexasalon_app` simplesmente falharia (e não deveria "funcionar" por
# acidente via bypass). Sem `NEXASALON_MIGRATIONS_DATABASE_URL` setada,
# cai em `database_url` — comportamento local/testes inalterado.
config.set_main_option("sqlalchemy.url", settings.migrations_database_url or settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
