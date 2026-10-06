from fastapi import APIRouter

from app.api.routes import bikes, map, photos, stops, trips
from app.api.routes.v2 import v2_router

api_router = APIRouter(prefix="/api")
api_router.include_router(trips.router)
api_router.include_router(stops.router)
api_router.include_router(photos.router)
api_router.include_router(bikes.router)
api_router.include_router(map.router)
api_router.include_router(v2_router)
