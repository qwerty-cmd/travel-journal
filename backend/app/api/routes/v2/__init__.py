"""
The v2 API surface — ``/api/v2`` (decision-log Entry 29).

Routes located by account and trip id rather than by slug. Each module adds its
own router here; ``app/api/routes/__init__.py`` mounts this one under ``/api``.
"""

from fastapi import APIRouter

from app.api.routes.v2 import auth

v2_router = APIRouter(prefix="/v2")
v2_router.include_router(auth.router)
