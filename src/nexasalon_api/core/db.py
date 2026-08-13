from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings

# pool_pre_ping evita erro de conexão "stale" após idle longo (comum em prod).
# pool_size/max_overflow configuráveis (Etapa 3C) — mesmos defaults que o
# SQLAlchemy já usava implicitamente, só expostos por env pra ajustar sem
# mexer em código quando o teto de conexões do Postgres gerenciado exigir.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
