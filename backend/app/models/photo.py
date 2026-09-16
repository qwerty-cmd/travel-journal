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
    id: str = Field(
        description="The photo's id — the same client-generated UUID4 that was sent on "
        "upload, so the device's local copy and the server's record share one identifier. "
        "Match photos by this, never by their position in a list."
    )
    stopId: str = Field(
        description="The id of the stop this photo hangs off — the same value as the "
        "matching `StopOut.id`, so a photo resolves back to its stop without another "
        "lookup. Stored as `photos.stop_id`."
    )
    url: str = Field(
        description="Presigned GET URL into S3-compatible storage, generated at "
        "read time by the storage/ module — never stored in the database, and never "
        "a OneDrive URL (OneDrive is write-only archive, not a read path)."
    )
    uploadedBy: str = Field(
        description="Display name only, no auth — see spec Section 6. Whoever the uploading "
        "device said it was; it is not tied to a user account and nothing verifies it. "
        "Stored as `photos.uploaded_by`."
    )
    # This description deliberately makes NO timezone claim — not an oversight, and
    # pinned by test_taken_at_description_makes_no_timezone_claim. Whether a capture
    # time must carry a UTC offset is still unruled (docs/api-contract.md item 5,
    # t-takenat-tz-question): EXIF DateTimeOriginal is naive by design, so the
    # StopCreate.arrivedAt ruling does not transfer by analogy. A description is
    # contract text Kubb ships to the client, so wording a rule here would settle the
    # question by the back door. Add the claim when the ruling lands, not before.
    takenAt: datetime = Field(
        description="When the photo was taken — the device's capture time, not the time the "
        "upload reached the server, which can be much later if the rider was offline. "
        "Stored as `photos.taken_at`."
    )
    archived: bool = Field(description="True once the background OneDrive sync has landed this photo.")
