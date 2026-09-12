from fastapi import APIRouter

# GET /trips/{slug} — trip metadata + bikes (either slug). Session 1 adds the
# request/response models and handler once the API contract is locked.
router = APIRouter(prefix="/trips", tags=["trips"])
