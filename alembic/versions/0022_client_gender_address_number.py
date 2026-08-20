"""Evolução do cadastro de Clientes (Etapa C.1) — gender + address_number

Antes de escrever esta migration, o modelo de `Client` foi inspecionado
e a maior parte dos campos pedidos ("nome, telefone, e-mail, nascimento,
CPF, CEP, UF, cidade, bairro, logradouro, complemento, observações") já
existia desde a migration 0015 — nada disso foi duplicado. Só faltavam
DOIS campos novos:

  - `gender`: enum novo `client_gender` (female/male/non_binary/
    prefer_not_to_say), OPCIONAL, sem nenhuma lógica de negócio
    associada (nunca requisito de cadastro nem de agendamento).
  - `address_number`: número do imóvel, `logradouro` (`address_line`)
    já existia mas era separado de número — o pedido lista os dois como
    campos distintos do formulário.

Ambas as colunas são NULLABLE sem backfill (opcionais por definição,
linhas existentes ficam corretamente com tudo NULL) — mesmo padrão de
segurança já usado pra `cpf`/endereço na 0015 (ambiente de staging já
tem dado real, nenhuma coluna nova nesta migration pode quebrar uma
linha existente).

Nenhuma constraint de unicidade nova pra CPF (item explícito do pedido
"analise antes de criar unicidade — nunca global"): ver a análise
completa na docstring de `models/client.py::Client` — a garantia de
não-duplicidade de CPF fica no service layer
(`services/clients.py::_assert_cpf_not_duplicated`), sempre escopada
por `organization_id`, não numa constraint de banco (staging já tem
dado real sem garantia de estar limpo o suficiente pra uma UNIQUE que
nunca poderia ser revertida sem apagar dado).

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_GENDER_VALUES = ["female", "male", "non_binary", "prefer_not_to_say"]


def upgrade() -> None:
    values_sql = ", ".join(f"'{v}'" for v in _GENDER_VALUES)
    op.execute(f"CREATE TYPE client_gender AS ENUM ({values_sql})")
    op.add_column(
        "clients",
        sa.Column("gender", postgresql.ENUM(*_GENDER_VALUES, name="client_gender", create_type=False), nullable=True),
    )
    op.add_column("clients", sa.Column("address_number", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "address_number")
    op.drop_column("clients", "gender")
    op.execute("DROP TYPE IF EXISTS client_gender")
