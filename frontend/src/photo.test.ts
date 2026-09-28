import { afterEach, describe, expect, test, vi } from "vitest";
import { PhotoDecodeError, composeTakenAt, fitWithin, parseExifTimes, processPhoto } from "./photo";

// t-photo-capture-processing. EXIF fixtures are hand-built JPEG byte streams:
// SOI, [APP0/JFIF], APP1 "Exif\0\0" + TIFF(IFD0 -> 0x8769 -> ExifIFD{0x9003, 0x9011}).

type Fixture = {
  le?: boolean;
  app0?: boolean;
  dto?: string | null; // null = tag absent
  offset?: string | null;
  dtoValueOffset?: number; // override the 0x9003 value pointer (TIFF-relative)
};

const DTO = "2026:06:14 10:00:00";
const OFF = "+09:30";

function jpeg({ le = false, app0 = false, dto = DTO, offset = OFF, dtoValueOffset }: Fixture = {}): ArrayBuffer {
  const tags: [number, string][] = [];
  if (dto !== null) tags.push([0x9003, dto]);
  if (offset !== null) tags.push([0x9011, offset]);

  const exifIfd = 8 + 2 + 12 + 4; // after IFD0 (one entry)
  let data = exifIfd + 2 + tags.length * 12 + 4; // value area after ExifIFD
  const tiff = new DataView(new ArrayBuffer(data + 64));
  const u16 = (o: number, x: number) => tiff.setUint16(o, x, le);
  const u32 = (o: number, x: number) => tiff.setUint32(o, x, le);

  tiff.setUint16(0, le ? 0x4949 : 0x4d4d);
  u16(2, 42);
  u32(4, 8);
  u16(8, 1);
  u16(10, 0x8769); u16(12, 4); u32(14, 1); u32(18, exifIfd);
  u32(22, 0);
  u16(exifIfd, tags.length);
  tags.forEach(([tag, s], i) => {
    const e = exifIfd + 2 + i * 12;
    const count = s.length + 1; // NUL-terminated
    u16(e, tag); u16(e + 2, 2); u32(e + 4, count);
    let at = e + 8; // inline when count <= 4
    if (count > 4) {
      at = data;
      u32(e + 8, tag === 0x9003 && dtoValueOffset !== undefined ? dtoValueOffset : data);
      data += count;
    }
    for (let j = 0; j < s.length; j++) tiff.setUint8(at + j, s.charCodeAt(j));
  });
  const tiffBytes = new Uint8Array(tiff.buffer, 0, data);

  const bytes: number[] = [0xff, 0xd8];
  if (app0) bytes.push(0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 1, 1, 0, 0, 1, 0, 1, 0, 0);
  const len = 2 + 6 + tiffBytes.length;
  bytes.push(0xff, 0xe1, len >> 8, len & 0xff, 0x45, 0x78, 0x69, 0x66, 0, 0, ...tiffBytes);
  bytes.push(0xff, 0xda, 0x00, 0x02, 0xff, 0xd9); // SOS + EOI
  return new Uint8Array(bytes).buffer;
}

describe("parseExifTimes — AC1 happy paths", () => {
  test("MM with both tags", () => {
    expect(parseExifTimes(jpeg())).toEqual({ dateTimeOriginal: DTO, offsetTimeOriginal: OFF });
  });
  test("II with DateTimeOriginal only", () => {
    expect(parseExifTimes(jpeg({ le: true, offset: null }))).toEqual({ dateTimeOriginal: DTO });
  });
  test("II with both tags", () => {
    expect(parseExifTimes(jpeg({ le: true }))).toEqual({ dateTimeOriginal: DTO, offsetTimeOriginal: OFF });
  });
  test("APP1 after APP0/JFIF is still found", () => {
    expect(parseExifTimes(jpeg({ app0: true }))).toEqual({ dateTimeOriginal: DTO, offsetTimeOriginal: OFF });
    expect(parseExifTimes(jpeg({ app0: true, le: true }))).toEqual({ dateTimeOriginal: DTO, offsetTimeOriginal: OFF });
  });
});

