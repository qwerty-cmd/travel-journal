"""
The v2 API surface — ``/api/v2`` (decision-log Entry 29).

Routes located by account and trip id rather than by slug. Each module adds its
own router here; ``app/api/routes/__init__.py`` mounts this one under ``/api``.
"""

from fastapi import APIRouter

from app.api.routes.v2 import auth, rider_writes, trips

v2_router = APIRouter(prefix="/v2")
v2_router.include_router(auth.router)
v2_router.include_router(trips.router)
v2_router.include_router(rider_writes.router)
v2_router.include_router(rider_writes.photo_router)
