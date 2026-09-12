from datetime import datetime

from pydantic import BaseModel, Field


class PhotoCreateForm(BaseModel):
    """
    POST /trips/{slug}/stops/{stop_id}/photos — multipart form fields, sent
    alongside the file itself (`file: UploadFile`, not part of this model since
    Pydantic models don't carry binary parts). Rider-slug only.
    """

    id: str = Field(
        description="Client-generated UUID4, assigned when the photo is captured "
        "(possibly offline, before upload succeeds). Replaying the same id — same "
        "photo retried after a dropped connection — returns the existing photo (200) "
        "instead of storing a duplicate. See docs/api-contract.md 'Idempotency'."
    )
    uploadedBy: str = Field(description="Display name only, no auth — see spec Section 6.")
    takenAt: datetime


class PhotoOut(BaseModel):
    id: str
    stopId: str
    url: str = Field(
        description="Presigned GET URL into S3-compatible storage, generated at "
        "read time by the storage/ module — never stored in the database, and never "
        "a OneDrive URL (OneDrive is write-only archive, not a read path)."
    )
    uploadedBy: str
    takenAt: datetime
    archived: bool = Field(description="True once the background OneDrive sync has landed this photo.")
