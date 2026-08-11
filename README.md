# nexasalon-api

Backend do NexaSalon. Etapa 2D: autenticação real (JWT + refresh token
em cookie HttpOnly), RBAC completo (inclusive nas rotas herdadas da
Etapa 2C), gestão de usuários/memberships com fluxo de convite, rate
limiting nos endpoints sensíveis de auth. Agenda/Appointment/Financeiro
ainda não implementados. Frontend Next.js ainda não conectado.

## Stack

Python · FastAPI · PostgreSQL · SQLAlchemy 2.x · Alembic · Pydantic

## Instalação local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # ajuste NEXASALON_DATABASE_URL para o seu Postgres
```

## Migrations

```bash
alembic upgrade head
```

- `0001` — extensões/enums nativos do Postgres
- `0002` — schema completo (20 tabelas)
- `0003` — Row Level Security (isolamento multi-tenant)
- `0004` — triggers de integridade da agenda (overlap com advisory lock + cache de bounds do Appointment)
- `0005` — tabela `refresh_tokens` (SEM Row Level Security — infraestrutura global de auth, segura por posse do token, não por tenant; ver docstring da migration)
- `0006` — amplia a policy RLS de `organization_memberships` com auto-acesso via `app.current_user_id`, pra permitir listar as próprias memberships entre organizações no login (antes de qualquer `app.current_org_id` existir)
- `0007` — seed do catálogo de permissions (20 chaves, granularidade `*.view`/`*.manage` por recurso) e dos 4 roles de sistema (OWNER/ADMIN/RECEPTIONIST/PROFESSIONAL)
- `0008` — corrige todas as policies RLS do projeto para usar `NULLIF(current_setting(...), '')::uuid` em vez do cast direto — parâmetros de sessão customizados do Postgres voltam pra string vazia (não `NULL`) depois do primeiro commit numa conexão reaproveitada por pool, o que quebrava algumas queries de auth com erro 500 não-determinístico antes desta correção

## Papel de conexão da aplicação e RLS

As migrations rodam como o usuário "dono" do schema. A API **não pode**
se conectar com esse mesmo usuário em produção — RLS não é aplicado ao
dono da tabela nem a superusuários, mesmo com `FORCE ROW LEVEL SECURITY`.
Crie um role de aplicação restrito antes de apontar a API pro banco:

```sql
CREATE ROLE nexasalon_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
GRANT ALL ON ALL TABLES IN SCHEMA public TO nexasalon_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO nexasalon_app;
```

`NEXASALON_DATABASE_URL` deve apontar para esse role, não para o dono
das migrations. A cada request, a dependency `get_db`
(`src/nexasalon_api/api/deps.py`) roda
`SELECT set_config('app.current_org_id', '<org>', true)` — `true` no
terceiro argumento é importante: escopa ao `SET LOCAL` da transação da
própria request, nunca vaza pra outra request que reuse a mesma conexão
do pool.

RLS é a **segunda** barreira, não a única: todo repository em
`src/nexasalon_api/repositories/` também filtra `organization_id`
explicitamente nas queries, independente do RLS estar ativo.

## Autenticação

### Estratégia de tokens

- **Access token**: JWT (HS256), vida curta (`NEXASALON_ACCESS_TOKEN_TTL_MINUTES`,
  padrão 15min), claims mínimos (`sub`, `org_id`, `membership_id`,
  `type`, `jti`, `exp`) — **nunca** carrega senha, role ou permissions.
  Vai no corpo JSON da resposta de login; o frontend guarda em memória e
  envia via `Authorization: Bearer <token>`.
- **Refresh token**: opaco (alta entropia, `secrets.token_urlsafe(48)`),
  guardado no banco só como hash SHA-256, com **rotação a cada uso** e
  **detecção de reuso** (um token já rotacionado sendo reapresentado
  revoga TODOS os refresh tokens daquele usuário — resposta a um
  possível roubo). **Nunca** aparece em JSON — é setado como cookie
  `HttpOnly` + `Secure` + `SameSite` (`core/config.py`,
  `NEXASALON_REFRESH_COOKIE_*`), inacessível a qualquer JavaScript do
  frontend.

Role/permissions nunca ficam no token: são recalculadas do banco a cada
request (`services/auth.compute_effective_permissions`), então desativar
uma membership corta o acesso imediatamente, mesmo com um access token
ainda dentro do prazo.

### CORS / CSRF (frontend e API em domínios distintos)

- `NEXASALON_CORS_ALLOWED_ORIGINS` é uma allowlist explícita de origins
  (nunca `*`) — obrigatória porque `allow_credentials=True` (necessário
  pro cookie do refresh token viajar cross-origin) e origin coringa são
  combinações que o browser recusa.
- Se frontend e API ficarem em domínios totalmente diferentes, o cookie
  só é enviado cross-site com `NEXASALON_REFRESH_COOKIE_SAMESITE=none`
  (e `Secure=true` junto, exigência do browser). Se ficarem no mesmo
  domínio-pai (`app.nexasalon.com` + `api.nexasalon.com`),
  `SameSite=Lax` já basta e é mais seguro — prefira essa topologia.
- Um cookie `SameSite=None` é enviado automaticamente em QUALQUER
  request cross-site (inclusive de um site malicioso) — por isso
  `POST /auth/refresh` e `POST /auth/logout` exigem o header
  `X-NexaSalon-Csrf` (nome configurável via `NEXASALON_CSRF_HEADER_NAME`).
  Um `<form>` cross-site não consegue adicionar headers customizados; um
  `fetch`/XHR cross-site cairia no preflight de CORS, barrado pela
  allowlist. `/auth/login` não precisa disso — não depende de nenhum
  cookie ambiente.

### RBAC

Catálogo fixo de 20 permissions (`*.view`/`*.manage` por recurso +
`agenda.*`/`finance.*`/`reports.view`/`settings.manage`/`users.manage`/
`organization.manage`), 4 roles de sistema (seed na migration `0007`).
`require_permission("chave")` (`api/deps.py`) é a dependency que qualquer
rota usa pra exigir uma permission — já aplicada em todas as rotas de
recurso (`branches`, `professionals`, `services`, `clients`, `users`), não
só nas novas de auth. O backend é sempre a autoridade final; esconder um
botão no frontend não substitui essa checagem.

### Convite de usuário

O administrador nunca cria nem vê a senha de um funcionário:

```
POST /users (users.manage)
    -> User/Membership entram como INVITED
    -> gera invite_token (JWT, NEXASALON_INVITE_TOKEN_TTL_DAYS, padrão 7 dias)
    -> funcionário abre o link, define a PRÓPRIA senha
    -> POST /auth/accept-invite -> membership vira ACTIVE, sessão já logada
