from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from nexasalon_api.api.v1.router import api_v1_router
from nexasalon_api.core.exceptions import DomainError

app = FastAPI(
    title="NexaSalon API",
    description=(
        "Backend do NexaSalon. Etapa 2C: primeira API funcional "
        "(Organization/Branch, Professionals, Services, Clients). "
        "Agenda/Appointment ainda não implementados."
    ),
    version="0.1.0",
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
    # Nunca expõe mensagem/stack trace da exceção original ao cliente —
    # só um 500 genérico. Detalhe real fica pro log do servidor (a
    # observabilidade de verdade é assunto de outra etapa).
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"type": "internal_error", "message": "Erro interno.", "details": None}},
    )


@app.get("/healthz", tags=["health"], summary="Healthcheck")
def healthz() -> dict:
    return {"status": "ok"}


app.include_router(api_v1_router)
