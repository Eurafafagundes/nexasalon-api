from fastapi import APIRouter

from . import (
    agenda,
    appointment_status_styles,
    appointments,
    auth,
    branches,
    cash_register_config,
    cash_registers,
    clients,
    dashboard,
    extract,
    inventory_counts,
    orders,
    organizations,
    products,
    professionals,
    public_booking,
    roles,
    schedule_blocks,
    service_categories,
    services,
    stock,
    users,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth.router)
api_v1_router.include_router(organizations.router)
api_v1_router.include_router(branches.router)
api_v1_router.include_router(professionals.router)
api_v1_router.include_router(services.router)
api_v1_router.include_router(service_categories.router)
api_v1_router.include_router(clients.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(roles.router)
api_v1_router.include_router(agenda.router)
api_v1_router.include_router(appointments.router)
api_v1_router.include_router(appointment_status_styles.router)
api_v1_router.include_router(schedule_blocks.router)
api_v1_router.include_router(orders.router)
api_v1_router.include_router(cash_registers.router)
api_v1_router.include_router(cash_register_config.router)
api_v1_router.include_router(extract.router)
api_v1_router.include_router(dashboard.router)
api_v1_router.include_router(products.router)
api_v1_router.include_router(stock.router)
api_v1_router.include_router(inventory_counts.router)
api_v1_router.include_router(public_booking.router)
