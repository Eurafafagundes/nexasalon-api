"""Informações do Estabelecimento (Etapa D) — logo + endereço + contato

Antes de escrever esta migration, `Organization` foi inspecionada (item
explícito "não duplique") — ver docstring completa em
`models/organization.py::Organization`. Resumo:

  REUSADOS (existiam, nunca lidos/escritos em código antes desta
  etapa): `name` -> nome fantasia, `document` -> CNPJ, `business_type`
  -> categoria, `phone`/`email`/`timezone` -> mesmos campos.

  NOVOS (nenhuma coluna equivalente existia): `legal_name` (razão
  social), `logo_url`, `cep`/`state`/`city`/`neighborhood`/
  `address_line`/`address_number`/`complement` (endereço — Organization
  não tinha nenhum campo de endereço), `whatsapp`, `instagram`,
  `website`.

`state` reusa o TIPO enum `brazilian_state` já criado na migration
0015 (`create_type=False` — não recria o tipo, só referencia).

Todas as colunas novas são NULLABLE sem backfill — mesmo padrão de
segurança já usado nas migrations 0015/0022 (staging tem dado real;
nenhuma coluna nova pode quebrar uma organização existente). Requisito
explícito do pedido: "campos devem ser opcionais... não impedir
operação do salão porque CNPJ/endereço não foi preenchido".

Nenhuma unicidade nova para `document`/CNPJ nesta migration — mesmo
raciocínio já documentado pra CPF de Client na 0022/`services/clients.py`:
a garantia (quando fizer sentido) fica no service layer, nunca numa
constraint de banco que poderia falhar contra dado existente.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_BRAZILIAN_STATES = [
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG",
    "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
]


def upgrade() -> None:
    op.add_column("organizations", sa.Column("legal_name", sa.String(length=255), nullable=True))
    op.add_column("organizations", sa.Column("logo_url", sa.String(length=500), nullable=True))
    op.add_column("organizations", sa.Column("cep", sa.String(length=8), nullable=True))
    op.add_column(
        "organizations",
        sa.Column(
            "state",
            postgresql.ENUM(*_BRAZILIAN_STATES, name="brazilian_state", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("organizations", sa.Column("city", sa.String(length=120), nullable=True))
    op.add_column("organizations", sa.Column("neighborhood", sa.String(length=120), nullable=True))
    op.add_column("organizations", sa.Column("address_line", sa.String(length=255), nullable=True))
    op.add_column("organizations", sa.Column("address_number", sa.String(length=20), nullable=True))
    op.add_column("organizations", sa.Column("complement", sa.String(length=120), nullable=True))
    op.add_column("organizations", sa.Column("whatsapp", sa.String(length=32), nullable=True))
    op.add_column("organizations", sa.Column("instagram", sa.String(length=120), nullable=True))
    op.add_column("organizations", sa.Column("website", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "website")
    op.drop_column("organizations", "instagram")
    op.drop_column("organizations", "whatsapp")
    op.drop_column("organizations", "complement")
    op.drop_column("organizations", "address_number")
    op.drop_column("organizations", "address_line")
    op.drop_column("organizations", "neighborhood")
    op.drop_column("organizations", "city")
    op.drop_column("organizations", "state")
    op.drop_column("organizations", "cep")
    op.drop_column("organizations", "logo_url")
    op.drop_column("organizations", "legal_name")