describe("parseExifTimes — AC2 malformed input returns {} / drops the tag, never throws", () => {
  test("empty buffer", () => {
    expect(parseExifTimes(new ArrayBuffer(0))).toEqual({});
  });
  test("no SOI", () => {
    const b = new Uint8Array(jpeg());
    b[1] = 0x00;
    expect(parseExifTimes(b.buffer)).toEqual({});
  });
  test("no APP1 (APP0 then SOS)", () => {
    const b = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x04, 0, 0, 0xff, 0xda, 0x00, 0x02]);
    expect(parseExifTimes(b.buffer)).toEqual({});
  });
  test.each([4, 14, 30, 50])("truncated at %i bytes", (n) => {
    expect(parseExifTimes(jpeg().slice(0, n))).toEqual({});
  });
  test("DTO value offset past end -> DTO absent, offset still read", () => {
    expect(parseExifTimes(jpeg({ dtoValueOffset: 10_000 }))).toEqual({ offsetTimeOriginal: OFF });
  });
  test.each(["0000:00:00 00:00:00", "                   ", "", "2026-06-14 10:00:00", "2026:13:14 10:00:00", "2026:06:14 24:00:00"])(
    "invalid DateTimeOriginal %j is absent",
    (dto) => {
      expect(parseExifTimes(jpeg({ dto, offset: null }))).toEqual({});
    },
  );
  test.each(["+0930", "09:30", "      ", "Z", "+9:30"])("invalid OffsetTimeOriginal %j is absent", (offset) => {
    expect(parseExifTimes(jpeg({ offset }))).toEqual({ dateTimeOriginal: DTO });
  });
});

describe("composeTakenAt — AC3 ladder", () => {
  const SUFFIX = /(Z|[+-]\d\d:\d\d)$/;
  const at = (tz: number) => {
    const now = new Date("2026-09-29T12:00:00Z");
    vi.spyOn(now, "getTimezoneOffset").mockReturnValue(tz);
    return now;
  };

  test("(a) EXIF time + EXIF offset", () => {
    const out = composeTakenAt({ dateTimeOriginal: DTO, offsetTimeOriginal: OFF }, at(300));
    expect(out).toBe("2026-06-14T10:00:00+09:30");
  });
  test.each([
    [-570, "+09:30"],
    [0, "+00:00"],
    [300, "-05:00"],
  ])("(b) EXIF time + device offset (getTimezoneOffset=%i -> %s)", (tz, suffix) => {
    const out = composeTakenAt({ dateTimeOriginal: DTO }, at(tz));
    expect(out).toBe(`2026-06-14T10:00:00${suffix}`);
    expect(out).toMatch(SUFFIX);
  });
  test.each([[{}], [{ offsetTimeOriginal: OFF }]])("(c) no usable EXIF %j -> now.toISOString()", (exif) => {
    const now = at(-570);
    const out = composeTakenAt(exif, now);
    expect(out).toBe(now.toISOString());
    expect(out).toMatch(SUFFIX);
  });
});

// t-photo-exif-invalid-values: validation lives in parseExifTimes; invalid values are absent,
// so composeTakenAt (fed the parsed output) drops to the next rung instead of emitting a 422-bound takenAt.
describe("parseExifTimes — calendar/offset validity (t-photo-exif-invalid-values)", () => {
  test.each(["2026:02:31 10:00:00", "2026:02:29 10:00:00", "2026:04:31 10:00:00", "2100:02:29 10:00:00", "2026:13:14 10:00:00", "2026:06:14 24:00:00"])(
    "impossible DateTimeOriginal %j is absent",
    (dto) => {
      expect(parseExifTimes(jpeg({ dto, offset: null }))).toEqual({});
    },
  );
  test.each(["2028:02:29 10:00:00", "2000:02:29 10:00:00", "2026:12:31 23:59:59", "2026:01:31 00:00:00"])(
    "real DateTimeOriginal %j is kept",
    (dto) => {
      expect(parseExifTimes(jpeg({ dto, offset: null }))).toEqual({ dateTimeOriginal: dto });
    },
  );
  test.each(["+14:00", "-14:00", "+13:59", "-00:00", "+00:00", "+05:45", "-09:30"])("offset %j is kept", (offset) => {
    expect(parseExifTimes(jpeg({ offset }))).toEqual({ dateTimeOriginal: DTO, offsetTimeOriginal: offset });
  });
  test.each(["+14:01", "-14:30", "+15:00", "+24:00", "+05:60", "+99:99"])("offset %j is absent", (offset) => {
    expect(parseExifTimes(jpeg({ offset }))).toEqual({ dateTimeOriginal: DTO });
  });
});

