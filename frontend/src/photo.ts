// Photo capture processing (architect ruling R4): downscale to a JPEG and derive
// an offset-aware `takenAt` per the api-contract ladder. EXIF is hand-parsed
// from the ORIGINAL file because re-encoding through a canvas strips it.
// Known limit: createImageBitmap decodes the source at full size (a 48MP photo
// costs a lot of memory on older phones) before we downscale. Only formats the
// browser can decode are accepted: HEIC is re-encoded to JPEG only where the
// browser can decode it; elsewhere (e.g. desktop Chrome) it throws
// PhotoDecodeError at pick time and is never queued.
// APIs called: none. The add-stop form enqueues the result; the offline queue
// uploads it.

export type ExifTimes = { dateTimeOriginal?: string; offsetTimeOriginal?: string };

/** Thrown when the browser cannot decode the picked file (e.g. HEIC on desktop Chrome). Show it at pick time; do not queue. */
export class PhotoDecodeError extends Error {
  constructor(message = "This photo could not be read. Try a JPEG or PNG.") {
    super(message);
    this.name = "PhotoDecodeError";
  }
}

const DTO_RE = /^(?!0000)\d{4}:(0[1-9]|1[0-2]):(0[1-9]|[12]\d|3[01]) ([01]\d|2[0-3]):[0-5]\d:[0-5]\d$/;
// Offsets are bounded to ±14:00 (the real-world range; Python's AwareDatetime rejects >= 24h).
const OFFSET_RE = /^[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00)$/;
// DTO_RE bounds each field; this rejects day overflow for the month (Feb 31, Feb 29 on a non-leap year).
const isRealDate = (dto: string) => {
  const [y, m, d] = dto.slice(0, 10).split(":").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDate() === d;
};

/** Reads EXIF DateTimeOriginal (0x9003) and OffsetTimeOriginal (0x9011) from JPEG bytes. Never throws; invalid tags (bad format, impossible date, offset beyond ±14:00) are treated as absent, so composeTakenAt falls to the next ladder rung. */
export function parseExifTimes(buf: ArrayBuffer): ExifTimes {
  try {
    const v = new DataView(buf);
    if (v.getUint16(0) !== 0xffd8) return {};
    // Walk JPEG segments to APP1 "Exif\0\0"; stop at SOS (image data follows).
    let p = 2;
    let tiff = -1;
    while (p + 4 <= v.byteLength) {
      const marker = v.getUint16(p);
      if ((marker & 0xff00) !== 0xff00 || marker === 0xffda) return {};
      const len = v.getUint16(p + 2);
      if (marker === 0xffe1 && v.getUint32(p + 4) === 0x45786966 && v.getUint16(p + 8) === 0) {
        tiff = p + 10;
        break;
      }
      p += 2 + len;
    }
    if (tiff < 0) return {};

    const order = v.getUint16(tiff);
    if (order !== 0x4d4d && order !== 0x4949) return {};
    const le = order === 0x4949;
    const u16 = (o: number) => v.getUint16(tiff + o, le);
    const u32 = (o: number) => v.getUint32(tiff + o, le);
    if (u16(2) !== 42) return {};

    // Returns the 12-byte entry offset (relative to TIFF start) for `tag` in the IFD at `ifd`.
    const findEntry = (ifd: number, tag: number) => {
      const n = u16(ifd);
      for (let i = 0; i < n; i++) {
        const e = ifd + 2 + i * 12;
        if (u16(e) === tag) return e;
      }
      return -1;
    };
    // ASCII (type 2) value; inline when count <= 4, else at an offset. undefined when out of bounds.
    const ascii = (e: number): string | undefined => {
      if (e < 0 || u16(e + 2) !== 2) return undefined;
      const count = u32(e + 4);
      const start = tiff + (count <= 4 ? e + 8 : u32(e + 8));
      if (start + count > v.byteLength) return undefined;
      let s = "";
      for (let i = 0; i < count; i++) {
        const c = v.getUint8(start + i);
        if (c === 0) break;
        s += String.fromCharCode(c);
      }
      return s;
    };

    const exifPtr = findEntry(u32(4), 0x8769);
    if (exifPtr < 0) return {};
    const exifIfd = u32(exifPtr + 8);
    const out: ExifTimes = {};
    const dto = ascii(findEntry(exifIfd, 0x9003));
    if (dto && DTO_RE.test(dto) && isRealDate(dto)) out.dateTimeOriginal = dto;
    const off = ascii(findEntry(exifIfd, 0x9011));
    if (off && OFFSET_RE.test(off)) out.offsetTimeOriginal = off;
    return out;
  } catch {
    return {}; // truncated/garbled: DataView RangeError falls through to the next ladder rung
  }
}

/** Composes the upload's timezone-aware `takenAt` per the api-contract ladder: (a) EXIF time + EXIF offset, (b) EXIF time + device offset at `now`, (c) `now`. */
export function composeTakenAt(exif: ExifTimes, now: Date): string {
  if (!exif.dateTimeOriginal) return now.toISOString();
  const [d, t] = exif.dateTimeOriginal.split(" ");
  const wall = `${d.replace(/:/g, "-")}T${t}`;
  if (exif.offsetTimeOriginal) return wall + exif.offsetTimeOriginal;
  // ponytail: current device offset, so an old photo across a DST change is off by 1h (Decision 3).
  const mins = -now.getTimezoneOffset();
  const abs = Math.abs(mins);
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  return `${wall}${mins < 0 ? "-" : "+"}${hh}:${mm}`;
}

/** Scales (w, h) so the long edge is at most `max`, preserving aspect ratio. Never upscales. */
export function fitWithin(w: number, h: number, max = 1600): { width: number; height: number } {
  const scale = Math.min(1, max / Math.max(w, h));
  return { width: Math.round(w * scale), height: Math.round(h * scale) };
}

/** Browser-only: returns a JPEG (<=1600px long edge, quality 0.82, EXIF orientation applied) and its `takenAt`. Throws PhotoDecodeError if the file can't be decoded. */
export async function processPhoto(file: File, now = new Date()): Promise<{ blob: Blob; takenAt: string }> {
  const takenAt = composeTakenAt(parseExifTimes(await file.slice(0, 131072).arrayBuffer()), now);
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    throw new PhotoDecodeError();
  }
  const { width, height } = fitWithin(bitmap.width, bitmap.height);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new PhotoDecodeError();
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close?.();
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
  if (!blob) throw new PhotoDecodeError();
  return { blob, takenAt };
}
