from fastapi import APIRouter

# POST/GET /trips/{slug}/stops/{id}/photos — multipart upload (rider-slug
# only) / list (either slug). Session 1 adds the request/response models and
# handler once the API contract is locked.
router = APIRouter(prefix="/trips/{slug}/stops/{stop_id}/photos", tags=["photos"])