describe("composeTakenAt fed from parseExifTimes — invalid values fall through the ladder", () => {
  const now = new Date("2026-09-29T12:00:00Z");
  afterEach(() => vi.restoreAllMocks());

  test.each(["+14:01", "+24:00", "+99:99", "+05:60"])("invalid offset %j -> rung (b) device offset", (offset) => {
    vi.spyOn(now, "getTimezoneOffset").mockReturnValue(-570);
    expect(composeTakenAt(parseExifTimes(jpeg({ offset })), now)).toBe("2026-06-14T10:00:00+09:30");
  });
  test.each(["2026:02:31 10:00:00", "2026:02:29 10:00:00"])("invalid date %j (valid offset) -> rung (c) now", (dto) => {
    expect(composeTakenAt(parseExifTimes(jpeg({ dto })), now)).toBe(now.toISOString());
  });
  test("Feb 29 2028 + edge offset -14:00 -> rung (a)", () => {
    expect(composeTakenAt(parseExifTimes(jpeg({ dto: "2028:02:29 23:59:59", offset: "-14:00" })), now)).toBe(
      "2028-02-29T23:59:59-14:00",
    );
  });
});

describe("fitWithin — AC4", () => {
  test.each([
    [4000, 3000, 1600, 1200],
    [3000, 4000, 1200, 1600],
    [800, 600, 800, 600],
  ])("(%i,%i) -> (%i,%i)", (w, h, ew, eh) => {
    expect(fitWithin(w, h)).toEqual({ width: ew, height: eh });
  });
  test("odd aspect ratio yields integers", () => {
    const { width, height } = fitWithin(4032, 3023);
    expect(width).toBe(1600);
    expect(Number.isInteger(height)).toBe(true);
  });
});

describe("processPhoto — AC5", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const stubCanvas = (blob: Blob | null, ctx: unknown = { drawImage: vi.fn() }) => {
    const seen: { width: number; height: number; type?: string; quality?: number }[] = [];
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as never);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(function (
      this: HTMLCanvasElement,
      cb: BlobCallback,
      type?: string,
      quality?: number,
    ) {
      seen.push({ width: this.width, height: this.height, type, quality });
      cb(blob);
    });
    return seen;
  };
  const bitmap = () => ({ width: 4000, height: 3000, close: vi.fn() });
  const file = (buf = jpeg()) => new File([buf], "p.jpg", { type: "image/jpeg" });

  test("success: EXIF from the original file, orientation applied, 1600x1200 JPEG at 0.82", async () => {
    const bmp = bitmap();
    const cib = vi.fn().mockResolvedValue(bmp);
    vi.stubGlobal("createImageBitmap", cib);
    const ctx = { drawImage: vi.fn() };
    const out = new Blob(["x"], { type: "image/jpeg" });
    const seen = stubCanvas(out, ctx);
    const f = file();

    const res = await processPhoto(f);

    expect(res.takenAt).toBe("2026-06-14T10:00:00+09:30");
    expect(res.blob).toBe(out);
    expect(cib).toHaveBeenCalledWith(f, { imageOrientation: "from-image" });
    expect(ctx.drawImage).toHaveBeenCalledWith(bmp, 0, 0, 1600, 1200);
    expect(bmp.close).toHaveBeenCalled();
    expect(seen).toEqual([{ width: 1600, height: 1200, type: "image/jpeg", quality: 0.82 }]);
  });

  test("file without EXIF falls to rung (c)", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue(bitmap()));
    stubCanvas(new Blob(["x"]));
    const now = new Date("2026-09-29T12:00:00Z");
    const res = await processPhoto(new File([new Uint8Array([1, 2, 3])], "p.png"), now);
    expect(res.takenAt).toBe(now.toISOString());
  });

  test("createImageBitmap rejects -> PhotoDecodeError", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn().mockRejectedValue(new DOMException("bad", "InvalidStateError")));
    stubCanvas(new Blob(["x"]));
    await expect(processPhoto(file())).rejects.toBeInstanceOf(PhotoDecodeError);
  });

  test("toBlob yields null -> PhotoDecodeError", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue(bitmap()));
    stubCanvas(null);
    await expect(processPhoto(file())).rejects.toBeInstanceOf(PhotoDecodeError);
  });

  test("getContext returns null -> PhotoDecodeError", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue(bitmap()));
    stubCanvas(new Blob(["x"]), null);
    await expect(processPhoto(file())).rejects.toBeInstanceOf(PhotoDecodeError);
  });
});
