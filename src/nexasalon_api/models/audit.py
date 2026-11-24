import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import AuditAction, pg_enum


class AuditLog(Base):
    """Append-only: sem updated_at, sem UPDATE/DELETE permitido pela
    aplicação. Usa BigInteger (não UUID) de propósito — alto volume,
    sequencial, facilita particionamento futuro por created_at."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Snapshot do nome do usuário NO MOMENTO da ação — nunca um join a
    # `users.name` (renomear ou desativar o usuário depois não pode
    # mudar retroativamente o nome mostrado no histórico). Mesmo padrão
    # de `Order.observation_updated_by_name`. Nullable: registros
    # anteriores a esta coluna (migration 0058) nunca são preenchidos
    # retroativamente — "Autor não registrado" é o estado honesto pra
    # eles, nunca um valor inventado.
    user_name: Mapped[str | None] = mapped_column(String(160))
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False
    )
    old_values: Mapped[dict | None] = mapped_column(JSONB)
    new_values: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
