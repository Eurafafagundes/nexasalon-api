import contextvars
import time

from sqlalchemy import create_engine, event
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


# ---------------------------------------------------------------------------
# Item de performance ("Fase B", B1 — baseline/instrumentação): contador de
# tempo/quantidade de queries POR REQUEST, usado pelo `ServerTimingMiddleware`
# (main.py) quando `settings.server_timing_enabled=True`. `ContextVar` (não
# uma variável de módulo comum) porque cada request async precisa do seu
# PRÓPRIO acumulador, isolado de requests concorrentes — o mesmo mecanismo já
# usado pra correlacionar `request_id` no logging estruturado. Os listeners
# ficam sempre registrados (custo desprezível: um `if get() is None: return`
# quando a instrumentação está desligada) — nunca precisam saber se estão
# "ligados", só verificam se existe um acumulador pra esta request.
# ---------------------------------------------------------------------------
query_stats_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("query_stats", default=None)


@event.listens_for(engine, "before_cursor_execute")
def _record_query_start(conn, cursor, statement, parameters, context, executemany):
    if query_stats_var.get() is not None:
        context._nexasalon_query_start = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _record_query_end(conn, cursor, statement, parameters, context, executemany):
    stats = query_stats_var.get()
    start = getattr(context, "_nexasalon_query_start", None)
    if stats is not None and start is not None:
        stats["db_time"] += time.perf_counter() - start
        stats["query_count"] += 1
