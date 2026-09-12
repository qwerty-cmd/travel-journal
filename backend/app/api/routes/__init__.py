from fastapi import APIRouter

from app.api.routes import bikes, map, photos, stops, trips

api_router = APIRouter(prefix="/api")
api_router.include_router(trips.router)
api_router.include_router(stops.router)
api_router.include_router(photos.router)
api_router.include_router(bikes.router)
api_router.include_router(map.router)
