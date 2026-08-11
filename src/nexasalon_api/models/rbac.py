import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin


class Role(Base, UUIDPKMixin, TimestampMixin):
    """`organization_id` nulo = role de sistema (Owner/Admin/Recepção/
    Profissional), disponível como template a todas as organizações.
    `organization_id` preenchido = role customizada, exclusiva daquela org.
    """

    __tablename__ = "roles"
    __table_args__ = (
        # unicidade de nome varia conforme o role é global ou de uma org —
        # não dá para expressar com um UniqueConstraint simples porque a
        # coluna organization_id é nullable.
        Index(
            "uq_roles_system_name",
            "name",
            unique=True,
            postgresql_where="organization_id IS NULL",
        ),
        Index(
            "uq_roles_org_name",
            "organization_id",
            "name",
            unique=True,
            postgresql_where="organization_id IS NOT NULL",
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    permissions: Mapped[list["RolePermission"]] = relationship(back_populates="role")


class Permission(Base):
    """Catálogo global de permissões (ex.: `agenda.view_own`). Definido em
    código/seed, não pertence a nenhuma organização — por isso não herda
    UUIDPKMixin/TimestampMixin: a PK é a própria chave semântica."""

    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    module: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )

    role: Mapped["Role"] = relationship(back_populates="permissions")
    permission: Mapped["Permission"] = relationship()
