from fastapi import APIRouter

from . import agenda, appointments, auth, branches, clients, organizations, professionals, services, users

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth.router)
api_v1_router.include_router(organizations.router)
api_v1_router.include_router(branches.router)
api_v1_router.include_router(professionals.router)
api_v1_router.include_router(services.router)
api_v1_router.include_router(clients.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(agenda.router)
api_v1_router.include_router(appointments.router)
