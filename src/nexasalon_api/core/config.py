from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-troque-isso"


class Settings(BaseSettings):
    """Configuração da aplicação.

    `environment` e `dev_auth_enabled` existem porque autenticação real
    (Etapa 2D) coexiste com o modo DEV ONLY usado só em testes locais —
    `dev_auth_enabled=True` liga uma dependency que fabrica um usuário/
    organização fixos, ver `core/dev_auth.py`. Os validators abaixo são
    a primeira de duas barreiras contra isso (e contra o JWT secret
    default) ir parar em produção; a segunda está nas próprias
    dependencies (`core/dev_auth.py`, `core/security.py`).
    """

    database_url: str = "postgresql+psycopg://nexasalon:nexasalon@localhost:5432/nexasalon"
    # Conexão SEPARADA usada só por `alembic/env.py` (migrations/DDL) —
    # deve apontar pro role dono do schema, nunca pro `nexasalon_app`
    # restrito. Quando ausente, cai em `database_url` (comportamento
    # local/testes, onde os dois papéis costumam ser o mesmo usuário
    # "postgres" do banco descartável). Nunca lida pelo app em runtime,
    # só pelo Alembic.
    migrations_database_url: str | None = None
    # "staging" existe desde a Etapa 3C: mesmo nível de rigor de
    # "production" nos guards abaixo (nunca DEV ONLY, nunca segredo
    # default, sempre cookie seguro, sempre rate limit ligado) — a
    # diferença entre os dois é só operacional (branch, domínio, dados),
    # nunca de postura de segurança.
    environment: Literal["development", "test", "staging", "production"] = "development"
    dev_auth_enabled: bool = False

    jwt_secret: str = _INSECURE_DEFAULT_JWT_SECRET
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    org_selection_token_ttl_minutes: int = 5
    invite_token_ttl_days: int = 7

    # --- Pool de conexões (SQLAlchemy) ---
    # Defaults iguais aos que o SQLAlchemy já usava implicitamente (5 +
    # 10 overflow) — só tornados configuráveis por env, sem mudar
    # comportamento local/testes. Bancos gerenciados (ex.: Neon free
    # tier) costumam ter um teto de conexões simultâneas mais apertado
    # do que um Postgres próprio; isto existe pra dar controle sem
    # precisar mexer em código quando isso importar.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Nível de log (`core/logging.py`). Nunca logamos senha, JWT, cookie,
    # invite_token ou o header Authorization inteiro em nenhum nível.
    log_level: str = "INFO"

    # --- Resolução do IP real do cliente (rate limiting), Etapa 3C ---
    # Ver docstring de `api/deps.py::_client_ip` para o raciocínio
    # completo. Resumo: isto é uma escolha por PROVIDER, não uma verdade
    # universal sobre `X-Forwarded-For` — por isso é configurável em vez
    # de hardcoded, e o valor default só é apropriado enquanto a
    # topologia for "Cloudflare → Load Balancer da Render → app" (a
    # atual). Se o provider mudar, ou se a suposição abaixo precisar ser
    # revista, troque via env sem precisar mexer em código.
    #
    # - "trust_first_proxy_hop": usa o primeiro elemento de
    #   `X-Forwarded-For`. Documentação oficial da Render (artigo "How
    #   Render handles DDoS attacks", abr/2026) e um tópico do fórum de
    #   feedback da própria Render descrevem que a borda (Cloudflare +
    #   Load Balancer da Render) grava o IP real do cliente na primeira
    #   posição do header em toda requisição, sem jamais permitir que o
    #   cliente sobrescreva essa posição — só o que vem DEPOIS da
    #   primeira posição pode ser forjado (a Render nunca limpa o que o
    #   cliente já tinha mandado, só acrescenta). Por isso confiamos
    #   SOMENTE na posição 0, nunca na última nem em "N hops a partir da
    #   direita" (esse padrão genérico, usado por `--forwarded-allow-ips`
    #   do uvicorn/gunicorn, pressupõe que cada proxy ACRESCENTA no fim —
    #   o oposto do que a Render faz — e seria explorável aqui).
    #   Confiança: alta, mas baseada em documentação de produto/comunidade
    #   da Render, não numa especificação formal e assinada
    #   criptograficamente do header — ou seja, depende da Render manter
    #   esse comportamento.
    # - "socket_only": ignora `X-Forwarded-For` por completo e usa
    #   `request.client.host` (o peer TCP direto — em Render, o IP do
    #   próprio Load Balancer, igual pra todas as requisições, não o
    #   cliente final). Modo conservador: nunca pode ser forjado, mas
    #   também não distingue clientes entre si atrás do mesmo proxy —
    #   rate limiting por IP vira, na prática, rate limiting global do
    #   serviço. Usar se a suposição acima for contestada/invalidada.
    client_ip_strategy: Literal["trust_first_proxy_hop", "socket_only"] = "trust_first_proxy_hop"

    # --- Transporte do refresh token (cookie) e CORS/CSRF ---
    # Decisão da Etapa 2D: access token no corpo JSON (Bearer, memória do
    # frontend); refresh token NUNCA no corpo/JS — só como cookie
    # HttpOnly. `refresh_cookie_path` restringe o cookie só às rotas de
    # auth (não é enviado em toda requisição de recurso, à toa).
    # `cors_allowed_origins` é uma allowlist explícita (nunca "*") porque
    # `allow_credentials=True` é obrigatório pra cookies cross-site
    # funcionarem, e o browser proíbe combinar isso com origin coringa.
    refresh_cookie_name: str = "nexasalon_refresh_token"
    refresh_cookie_path: str = "/api/v1/auth"
    refresh_cookie_domain: str | None = None
    refresh_cookie_secure: bool = True
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_header_name: str = "X-NexaSalon-Csrf"
    cors_allowed_origins: Annotated[list[str], NoDecode] = []

    # --- Rate limiting (endpoints sensíveis de auth) ---
    rate_limit_enabled: bool = True
    rate_limit_login_max_attempts: int = 10
    rate_limit_login_window_seconds: int = 300
    rate_limit_refresh_max_attempts: int = 30
    rate_limit_refresh_window_seconds: int = 300
    rate_limit_select_organization_max_attempts: int = 20
    rate_limit_select_organization_window_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", env_prefix="NEXASALON_", extra="ignore")

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value):
        # `NoDecode` acima desliga o parsing JSON automático que o
        # pydantic-settings tenta pra campos list[str] — sem isso, um
        # valor comma-separated simples (`NEXASALON_CORS_ALLOWED_ORIGINS=
        # https://a.com,https://b.com`) quebraria com JSONDecodeError
        # antes mesmo de chegar neste validator.
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _guard_dev_auth_never_in_production(self) -> "Settings":
        # staging entra na mesma trava que production: é um ambiente
        # real, alcançável pela internet, com dados de teste que ainda
        # assim não devem ficar expostos por um bypass de autenticação.
        if self.environment in ("staging", "production") and self.dev_auth_enabled:
            # ValueError (não RuntimeError): dentro de um @model_validator
            # do Pydantic, é isso que vira um ValidationError de verdade
            # pra quem instanciar Settings — RuntimeError passaria direto.
            raise ValueError(
                "Configuração inválida e perigosa: dev_auth_enabled=True com "
                f"environment={self.environment}. A aplicação recusa iniciar. "
                "DEV ONLY nunca pode rodar em staging/produção."
            )
        return self

    @model_validator(mode="after")
    def _guard_jwt_secret_never_default_in_production(self) -> "Settings":
        if self.environment in ("staging", "production") and self.jwt_secret == _INSECURE_DEFAULT_JWT_SECRET:
            raise ValueError(
                f"NEXASALON_JWT_SECRET não pode ficar no valor default em {self.environment}. "
                "Defina um segredo forte e único no ambiente."
            )
        return self

    @model_validator(mode="after")
    def _guard_refresh_cookie_secure_in_production(self) -> "Settings":
        if self.environment in ("staging", "production") and not self.refresh_cookie_secure:
            raise ValueError(
                f"NEXASALON_REFRESH_COOKIE_SECURE não pode ser false em {self.environment} — "
                "o cookie do refresh token precisa do atributo Secure (HTTPS)."
            )
        return self

    @model_validator(mode="after")
    def _guard_samesite_none_requires_secure(self) -> "Settings":
        # Regra dos browsers, não só nossa: um cookie SameSite=None sem
        # Secure é rejeitado silenciosamente (Chrome/Firefox recentes).
        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError(
                "refresh_cookie_samesite='none' exige refresh_cookie_secure=true "
                "(exigência dos browsers para cookies cross-site)."
            )
        return self

    @model_validator(mode="after")
    def _guard_rate_limit_enabled_in_production(self) -> "Settings":
        if self.environment in ("staging", "production") and not self.rate_limit_enabled:
            raise ValueError(
                f"NEXASALON_RATE_LIMIT_ENABLED não pode ser false em {self.environment} — "
                "login/refresh/select-organization ficariam sem proteção contra força bruta."
            )
        return self


settings = Settings()
