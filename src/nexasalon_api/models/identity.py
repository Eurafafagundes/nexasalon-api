import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin
from .enums import MembershipStatus, PermissionEffect, pg_enum


class User(Base, UUIDPKMixin, TimestampMixin):
    """Identidade global da Área Interna — cross-tenant. Um User pode ter
    várias OrganizationMembership, uma por organização. Login por e-mail.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    # placeholder — sem autenticação real implementada nesta etapa.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    memberships: Mapped[list["OrganizationMembership"]] = relationship(back_populates="user")


class OrganizationMembership(Base, UUIDPKMixin, TimestampMixin):
    """User -> OrganizationMembership -> Organization, com role e status
    por organização.

    Ajuste aprovado: esta tabela NÃO guarda `professional_id`. A única FK
    canônica do vínculo profissional/login é `Professional.user_id`
    (ver professional.py). `professional` abaixo é uma relationship
    viewonly (sem coluna própria) que casa por (organization_id, user_id)
    — dá acesso de conveniência em código sem duplicar a fonte de verdade.
    """

    __tablename__ = "organization_memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="SET NULL")
    )
    status: Mapped[MembershipStatus] = mapped_column(
        pg_enum(MembershipStatus, "membership_status"),
        nullable=False,
        server_default=MembershipStatus.INVITED.value,
    )

    user: Mapped["User"] = relationship(back_populates="memberships")
    role: Mapped["Role"] = relationship()  # noqa: F821 (importado via models/__init__)

    professional: Mapped["Professional | None"] = relationship(  # noqa: F821
        primaryjoin=(
            "and_(OrganizationMembership.organization_id == foreign(Professional.organization_id), "
            "OrganizationMembership.user_id == foreign(Professional.user_id))"
        ),
        viewonly=True,
        uselist=False,
    )

    permission_overrides: Mapped[list["MembershipPermissionOverride"]] = relationship(
        back_populates="membership"
    )


class MembershipPermissionOverride(Base):
    """Grant/deny pontual de uma permissão específica, além do que o Role
    já concede — substitui um campo solto `permissions` (jsonb) na
    membership, que quebraria integridade referencial com o catálogo de
    Permission e seria difícil de auditar."""

    __tablename__ = "membership_permission_overrides"

    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_memberships.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )
    effect: Mapped[PermissionEffect] = mapped_column(
        pg_enum(PermissionEffect, "permission_effect"),
        nullable=False,
    )

    membership: Mapped["OrganizationMembership"] = relationship(back_populates="permission_overrides")
