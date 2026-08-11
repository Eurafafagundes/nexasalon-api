import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPKMixin


class RefreshToken(Base, UUIDPKMixin):
    """Sessão de refresh — token opaco, guardado só como hash.

    Decisão de modelagem (nova nesta etapa, não fazia parte do domínio
    aprovado até aqui): esta tabela NÃO tem Row Level Security, ao
    contrário de quase toda tabela de negócio do sistema. Ela entra na
    mesma categoria de `users`/`permissions` — infraestrutura de auth
    global, não dado de tenant isolado por RLS. A segurança dela vem de
    outro lugar: só é encontrável/usável por quem possui o token bruto
    (alta entropia, nunca armazenado, só o hash SHA-256) — o mesmo
    princípio de um link de "esqueci minha senha". Isso também resolve
    um problema de bootstrap: o fluxo de login/refresh não tem
    `app.current_org_id` setado ainda quando precisa consultar esta
    tabela (é justamente isso que ele está tentando descobrir).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_memberships.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
