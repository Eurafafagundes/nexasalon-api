import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, UUIDPKMixin


class MembershipAgendaGrant(Base, UUIDPKMixin, TimestampMixin):
    """Concessão pontual de VISUALIZAÇÃO/EDIÇÃO de UM profissional
    específico para UMA membership — só tem efeito quando o respectivo
    `OrganizationMembership.agenda_view_scope`/`agenda_edit_scope`
    (`models/identity.py`) está em `SELECTED`; em `ALL` estas linhas são
    ignoradas (o ator já vê/edita todo mundo, inclusive profissionais
    futuros).

    `can_view=False, can_edit=True` nunca deveria existir — quem edita a
    agenda de alguém precisa primeiro poder VER essa agenda (item
    explícito do pedido: "editar" é mais restrito que "visualizar", nunca
    o contrário). Reforçado por `CheckConstraint` no banco, não só em
    validação de aplicação.

    Independe de `agenda.view_all`/`agenda.view_own`/`agenda.edit`
    (`models/rbac.py` catálogo de permissions): aquelas continuam sendo o
    portão de entrada ("este ator pode usar o módulo Agenda de alguma
    forma?"); esta tabela é sobre QUAIS profissionais especificamente,
    inclusive permitindo um ator com só `agenda.view_own` enxergar
    também a agenda de um colega (caso de uso explícito do pedido)."""

    __tablename__ = "membership_agenda_grants"
    __table_args__ = (
        UniqueConstraint("membership_id", "professional_id", name="uq_membership_agenda_grants_membership_professional"),
        CheckConstraint("can_view OR NOT can_edit", name="ck_membership_agenda_grants_edit_requires_view"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_memberships.id", ondelete="CASCADE"), nullable=False
    )
    professional_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professionals.id", ondelete="CASCADE"), nullable=False
    )
    can_view: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    can_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    membership: Mapped["OrganizationMembership"] = relationship()  # noqa: F821
