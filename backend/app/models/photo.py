from datetime import datetime

from pydantic import BaseModel, Field


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
        description="Who uploaded the photo: the uploading account's `displayName` at upload "
        "time, set by the server from the session, whatever the upload form sent. For photos "
        "uploaded before accounts existed, the free-text label stored then. A snapshot: it "
        "does not change if the account is renamed. Stored as `photos.uploaded_by`."
    )
    takenAt: datetime = Field(
        description="When the photo was taken, as a timezone-aware ISO 8601 instant — the "
        "device's capture time, not the time the upload reached the server, which can be much "
        "later if the rider was offline. Always carries an offset, because upload rejects a "
        "naive value (422 / VALIDATION_ERROR). Every response, the `201` upload included, reads "
        "it back from `photos.taken_at`, a `timestamptz`, so it is always a **UTC** instant "
        "(`Z`), whatever offset the device sent. Compare it as a datetime, not a string."
    )
    archived: bool = Field(
        description="True once the background OneDrive sync has landed this photo."
    )
