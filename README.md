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
- `0009` — dinamismo total do domínio (categorias de serviço, config de agenda por profissional/serviço, `business_type` como metadado)
- `0010` — configuração de apresentação da Agenda por unidade (`agenda_view_start/end`, `agenda_slot_minutes`)

### Rodando migrations em staging/produção

`alembic upgrade head` deve rodar com `NEXASALON_MIGRATIONS_DATABASE_URL`
apontando pro role administrativo (dono do schema), **nunca**
automaticamente no boot de cada instância da API — ver seção "Ambientes
(local/staging/produção)" mais abaixo para o fluxo completo.

## Papel de conexão da aplicação e RLS

As migrations rodam como o usuário "dono" do schema. A API **não pode**
se conectar com esse mesmo usuário em staging/produção — RLS não é
aplicado ao dono da tabela nem a superusuários, mesmo com `FORCE ROW
LEVEL SECURITY`. Crie um role de aplicação restrito antes de apontar a
API pro banco:

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

## Ambientes (local / staging / produção)

`NEXASALON_ENVIRONMENT` aceita `development`, `test`, `staging` e
`production`. `staging` tem exatamente as mesmas travas de segurança de
`production` em `core/config.py` (recusa subir com DEV ONLY ligado, JWT
secret default, cookie inseguro ou rate limit desligado) — a diferença
entre os dois é só operacional (banco, domínio, dados), nunca de
postura de segurança. `development`/`test` são os únicos ambientes onde
essas travas ficam de fora.

### Comando de start (staging/produção)

```bash
uvicorn nexasalon_api.main:app --host 0.0.0.0 --port $PORT --proxy-headers
```

- `--host 0.0.0.0`: obrigatório em qualquer PaaS — o processo precisa
  aceitar conexões do proxy do provedor, não só de `localhost`.
- `--port $PORT`: a porta nunca é fixa; o provedor injeta via env var.
- Sem `--reload` (é só para desenvolvimento local).
- `--proxy-headers`: usado só para o uvicorn confiar no `X-Forwarded-Proto`
  do Render (necessário pro FastAPI saber que a requisição original era
  HTTPS mesmo chegando por HTTP internamente) — **não** é usado para
  extrair o IP do cliente (ver próxima seção, é um mecanismo à parte e
  deliberadamente não-genérico).

### IP real do cliente e rate limiting (por trás do proxy do Render)

`api/deps.py::_client_ip` não usa a lógica padrão de "confiar nos
últimos N saltos de `X-Forwarded-For`" (a que `--forwarded-allow-ips`
do uvicorn implementa) porque o Render tem um comportamento diferente
do padrão mais comum: eles **inserem o IP real do cliente como o
primeiro elemento** de `X-Forwarded-For`, mas **não limpam** o que o
cliente já tiver mandado depois dele. Ou seja, um cliente pode forjar
`X-Forwarded-For: 1.1.1.1` e a aplicação recebe `<ip-real>, 1.1.1.1` —
confiar em qualquer posição que não seja a primeira (ou na lista
inteira) permitiria spoofing do rate limiter.

**Evidência verificada (revisão pedida explicitamente antes do
commit):** todo tráfego do Render passa por Cloudflare e depois pelo
Load Balancer da Render antes de chegar na aplicação — não existe rota
direta até o processo uvicorn. O artigo oficial ["How Render handles
DDoS attacks"](https://render.com/articles/how-render-handles-ddos-attacks)
(abr/2026), na seção "Rate limiting", usa como exemplo oficial
`req.headers['x-forwarded-for']?.split(',')[0]` — ou seja, a própria
Render instrui a confiar na primeira posição do header pra esse fim.
Isso é consistente com um tópico do fórum de feedback da Render que
descreve o mecanismo: a borda grava o IP real na posição 0 em toda
requisição, sem nunca deixar o cliente sobrescrevê-la.

**Limitação honesta, documentada por pedido explícito:** essa é
documentação de produto/comunidade da Render, não uma especificação
formal e assinada do header — a garantia depende da Render manter esse
comportamento, e não há como validar isso de dentro da aplicação. Por
isso:

- a extração é uma função pequena e explícita, não um middleware
  genérico: sempre o primeiro elemento do header, nunca outra posição,
  nunca a lista inteira, com fallback pro peer TCP direto quando o
  header não existe (dev local, testes);
- a estratégia é **configurável, não fixa no código**
  (`NEXASALON_CLIENT_IP_STRATEGY`, default `trust_first_proxy_hop`).
  Se essa suposição precisar ser revista — mudança de provedor, dúvida
  sobre a topologia atual — trocar para `socket_only` desliga
  totalmente a leitura de `X-Forwarded-For` e usa só o peer TCP direto
  (na Render, o IP do próprio Load Balancer, igual pra todas as
  requisições). É um modo deliberadamente conservador: nunca forjável,
  mas o rate limiting por IP vira, na prática, um limite global do
  serviço em vez de por cliente — não é "grátis", é o preço de não
  confiar em nenhum header;
- ao trocar de provedor de hospedagem, revisar a evidência e o valor
  default de `NEXASALON_CLIENT_IP_STRATEGY` — não é seguro reaproveitar
  sem confirmar de novo o comportamento do proxy do novo provedor.

### Bootstrap do primeiro usuário

Não existe (nem deve existir) uma rota HTTP para criar o primeiro
usuário/organização — seria uma rota de escalada de privilégio exposta
publicamente. Use o CLI administrativo, rodado manualmente, uma vez,
por ambiente:

```bash
NEXASALON_DATABASE_URL=<url-do-ambiente-alvo> python -m nexasalon_api.cli.bootstrap_owner
```

Pede organização/unidade/OWNER interativamente; a senha é lida com
`getpass` (nunca por argumento de linha de comando, nunca com default) e
recebe o mesmo hash Argon2id de qualquer outro usuário. Usa o role de
sistema `OWNER` já semeado pela migration `0007` — ver
`src/nexasalon_api/cli/bootstrap_owner.py`.

### Logging

`core/logging.py` configura `logging` pro stdout (nível via
`NEXASALON_LOG_LEVEL`) — o provedor de deploy coleta stdout como log
stream, sem precisar de nada além disso para staging. O handler global
de exceção (`main.py`) agora **loga de verdade** o erro inesperado
(`logger.exception`, com stack trace) antes de devolver o 500 genérico
ao cliente — antes da Etapa 3C essa exceção desaparecia silenciosamente.
Cada request ganha um `X-Request-Id` (gerado ou ecoado do que o cliente
mandar) que aparece nos logs de erro para correlação.

**Nunca logar**: senha, JWT (access ou refresh), o valor do cookie de
refresh, `invite_token`/`org_selection_token`, ou o header
`Authorization` inteiro — nenhum `logger.*` do projeto faz isso hoje;
manter essa regra ao adicionar logging novo em qualquer rota.

### Healthcheck e readiness

- `GET /healthz` — liveness pura, não toca banco, sempre rápida. Usada
  como Health Check Path do provedor.
- `GET /readyz` — readiness: faz um `SELECT 1` real no Postgres.
  Nenhum dos dois expõe connection string, stack trace ou qualquer dado
  interno na resposta — erros vão pro log, não pro corpo da resposta.

### Pool de conexões

`NEXASALON_DB_POOL_SIZE`/`NEXASALON_DB_MAX_OVERFLOW` (defaults 5/10,
os mesmos que o SQLAlchemy já usava implicitamente) — ajustável sem
mexer em código se o Postgres gerenciado limitar conexões simultâneas.
