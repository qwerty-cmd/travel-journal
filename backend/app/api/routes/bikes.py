from fastapi import APIRouter

# POST/PATCH /trips/{slug}/bikes — add/edit a bike + specs (rider-slug only).
# Session 1 adds the request/response models and handler.
router = APIRouter(prefix="/trips/{slug}/bikes", tags=["bikes"])