```

> **Nota (temporário, só para desenvolvimento):** hoje `POST /users` e
> `POST /users/{id}/resend-invite` retornam `invite_token` diretamente
> na resposta HTTP, pro administrador copiar/repassar manualmente
> (WhatsApp, e-mail manual etc.), já que não existe serviço de envio de
> e-mail integrado ainda. **Isso não deve ir para produção como está**:
> quando o serviço de e-mail existir, o token deixa de ser devolvido no
> corpo da resposta (nunca deve ficar exposto a um frontend
> administrativo comum) e passa a ser enviado exclusivamente pelo canal
> de convite (e-mail) diretamente ao usuário convidado.

### Rate limiting

`core/rate_limit.py` protege `/auth/login`, `/auth/refresh` e
`/auth/select-organization` contra tentativa de senha em massa — janela
deslizante por IP de origem, limites configuráveis
(`NEXASALON_RATE_LIMIT_*`). `Settings` recusa subir em produção com
`NEXASALON_RATE_LIMIT_ENABLED=false` (mesmo padrão de guard usado pro
DEV ONLY actor e pro JWT secret default).

> **Limitação conhecida:** a implementação atual (`InMemoryRateLimiter`)
> é **em memória, por processo** — funciona para uma única instância da
> API. **Com múltiplas réplicas atrás de um load balancer, cada réplica
> conta separadamente**, e o limite efetivo vira `max_attempts × nº de
> réplicas` (na prática, mais permissivo do que o configurado). Antes de
> rodar a API com mais de uma réplica em produção, troque
> `InMemoryRateLimiter` por uma implementação com backend compartilhado
> — Redis (`INCR` + `EXPIRE`) é o padrão de mercado para isso — mantendo
> a mesma interface `RateLimiter` (`core/rate_limit.py`), sem precisar
> mudar nenhum call site (`api/deps.py`).

### Modo DEV ONLY

Mantido só para desenvolvimento/testes locais, como uma alternativa ao
login real. `get_current_actor` (`api/deps.py`) decide em runtime entre
o caminho real e `get_current_actor_DEV_ONLY`
(`src/nexasalon_api/core/dev_auth.py`, que fabrica uma organização e
usuário fixos `dev-only-do-not-use-in-production`, com o role "Dev
Owner" recebendo todas as permissions do catálogo). Três barreiras
impedem isso de rodar em produção:

1. `Settings` recusa instanciar se `NEXASALON_ENVIRONMENT=production` e
   `NEXASALON_DEV_AUTH_ENABLED=true` ao mesmo tempo.
2. A própria dependency verifica de novo em tempo de execução.
3. `get_current_actor` só cai nesse caminho se `dev_auth_enabled=True`
   E `environment != production` — nunca é o padrão.

Pra rodar localmente:

```bash
# .env
NEXASALON_ENVIRONMENT=development
NEXASALON_DEV_AUTH_ENABLED=true
```

## Rodando a API

```bash
uvicorn nexasalon_api.main:app --reload
```

Documentação interativa (Swagger) em `/docs`, OpenAPI JSON em `/openapi.json`.

## Testes

```bash
pytest
```

Sobe um Postgres descartável (via `pgserver`, embutido — não precisa de
Docker nem de um Postgres já rodando), roda as migrations, cria o role
`nexasalon_app` e testa a API real através dele — os testes de
isolamento multi-tenant só valem alguma coisa rodando dessa forma (não
como superusuário).
