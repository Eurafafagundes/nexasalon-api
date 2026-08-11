# nexasalon-api

Backend do NexaSalon — Etapa 2B: modelos de domínio (SQLAlchemy 2.x) e
migrations (Alembic). Sem endpoints ainda.

## Stack

Python · FastAPI (instalado, não usado ainda) · PostgreSQL · SQLAlchemy 2.x · Alembic · Pydantic

## Rodando as migrations

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # ajuste DATABASE_URL para o seu Postgres
alembic upgrade head
```

## Migrations

- `0001` — extensões/enums nativos do Postgres
- `0002` — schema completo (20 tabelas, autogenerado a partir dos models e revisado à mão)
- `0003` — Row Level Security (isolamento multi-tenant)
- `0004` — triggers de integridade da agenda (overlap com advisory lock + cache de bounds do Appointment)

## Papel de conexão da aplicação

As migrations rodam como o usuário "dono" do schema. A API **não pode**
se conectar com esse mesmo usuário em produção — RLS não é aplicado ao
dono da tabela nem a superusuários, mesmo com `FORCE ROW LEVEL SECURITY`.
Crie um role de aplicação restrito antes de apontar a API pro banco:

```sql
CREATE ROLE nexasalon_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
GRANT ALL ON ALL TABLES IN SCHEMA public TO nexasalon_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO nexasalon_app;
```

E a API deve, no início de cada transação, setar o contexto do tenant:

```sql
SELECT set_config('app.current_org_id', '<uuid da organização>', false);
```
