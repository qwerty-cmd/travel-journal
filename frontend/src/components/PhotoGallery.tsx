import { useEffect, useRef, useState } from "react";
import type { PhotoOut } from "../api/gen/types/PhotoOut";
import "./PhotoGallery.css";

// Design feature: a stop's photos (docs/design/screens/stop-detail.md, DESIGN.md
// §4.7), read-only for everyone. Same behaviour as the legacy stop screen:
// presigned 1 h URLs are shown straight from the query and never persisted; an
// <img> error (typically an expired URL) refetches the list once, and a list
// that is itself the result of that refetch never triggers another, so a
// genuinely broken photo can't loop. The enlarged view is looked up by id, so
// it picks up the fresh URL too.
// Design format: a wrap of 96 px square lazy thumbnails (buttons); tapping one
// opens a full-screen dialog; a click anywhere or Escape closes it.
// APIs called: none itself; the caller passes the photo list query's data,
// dataUpdatedAt and refetch.
export function PhotoGallery({
  photos,
  dataUpdatedAt,
  refetch,
}: {
  photos: PhotoOut[];
  dataUpdatedAt: number;
  refetch: () => Promise<{ dataUpdatedAt: number }>;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const open = photos.find((p) => p.id === openId);

  const refresh = useRef<{ busy: boolean; resultAt: number | null }>({ busy: false, resultAt: null });
  function onPhotoError() {
    const r = refresh.current;
    if (r.busy || dataUpdatedAt === r.resultAt) return;
    r.busy = true;
    refetch().then((res) => {
      r.busy = false;
      r.resultAt = res.dataUpdatedAt;
    });
  }

  useEffect(() => {
    if (!openId) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpenId(null);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [openId]);

  return (
    <>
      <div className="gallery">
        {photos.map((photo) => (
          <button key={photo.id} type="button" className="gallery__thumb" onClick={() => setOpenId(photo.id)}>
            <img src={photo.url} alt={`Photo by ${photo.uploadedBy}`} loading="lazy" onError={onPhotoError} />
          </button>
        ))}
      </div>
      {open && (
        <div role="dialog" aria-modal="true" aria-label="Photo" className="gallery__viewer" onClick={() => setOpenId(null)}>
          <img src={open.url} alt={`Photo by ${open.uploadedBy}`} onError={onPhotoError} />
        </div>
      )}
    </>
  );
}
