import { type ChangeEvent, useEffect, useRef, useState } from "react";
import { CameraIcon, XIcon } from "../icons";
import { processPhoto } from "../photo";
import { Button } from "./Button";
import "./PhotoPicker.css";

// Design feature: picking photos for a stop, shared by add stop
// (docs/design/screens/add-stop.md §7) and "Add photos" on an existing stop
// (docs/design/screens/stop-detail.md §6-7). Photos are processed at pick time
// (processPhoto: ≤ 1600 px JPEG + takenAt); each gets its client id when
// processing completes; an undecodable file is reported, the others stay.
// Design format: `usePhotoPicks` holds the picks; `AddPhotosControl` is the
// hidden `<input type="file" accept="image/*" multiple>` (visually-hidden label,
// out of the tab order) opened by a secondary "Add photos" button with the
// camera icon; `PickStatus` is "Processing N photos…" and the "Couldn't read
// photo X" lines; `PickedPreviews` is the row of 72 px previews, each with a
// 48 px "Remove photo N". With `addButtonId`, removing moves focus to the next
// tile's Remove button, or to that button when none are left.
// APIs called: none. Callers enqueue the picks on the offline queue.

export type PickedPhoto = { id: string; fileName: string; blob: Blob; takenAt: string };

export function usePhotoPicks() {
  const [photos, setPhotos] = useState<PickedPhoto[]>([]);
  const [processing, setProcessing] = useState(0);
  const [errors, setErrors] = useState<string[]>([]);

  function pick(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    setErrors([]);
    for (const file of files) {
      setProcessing((n) => n + 1);
      processPhoto(file)
        .then(({ blob, takenAt }) => setPhotos((ps) => [...ps, { id: crypto.randomUUID(), fileName: file.name, blob, takenAt }]))
        .catch(() => setErrors((es) => [...es, `Couldn't read photo ${file.name}`]))
        .finally(() => setProcessing((n) => n - 1));
    }
  }

  const remove = (id: string) => setPhotos((ps) => ps.filter((x) => x.id !== id));
  const clear = () => {
    setPhotos([]);
    setErrors([]);
  };

  return { photos, processing, errors, pick, remove, clear };
}

export function AddPhotosControl({
  inputId,
  onPick,
  size,
  buttonId,
}: {
  inputId: string;
  onPick: (e: ChangeEvent<HTMLInputElement>) => void;
  size: "md" | "lg";
  buttonId?: string;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  return (
    <>
      <label htmlFor={inputId} className="visually-hidden">
        Photos
      </label>
      <input id={inputId} ref={fileInput} className="visually-hidden" type="file" accept="image/*" multiple onChange={onPick} tabIndex={-1} />
      <Button id={buttonId} type="button" variant="secondary" size={size} leadingIcon={<CameraIcon />} onClick={() => fileInput.current?.click()}>
        Add photos
      </Button>
    </>
  );
}

export function PickStatus({ processing, errors }: { processing: number; errors: string[] }) {
  return (
    <>
      {processing > 0 && (
        <p className="photo-picker__muted" role="status">
          Processing {processing} {processing === 1 ? "photo" : "photos"}…
        </p>
      )}
      {errors.map((m) => (
        <p key={m} className="photo-picker__error" role="alert">
          {m}
        </p>
      ))}
    </>
  );
}

export function PickedPreviews({
  photos,
  onRemove,
  addButtonId,
}: {
  photos: PickedPhoto[];
  onRemove: (id: string) => void;
  addButtonId?: string;
}) {
  const list = useRef<HTMLUListElement>(null);
  const focusIndex = useRef<number | null>(null);

  useEffect(() => {
    const i = focusIndex.current;
    if (i === null) return;
    focusIndex.current = null;
    const buttons = list.current?.querySelectorAll<HTMLButtonElement>(".photo-picker__remove");
    const next = buttons?.[Math.min(i, buttons.length - 1)];
    (next ?? (addButtonId ? document.getElementById(addButtonId) : null))?.focus();
  }, [photos, addButtonId]);

  if (photos.length === 0) return null;
  return (
    <ul ref={list} className="photo-picker__previews">
      {photos.map((p, i) => (
        <li key={p.id} className="photo-picker__preview">
          <Preview blob={p.blob} alt={p.fileName} />
          <button
            type="button"
            className="photo-picker__remove"
            aria-label={`Remove photo ${i + 1}`}
            onClick={() => {
              if (addButtonId) focusIndex.current = i;
              onRemove(p.id);
            }}
          >
            <XIcon size="sm" />
          </button>
        </li>
      ))}
    </ul>
  );
}

/** A 72 px preview of a processed photo; the object URL lives as long as the tile. */
function Preview({ blob, alt }: { blob: Blob; alt: string }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    const u = URL.createObjectURL(blob);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [blob]);
  return url ? <img className="photo-picker__thumb" src={url} alt={alt} /> : <span className="photo-picker__thumb" />;
}
