import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import BrazilianState, ClientGender, pg_enum


class Client(Base, UUIDPKMixin, TimestampMixin):
    """Cliente final do salão. Separado de User de propósito — não loga
    no MVP (ver Etapa 1: Área Interna vs. Agendamento Público).

    Cadastro universal pra negócio de beleza (rodada "evolução
    funcional" — Clientes/Financeiro/Caixa): `cpf`/endereço são TODOS
    opcionais (nunca obrigatórios), e `phone`/`whatsapp`/`cpf` são
    armazenados NORMALIZADOS (`core/normalize.py`, aplicado nos schemas
    de entrada) — "(61) 99999-9999", "61999999999" e "+5561999999999"
    viram o mesmo valor gravado, evitando duplicidade de cliente só por
    formatação diferente. Sem dedupe automático nesta rodada, mas a
    normalização já deixa isso possível depois sem mudar schema.
    `cliente desde`/`atendimentos`/`total gasto` NUNCA são colunas
    aqui — são sempre derivados de `Order` (ver `services/clients.py`),
    exatamente pra não duplicar fonte de verdade com o Financeiro.

    Etapa C.1 ("evolução do cadastro de clientes") acrescenta só
    `gender` e `address_number` — todo o resto do pedido (telefone,
    e-mail, nascimento, CEP, UF, cidade, bairro, logradouro,
    complemento, observações) já existia desde a migration 0015 e não
    foi duplicado. `gender` é opcional e SEM NENHUMA lógica de negócio
    associada (nunca requisito de cadastro/agendamento — ver
    `ClientGender`).

    Unicidade de CPF (item explícito "analise antes de criar regra de
    unicidade — nunca global, pessoa pode existir em orgs diferentes"):
    deliberadamente NÃO virou uma constraint de banco nesta rodada.
    `ix_clients_org_cpf` já era só um índice de BUSCA (não único) desde
    a 0015, e o ambiente de staging já tem dado real (mesma cautela
    documentada na 0015) — não há garantia de que não existam dois
    clientes com o mesmo CPF já cadastrados por engano em alguma
    organização. Adicionar uma UNIQUE(organization_id, cpf) agora
    arriscaria uma migration que nunca poderia ser aplicada sem antes
    auditar/higienizar dado de produção, o que está fora do escopo
    desta etapa. A garantia de não-duplicidade fica no SERVICE LAYER
    (`services/clients.py::_assert_cpf_not_duplicated`), sempre
    escopada por `organization_id` (nunca global) — o mesmo CPF pode
    existir em organizações diferentes sem nenhum conflito, exatamente
    como o resto do isolamento multiempresa desta base."""

    __tablename__ = "clients"
    __table_args__ = (
        Index("ix_clients_org_name", "organization_id", "name"),
        Index("ix_clients_org_phone", "organization_id", "phone"),
        Index("ix_clients_org_cpf", "organization_id", "cpf"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    whatsapp: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    birth_date: Mapped[date | None] = mapped_column(Date)
    # Opcional, sem lógica de negócio associada (ver docstring acima).
    gender: Mapped[ClientGender | None] = mapped_column(pg_enum(ClientGender, "client_gender"))
    # Informação INTERNA do estabelecimento — só existe hoje em
    # `ClientRead` (`GET/PUT /clients/*`, sempre atrás de
    # `clients.view`/`clients.manage`, nunca num endpoint público). O
    # Comprovante de Atendimento ainda não existe (é a Etapa D) — quando
    # for implementado, o schema do comprovante precisa continuar
    # excluindo este campo deliberadamente, exatamente como já é feito
    # aqui.
    notes: Mapped[str | None] = mapped_column(String)
    # CPF opcional, normalizado (só dígitos) — validado no schema de
    # entrada (`core/normalize.py::is_valid_cpf`) quando informado.
    cpf: Mapped[str | None] = mapped_column(String(11))
    cep: Mapped[str | None] = mapped_column(String(8))
    state: Mapped[BrazilianState | None] = mapped_column(pg_enum(BrazilianState, "brazilian_state"))
    city: Mapped[str | None] = mapped_column(String(120))
    neighborhood: Mapped[str | None] = mapped_column(String(120))
    address_line: Mapped[str | None] = mapped_column(String(255))
    # Número do imóvel — campo novo (Etapa C.1), separado de
    # `address_line` (logradouro) porque o pedido explicitamente lista
    # "logradouro/endereço" e "número" como dois campos distintos do
    # formulário (permite, por ex., alinhar telas de endereço com
    # outras integrações que sempre separam os dois).
    address_number: Mapped[str | None] = mapped_column(String(20))
    complement: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
