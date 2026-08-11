"""Exceções de domínio e tratamento padronizado de erros.

Toda resposta de erro da API segue o mesmo formato:

    {"error": {"type": "not_found", "message": "...", "details": null}}

Rotas e a camada de serviço levantam as exceções de domínio abaixo — elas
nunca formatam a resposta HTTP diretamente. Os handlers registrados em
`main.py` fazem essa tradução num único lugar.
"""
from typing import Any


class DomainError(Exception):
    """Base de todas as exceções de domínio da aplicação."""

    status_code = 500
    error_type = "internal_error"

    def __init__(self, message: str, details: Any = None) -> None:
        self.message = message
        self.details = details
        super().__init__(message)


class NotFoundError(DomainError):
    status_code = 404
    error_type = "not_found"


class ConflictError(DomainError):
    status_code = 409
    error_type = "conflict"


class ValidationDomainError(DomainError):
    """Erro de regra de negócio (não confundir com validação de schema do
    Pydantic, que o próprio FastAPI já trata)."""

    status_code = 422
    error_type = "validation_error"
