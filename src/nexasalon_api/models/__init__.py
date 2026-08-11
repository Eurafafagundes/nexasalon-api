"""
Modelos SQLAlchemy 2.x do NexaSalon.

Import central para que o Alembic (via `Base.metadata`) enxergue todas as
tabelas ao gerar/rodar migrations — cada módulo de model precisa estar
importado aqui, senão fica invisível para o autogenerate.
"""
from .base import Base  # noqa: F401
from .organization import Organization, Branch  # noqa: F401
from .rbac import Role, Permission, RolePermission  # noqa: F401
from .identity import User, OrganizationMembership, MembershipPermissionOverride  # noqa: F401
from .professional import Professional, WorkingHours, ScheduleBlock  # noqa: F401
from .service import Service, ProfessionalService  # noqa: F401
from .client import Client  # noqa: F401
from .appointment import Recurrence, Appointment, AppointmentItem  # noqa: F401
from .tag import Tag, AppointmentTag  # noqa: F401
from .audit import AuditLog  # noqa: F401

__all__ = [
    "Base",
    "Organization",
    "Branch",
    "Role",
    "Permission",
    "RolePermission",
    "User",
    "OrganizationMembership",
    "MembershipPermissionOverride",
    "Professional",
    "WorkingHours",
    "ScheduleBlock",
    "Service",
    "ProfessionalService",
    "Client",
    "Recurrence",
    "Appointment",
    "AppointmentItem",
    "Tag",
    "AppointmentTag",
    "AuditLog",
]
