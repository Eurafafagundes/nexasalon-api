"""Rate limiting para endpoints sensíveis de autenticação
(login/refresh/select-organization) — mitiga tentativa de senha em
massa e abuso de rotação de refresh token.

DECISÃO: implementação EM MEMÓRIA (por processo), de propósito —
"não precisa criar uma infraestrutura enorme agora". Isso significa uma
limitação real que deve ser documentada, não escondida: com múltiplas
réplicas da API atrás de um load balancer, cada réplica conta
separadamente (o limite efetivo vira `max_attempts * nº de réplicas`).
Suficiente para um único processo/instância; quando houver mais de uma
réplica em produção, troque `InMemoryRateLimiter` por uma implementação
com backend compartilhado (Redis é o padrão de mercado — `INCR` +
`EXPIRE`) SEM mudar a interface `RateLimiter` nem os call sites.

A trava de produção (`Settings._guard_rate_limit_enabled_in_production`)
garante que ninguém sobe em produção com `rate_limit_enabled=False` "só
por enquanto" e esquece — mesmo padrão usado para o DEV ONLY actor e
para o JWT secret default.
"""
import threading
import time
from typing import Protocol

from .exceptions import TooManyRequestsError


class RateLimiter(Protocol):
    def hit(self, key: str, *, max_attempts: int, window_seconds: int) -> None:
        """Registra uma tentativa para `key`. Levanta `TooManyRequestsError`
        se `key` já tiver `max_attempts` tentativas dentro dos últimos
        `window_seconds` segundos (janela deslizante)."""
        ...


class InMemoryRateLimiter:
    """Janela deslizante simples, com lock — as rotas do FastAPI (síncronas)
    rodam num threadpool, então isto PRECISA ser thread-safe."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, *, max_attempts: int, window_seconds: int) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            timestamps = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(timestamps) >= max_attempts:
                self._hits[key] = timestamps
                raise TooManyRequestsError(
                    "Muitas tentativas em pouco tempo. Aguarde antes de tentar de novo.",
                    details={"retry_after_seconds": round(timestamps[0] + window_seconds - now, 1)},
                )
            timestamps.append(now)
            self._hits[key] = timestamps

    def reset(self) -> None:
        """Só para testes — limpa todo o estado em memória."""
        with self._lock:
            self._hits.clear()


# Singleton do processo — todas as dependencies de rate limit (api/deps.py)
# compartilham este mesmo limitador.
rate_limiter = InMemoryRateLimiter()
