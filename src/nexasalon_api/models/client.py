import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import BrazilianState, pg_enum


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
    exatamente pra não duplicar fonte de verdade com o Financeiro."""

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
    notes: Mapped[str | None] = mapped_column(String)
    # CPF opcional, normalizado (só dígitos) — validado no schema de
    # entrada (`core/normalize.py::is_valid_cpf`) quando informado.
    cpf: Mapped[str | None] = mapped_column(String(11))
    cep: Mapped[str | None] = mapped_column(String(8))
    state: Mapped[BrazilianState | None] = mapped_column(pg_enum(BrazilianState, "brazilian_state"))
    city: Mapped[str | None] = mapped_column(String(120))
    neighborhood: Mapped[str | None] = mapped_column(String(120))
    address_line: Mapped[str | None] = mapped_column(String(255))
    complement: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
