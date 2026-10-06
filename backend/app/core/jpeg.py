"""
JPEG check and metadata strip for photo uploads — pure Python, no imaging library.

**Context.** The server never stores photo metadata and accepts JPEG only
(decision-log Entries 29 and 31; `docs/api-contract.md`, "Photo upload: JPEG
only, metadata stripped, 15 MiB cap"). The client's canvas re-encode already
drops EXIF, but the server cannot rely on the client, so every upload route
calls `strip_metadata` on the bytes before they reach storage.

**How it works.** One forward walk over the whole file, ending at the first
top-level End-of-Image (`EOI`, `FF D9`) that follows at least one Start-of-Scan
(`SOS`, `FF DA`). At every top-level marker — before the first scan and between
progressive scans — the segment is either copied verbatim or dropped:

- Dropped: COM (`FF FE`); APP1–APP13 and APP15 (`FF E1`–`FF ED`, `FF EF`:
  EXIF/GPS, XMP, ICC, MPF, Photoshop IRB); an APP0 that is not exactly a plain
  16-byte `JFIF\\0` header (JFXX, or JFIF carrying a thumbnail); an APP14 that
  is not exactly the 14-byte `Adobe` segment.
- Kept: SOI, plain JFIF APP0, Adobe APP14, DQT, SOF*, DHT, DAC, DRI, DNL, SOS
  headers and any other length-bearing marker. TEM and RST0–7 outside a scan
  have no length and are walked past.

The Adobe APP14 is kept because decoders read its colour-transform flag to
tell YCbCr from RGB and YCCK from CMYK (libjpeg-turbo `jdapimin.c`); dropping
it would shift colours. Its 12 payload bytes carry nothing about the rider.

After each SOS header the entropy-coded data is skipped with one compiled
search, `_SCAN_END = re.compile(rb"\\xff[^\\x00\\xd0-\\xd7\\xff]")`: `FF 00`
stuffing, RST0–7 and fill `FF`s never match, so the first match is the next
real marker. The pattern has no `\\xff+` quantifier, so a long run of `FF`
cannot make the search backtrack quadratically. Fill bytes in front of that
marker stay in the kept scan run (valid per ITU T.81 B.1.1.2). Everything
after the terminating `EOI` — MPF secondary images, Motion Photo video, gain
maps, any trailer — is discarded. A JPEG with nothing to strip comes back
byte-identical, as the same object.

Anything that does not walk cleanly to that `EOI` raises `JpegError` (never a
best-effort repair): a missing `FF D8 FF` prefix, a byte that is not a marker
where one must be (including after TEM), a length field below 2 or pointing
past the end, SOI or `FF 00` at a marker position, an EOI before any scan, or
input that ends first — inside a header, a scan, or a run of fill bytes.

**Bounds.** Each step advances by at least two bytes and every byte scan runs
in C, so time is linear in the input. Kept bytes are copied as whole runs
between dropped segments into one buffer, never per segment, so extra memory
stays at about twice the output whatever the input claims about its lengths.

**Related.** `app/api/routes/photos.py` enforces `MAX_PHOTO_BYTES` and maps
`JpegError` to `422 VALIDATION_ERROR`.
"""

from __future__ import annotations

import re

# 15 MiB. The file part may be exactly this long; one byte more is a 422.
MAX_PHOTO_BYTES = 15 * 1024 * 1024

_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA
_TEM = 0x01
_RST0, _RST7 = 0xD0, 0xD7
_APP0, _APP14 = 0xE0, 0xEE
_APP1, _APP15 = 0xE1, 0xEF
_COM = 0xFE

# Segment length (counting itself) of the only APP0 and APP14 forms kept.
_JFIF_LENGTH = 16
_ADOBE_LENGTH = 14

# The first byte after a run of fill FFs; finds a marker byte without a
# quantifier, so megabytes of FF padding cost one C-level search.
_NOT_FF = re.compile(rb"[^\xff]")
# The next real marker inside entropy-coded data (see the module docstring).
_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


class JpegError(ValueError):
    """The input is not a JPEG this walker can take cleanly to its EOI. The text is for logs."""


def _marker_at(data: bytes, pos: int) -> int:
    """Index of the marker byte for the marker (with optional fill) starting at `pos`."""
    if pos >= len(data):
        raise JpegError("ends before EOI")
    if data[pos] != 0xFF:
        raise JpegError(f"expected a marker at offset {pos}")
    found = _NOT_FF.search(data, pos + 1)
    if found is None:
        raise JpegError("ends inside a marker")
    return found.start()


def _segment_end(data: bytes, pos: int, marker: int) -> int:
    """End offset of the length-bearing segment whose length field starts at `pos`."""
    if pos + 2 > len(data):
        raise JpegError("ends inside a segment length")
    length = (data[pos] << 8) | data[pos + 1]
    if length < 2:
        raise JpegError(f"segment FF {marker:02X} has length {length}")
    end = pos + length
    if end > len(data):
        raise JpegError(f"segment FF {marker:02X} runs past the end of the input")
    return end


def _is_dropped(data: bytes, marker: int, pos: int, end: int) -> bool:
    """Whether the segment with length field at `pos` and end `end` is metadata."""
    length = end - pos
    if marker == _APP0:
        return not (length == _JFIF_LENGTH and data.startswith(b"JFIF\x00", pos + 2))
    if marker == _APP14:
        return not (length == _ADOBE_LENGTH and data.startswith(b"Adobe", pos + 2))
    return _APP1 <= marker <= _APP15 or marker == _COM


def _scan_end(data: bytes, pos: int) -> int:
    """Offset of the FF of the first real marker after the scan data starting at `pos`."""
    found = _SCAN_END.search(data, pos)
    if found is None:
        raise JpegError("ends inside a scan")
    return found.start()


def strip_metadata(data: bytes) -> bytes:
    """
    Return `data` up to its first EOI, without COM and metadata APPn segments.

    Raises `JpegError` if `data` is not a JPEG that walks cleanly to an EOI
    after at least one SOS.
    """
    if len(data) < 3 or data[0] != 0xFF or data[1] != _SOI or data[2] != 0xFF:
        raise JpegError("does not start with FF D8 FF")

    # Kept bytes are copied one run at a time, not one segment at a time: a
    # run starts at `kept_from` and is flushed into one growing buffer only
    # when a dropped segment begins. Per-segment `bytes` slices let 15 MiB of
    # 2-byte markers build millions of small objects (about 1 GiB peak); a
    # list of runs would do the same to input that alternates kept and dropped
    # segments. One buffer keeps the extra memory to about twice the output.
    view = memoryview(data)
    out: bytearray | None = None
    kept_from = 0
    seen_scan = False
    pos = 2
    while True:
        segment_start = pos
        pos = _marker_at(data, pos)
        marker = data[pos]
        pos += 1

        if marker == _EOI:
            if not seen_scan:
                raise JpegError("reaches EOI before any SOS")
            break
        if marker == 0x00 or marker == _SOI:
            raise JpegError(f"marker FF {marker:02X} is not valid at offset {pos - 1}")
        if marker == _TEM or _RST0 <= marker <= _RST7:
            # Standalone markers: no length field, nothing to strip.
            continue

        end = _segment_end(data, pos, marker)
        if marker == _SOS:
            seen_scan = True
            pos = _scan_end(data, end)
            continue
        if _is_dropped(data, marker, pos, end):
            if out is None:
                out = bytearray()
            out += view[kept_from:segment_start]
            kept_from = end
        pos = end

    if out is not None:
        out += view[kept_from:pos]
        return bytes(out)
    if pos == len(data):
        return data
    return data[:pos]
