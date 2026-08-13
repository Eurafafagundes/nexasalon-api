import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware

from nexasalon_api.api.v1.router import api_v1_router
from nexasalon_api.core.config import settings
from nexasalon_api.core.db import SessionLocal, engine
from nexasalon_api.core.exceptions import DomainError
from nexasalon_api.core.logging import configure_logging

configure_logging()
logger = logging.getLogger("nexasalon_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log de boot só com metadado não-sensível (nunca a connection
    # string, que carrega senha) — ver core/logging.py pra regra geral.
    logger.info("startup environment=%s", settings.environment)
    yield
    logger.info("shutdown")
    engine.dispose()


app = FastAPI(
    title="NexaSalon API",
    description=(
        "Backend do NexaSalon. Etapa 3C: preparo pra staging (logging, "
        "healthcheck/readiness, roles de banco separados, bootstrap "
        "administrativo). Financeiro/Estoque/Relatórios ainda não "
        "implementados."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Correlaciona cada request com um id simples — ecoa em
    `X-Request-Id` (reaproveita o do cliente se ele já mandar um) e é
    usado no log de erro inesperado abaixo, pra dar pra achar uma falha
    específica no log a partir da resposta que o cliente recebeu."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


app.add_middleware(RequestIdMiddleware)

# CORS: allowlist explícita (nunca "*"), obrigatória porque
# `allow_credentials=True` é o que permite o browser enviar o cookie
# HttpOnly do refresh token em requisições cross-origin — e os browsers
# proíbem combinar allow_credentials com Access-Control-Allow-Origin
# coringa. Quando o frontend (Next.js) estiver em outro domínio/subdomínio
# da API, o origin dele precisa estar em NEXASALON_CORS_ALLOWED_ORIGINS.
# Vazio por padrão: sem origins configuradas, nenhum browser consegue
# fazer request cross-origin autenticado — falha fechada, não aberta.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", settings.csrf_header_name],
)


@app.exception_handler(DomainError)
def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"type": exc.error_type, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "type": "validation_error",
                "message": "Dados inválidos.",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.exception_handler(IntegrityError)
def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    # Não expõe a mensagem crua do driver (pode conter nomes de tabela/
    # constraint internos) — só um 409 genérico.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": {"type": "conflict", "message": "Conflito de dados.", "details": None}},
    )


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Etapa 3C: antes, esta exceção desaparecia silenciosamente — nada
    # era logado. `logger.exception` grava tipo, mensagem e stack trace
    # no log do servidor (nunca na resposta ao cliente, que continua
    # genérica) — é assim que dá pra investigar um 500 em staging.
    request_id = getattr(request.state, "request_id", None)
    logger.exception("unhandled_exception request_id=%s path=%s", request_id, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"type": "internal_error", "message": "Erro interno.", "details": None}},
    )


@app.get("/healthz", tags=["health"], summary="Healthcheck (liveness)")
def healthz() -> dict:
    """Liveness pura — só confirma que o processo está de pé, sem tocar
    banco/dependências externas (precisa ser rápido e sempre responder,
    mesmo se o Postgres estiver fora do ar). Não expõe versão, connection
    string, nem qualquer dado interno."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"], summary="Readiness (banco alcançável)")
def readyz() -> JSONResponse:
    """Readiness — confirma que a dependência real (Postgres) responde.
    Resposta continua mínima em qualquer um dos dois casos: nunca expõe
    a connection string, mensagem de erro do driver ou stack trace."""
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readyz_db_unreachable")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ok"})


app.include_router(api_v1_router)
