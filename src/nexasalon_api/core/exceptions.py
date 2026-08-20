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


class UnauthorizedError(DomainError):
    """Credenciais ausentes, inválidas, expiradas ou token revogado —
    quem está pedindo não está autenticado."""

    status_code = 401
    error_type = "unauthorized"


class ForbiddenError(DomainError):
    """Autenticado, mas sem a permissão/role necessária para a ação —
    ou membership inativa/removida na organização atual."""

    status_code = 403
    error_type = "forbidden"


class TooManyRequestsError(DomainError):
    """Limite de tentativas excedido num endpoint sensível (login,
    refresh, seleção de organização) — ver `core/rate_limit.py`."""

    status_code = 429
    error_type = "rate_limited"


class ServiceUnavailableError(DomainError):
    """Dependência externa opcional não configurada neste ambiente —
    ex.: upload de logo (Etapa D) quando nenhum storage foi
    configurado. Diferente de `ValidationDomainError`: não é o cliente
    que errou o payload, é o ambiente que não tem o recurso ligado."""

    status_code = 503
    error_type = "service_unavailable"
