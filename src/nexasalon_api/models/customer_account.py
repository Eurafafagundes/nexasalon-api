"""Identidade da CLIENTE FINAL do Agendamento Online (Etapa L, Blocos
5-8) — deliberadamente separada de `User`/`OrganizationMembership`
(models/identity.py). `CustomerAccount` é uma pessoa que reserva
horários pelo link público; `User` é um FUNCIONÁRIO com acesso à área
interna. Misturar as duas seria misturar dois papéis de segurança
diferentes — uma CustomerAccount jamais deve poder acessar rotas
internas de Agenda/Financeiro/Estoque/RBAC, mesmo por engano (ver
`api/deps.py::get_current_customer`, que só aceita tokens com
`type=customer_access`, nunca `type=access`, e vice-versa).

`CustomerAccount` é GLOBAL (cross-tenant, sem `organization_id` e sem
RLS — mesmo raciocínio de `User`): a MESMA pessoa pode existir em várias
Organizations (frequenta o Salão A e o Salão B), então a identidade não
pode ser presa a uma organização. O vínculo com o `Client` específico de
CADA organização é modelado à parte, em `CustomerAccountLink`."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UUIDPKMixin


class CustomerAccount(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "customer_accounts"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Sempre normalizado para minúsculas por `services/customer_accounts.py`
    # antes de gravar/consultar — Postgres não tem unicidade
    # case-insensitive nativa sem extensão adicional (citext), e não
    # queremos depender de uma extensão nova só para isto.
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    # Nulo quando a conta é só-Google (nunca teve senha própria).
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Nulo quando a conta nunca autenticou via Google. Único quando presente.
    google_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class CustomerRefreshToken(Base, UUIDPKMixin):
    """Sessão de refresh da CLIENTE final (ajuste pós-Etapa L: persistir
    a sessão com segurança em vez de um `access_token` de 30 dias vivendo
    só em memória no frontend, perdido a cada reload).

    Mesma TÉCNICA de `models/auth.py::RefreshToken` (o refresh token de
    FUNCIONÁRIO): token opaco de alta entropia, guardado só como hash
    SHA-256, rotacionado a cada uso, com detecção de reuso (ver
    `services/customer_accounts.py::refresh_session`). Mas é uma tabela
    TOTALMENTE separada — nenhuma linha, FK ou cookie é compartilhado
    entre as duas (Bloco 7: "não misturar cliente com funcionário"). Sem
    `organization_id`/`membership_id` (CustomerAccount é global, sem
    organização própria — ver docstring de `CustomerAccount`). Sem RLS,
    pelo mesmo raciocínio de `RefreshToken`: segurança vem da posse do
    token bruto (nunca armazenado), não de isolamento por tenant."""

    __tablename__ = "customer_refresh_tokens"

    customer_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_refresh_tokens.id", ondelete="SET NULL")
    )


class CustomerAccountLink(Base, UUIDPKMixin):
    """Vínculo CustomerAccount <-> Client, POR Organization — resolvido
    uma única vez (primeira reserva bem-sucedida naquela organização) e
    reaproveitado em todas as reservas seguintes (Bloco 8: "não criar
    outro Client a cada Agendamento Online"). `organization_id` próprio
    -> RLS de tenant igual a qualquer outra tabela direta (ver migration
    0031)."""

    __tablename__ = "customer_account_links"
    __table_args__ = (UniqueConstraint("customer_account_id", "organization_id"),)

    customer_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_accounts.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
