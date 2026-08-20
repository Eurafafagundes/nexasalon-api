"""
Modelos SQLAlchemy 2.x do NexaSalon.

Import central para que o Alembic (via `Base.metadata`) enxergue todas as
tabelas ao gerar/rodar migrations — cada módulo de model precisa estar
importado aqui, senão fica invisível para o autogenerate.
"""
from .base import Base  # noqa: F401
from .auth import RefreshToken  # noqa: F401
from .organization import Organization, Branch  # noqa: F401
from .rbac import Role, Permission, RolePermission  # noqa: F401
from .identity import User, OrganizationMembership, MembershipPermissionOverride  # noqa: F401
from .agenda_access import MembershipAgendaGrant  # noqa: F401
from .professional import Professional, WorkingHours, ScheduleBlock  # noqa: F401
from .service import Service, ServiceCategory, ProfessionalService  # noqa: F401
from .client import Client  # noqa: F401
from .appointment import Recurrence, Appointment, AppointmentItem  # noqa: F401
from .appointment_status_style import AppointmentStatusStyle  # noqa: F401
from .cash_register import CashRegister, CashMovement  # noqa: F401
from .order import Order, OrderItem, OrderProductItem, Payment  # noqa: F401
from .product import Product, StockLevel  # noqa: F401
from .stock import StockMovement, StockTransfer, InventoryCount, InventoryCountItem  # noqa: F401
from .tag import Tag, AppointmentTag  # noqa: F401
from .audit import AuditLog  # noqa: F401

__all__ = [
    "Base",
    "RefreshToken",
    "Organization",
    "Branch",
    "Role",
    "Permission",
    "RolePermission",
    "User",
    "OrganizationMembership",
    "MembershipPermissionOverride",
    "MembershipAgendaGrant",
    "Professional",
    "WorkingHours",
    "ScheduleBlock",
    "Service",
    "ServiceCategory",
    "ProfessionalService",
    "Client",
    "Recurrence",
    "Appointment",
    "AppointmentItem",
    "AppointmentStatusStyle",
    "CashRegister",
    "CashMovement",
    "Order",
    "OrderItem",
    "OrderProductItem",
    "Payment",
    "Product",
    "StockLevel",
    "StockMovement",
    "StockTransfer",
    "InventoryCount",
    "InventoryCountItem",
    "Tag",
    "AppointmentTag",
    "AuditLog",
]
