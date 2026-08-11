# nexasalon-api

Backend do NexaSalon. Etapa 2C: primeira API funcional (Organization/
Branch, Professionals, Services, Clients). Agenda/Appointment ainda não
implementados. Sem autenticação real ainda.

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

## Autenticação DEV ONLY

Login real ainda não existe. Em desenvolvimento, todas as rotas usam a
dependency `get_current_actor_DEV_ONLY`
(`src/nexasalon_api/core/dev_auth.py`), que fabrica uma organização e
usuário fixos (`dev-only-do-not-use-in-production`). Duas barreiras
impedem isso de rodar em produção:

1. `Settings` recusa instanciar se `NEXASALON_ENVIRONMENT=production` e
   `NEXASALON_DEV_AUTH_ENABLED=true` ao mesmo tempo.
2. A própria dependency verifica de novo em tempo de execução.

Pra rodar localmente:

```bash
# .env
NEXASALON_ENVIRONMENT=development
NEXASALON_DEV_AUTH_ENABLED=true
```

Quando a autenticação real for implementada, o único ponto de troca é
`get_current_actor` em `api/deps.py` — nenhuma rota precisa mudar.

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
