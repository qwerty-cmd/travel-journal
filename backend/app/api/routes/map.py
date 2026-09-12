from fastapi import APIRouter

# GET /trips/{slug}/map — GeoJSON of stops + chronological trail (either
# slug). Session 1 adds the request/response models and handler.
router = APIRouter(prefix="/trips/{slug}/map", tags=["map"])
