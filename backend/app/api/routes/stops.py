from fastapi import APIRouter

# GET/POST /trips/{slug}/stops — list/create stops (POST is rider-slug only,
# 403 on viewer slug). Session 1 adds the request/response models and handler.
router = APIRouter(prefix="/trips/{slug}/stops", tags=["stops"])
