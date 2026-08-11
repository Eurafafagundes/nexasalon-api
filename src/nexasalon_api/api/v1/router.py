from fastapi import APIRouter

from . import branches, clients, organizations, professionals, services

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(organizations.router)
api_v1_router.include_router(branches.router)
api_v1_router.include_router(professionals.router)
api_v1_router.include_router(services.router)
api_v1_router.include_router(clients.router)
