"""Verificação de identidade Google (Etapa L, Bloco 6) — "Sign in with
Google" client-side: o FRONTEND obtém um ID TOKEN via Google Identity
Services e manda só esse token pro backend. O NexaSalon NUNCA recebe
senha do Google (item explícito do pedido) — só verifica a ASSINATURA
e o `aud` (audience) do token contra `settings.google_client_id`.

`GoogleIdentityVerifier` é um Protocol (mesmo estilo leve de
`core/storage.py::StorageBackend`) especificamente para ser
TESTÁVEL/MOCKÁVEL (item explícito do pedido, teste obrigatório #12) —
`get_google_verifier()` é a dependency usada em `Depends()`, sobrescrita
em testes via `app.dependency_overrides` por um fake determinístico, sem
precisar de rede nem de um ID token real do Google pra rodar a suíte.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nexasalon_api.core.config import settings
from nexasalon_api.core.exceptions import ServiceUnavailableError, UnauthorizedError


@dataclass(frozen=True)
class GoogleIdentity:
    """Identidade já verificada — só o que o Bloco 6 pede: "obter
    identidade verificada, obter nome, obter e-mail"."""

    subject: str  # `sub` do ID token — identificador ESTÁVEL da conta Google.
    email: str
    email_verified: bool
    name: str


class GoogleIdentityVerifier(Protocol):
    def verify(self, id_token: str) -> GoogleIdentity:
        """Levanta `UnauthorizedError` se o token for inválido/expirado/
        de outra audience."""
        ...


class RealGoogleIdentityVerifier:
    """Implementação de produção — `google-auth` faz o download/cache
    das chaves públicas do Google e valida assinatura + expiração +
    `aud` num único passo."""

    def __init__(self, *, client_id: str) -> None:
        self._client_id = client_id

    def verify(self, id_token: str) -> GoogleIdentity:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        try:
            payload = google_id_token.verify_oauth2_token(
                id_token, google_requests.Request(), self._client_id
            )
        except Exception as exc:
            raise UnauthorizedError("Token do Google inválido ou expirado.") from exc

        if payload.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise UnauthorizedError("Token do Google com emissor inesperado.")

        email = payload.get("email")
        subject = payload.get("sub")
        if not email or not subject:
            raise UnauthorizedError("Token do Google sem e-mail/identificador.")

        return GoogleIdentity(
            subject=subject,
            email=email,
            email_verified=bool(payload.get("email_verified", False)),
            name=payload.get("name") or email.split("@")[0],
        )


def get_google_verifier() -> GoogleIdentityVerifier | None:
    """Dependency (`Depends(get_google_verifier)`) — `None` quando
    `NEXASALON_GOOGLE_CLIENT_ID` não está configurado (mesmo padrão de
    `core/storage.py::get_storage_backend`): a aplicação sobe
    normalmente sem login Google disponível, a rota correspondente
    responde 503 em vez de quebrar a subida inteira."""
    if not settings.google_client_id:
        return None
    return RealGoogleIdentityVerifier(client_id=settings.google_client_id)


def require_google_verifier(verifier: GoogleIdentityVerifier | None) -> GoogleIdentityVerifier:
    if verifier is None:
        raise ServiceUnavailableError("Login com Google indisponível: não configurado neste ambiente.")
    return verifier
