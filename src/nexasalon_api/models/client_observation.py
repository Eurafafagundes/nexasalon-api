import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPKMixin


class ClientObservation(Base, UUIDPKMixin):
    """Observação GERAL do cliente (central de Observações — item
    "Observações gerais do cadastro") — cada linha é um registro
    INDEPENDENTE (nunca sobrescreve a anterior, ao contrário do antigo
    `Client.notes` único). Observações de agendamento (`Appointment.
    notes`) e de comanda (`Order.observation`) permanecem nas tabelas de
    origem — nunca duplicadas aqui; a central de observações do cliente
    (`services/client_observations.py`) agrega as 3 fontes só na
    LEITURA, nunca grava uma cópia.

    `created_at`/`created_by`/`created_by_name` e
    `updated_at`/`updated_by`/`updated_by_name` são NULLABLE de
    propósito (não usa `TimestampMixin`, que é NOT NULL): uma linha
    nascida do backfill de `Client.notes` (`is_legacy_import=True`) não
    tem data/autor de criação conhecidos — exibir "Data de criação não
    registrada"/"Autor não registrado" só é honesto se a coluna puder
    mesmo ser `NULL`. `updated_at/by/by_name` ficam `NULL` até a
    PRIMEIRA edição de verdade (mesmo padrão de
    `Order.observation_updated_at`), nunca copiados de `created_at`.

    `version` nasce em `0` — concorrência otimista EXPLÍCITA (mesmo
    padrão de `Order.observation_version`): "nunca editada" é um valor
    real e comparável, nunca um timestamp `NULL` inicial (que não
    distingue "nunca editada" de "sem controle de versão")."""

    __tablename__ = "client_observations"
    __table_args__ = (
        Index("ix_client_observations_org_client", "organization_id", "client_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_legacy_import: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_name: Mapped[str | None] = mapped_column(String(160))
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by_name: Mapped[str | None] = mapped_column(String(160))
