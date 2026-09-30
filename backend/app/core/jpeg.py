"""
JPEG check and metadata strip for photo uploads — pure Python, no imaging library.

**Context.** The server never stores photo metadata and accepts JPEG only
(decision-log Entry 29; `docs/api-contract.md`, "Photo upload: JPEG only,
metadata stripped, 15 MiB cap"). The client's canvas re-encode already drops
EXIF, but the server cannot rely on the client, so every upload route calls
`strip_metadata` on the bytes before they reach storage.

**How it works.** A single forward walk over the segments in front of the
first Start-of-Scan (`SOS`, `FF DA`). Each segment is either copied verbatim
(SOI, APP0/JFIF, DQT, SOF*, DHT, DRI, …) or dropped (APP1–APP15, `FF E1`–`FF EF`,
which carry EXIF/GPS, XMP, ICC and Photoshop IRB; and COM, `FF FE`). From the
`SOS` marker onward everything is copied untouched: the entropy-coded data, its
RST markers and `FF 00` stuffing, and whatever follows. Fill bytes (a run of
`FF` in front of a marker, ITU T.81 B.1.1.2) are kept with the marker they pad,
so a JPEG with nothing to strip comes back byte-identical — as the same object.

Anything that does not walk cleanly to an `SOS` raises `JpegError`: a missing
`FF D8 FF` prefix, a byte that is not a marker where one must be, a length
field below 2 or pointing past the end, a second SOI, an EOI before any scan,
or input that simply ends. The walker reads only headers — each step advances
by at least two bytes, and kept bytes are copied as whole runs between dropped
segments — so time is linear in the input and extra memory is bounded by the
output, whatever the input claims about its own lengths.

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
_APP1, _APP15 = 0xE1, 0xEF
_COM = 0xFE

# A run of fill bytes plus the marker's own FF, matched in C rather than a
# Python loop, so megabytes of FF padding cost one regex call.
_FF_RUN = re.compile(rb"\xff+")


class JpegError(ValueError):
    """The input is not a JPEG this walker can take to its first scan. The text is for logs."""


def _is_stripped(marker: int) -> bool:
    return _APP1 <= marker <= _APP15 or marker == _COM


def strip_metadata(data: bytes) -> bytes:
    """
    Return `data` without APP1–APP15 and COM segments before the first SOS.

    Raises `JpegError` if `data` is not a JPEG that walks cleanly to an SOS.
    """
    size = len(data)
    if size < 3 or data[0] != 0xFF or data[1] != _SOI or data[2] != 0xFF:
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
    pos = 2
    while pos < size:
        segment_start = pos
        run = _FF_RUN.match(data, pos)
        if run is None:
            raise JpegError(f"expected a marker at offset {pos}")
        pos = run.end()
        if pos >= size:
            raise JpegError("ends inside a marker")
        marker = data[pos]
        pos += 1

        if marker == 0x00 or marker == _SOI:
            raise JpegError(f"marker FF {marker:02X} is not valid before the first scan")
        if marker == _EOI:
            raise JpegError("reaches EOI before any SOS")
        if marker == _TEM or _RST0 <= marker <= _RST7:
            # Standalone markers: no length field, nothing to strip.
            continue

        if pos + 2 > size:
            raise JpegError("ends inside a segment length")
        length = (data[pos] << 8) | data[pos + 1]
        if length < 2:
            raise JpegError(f"segment FF {marker:02X} has length {length}")
        end = pos + length
        if end > size:
            raise JpegError(f"segment FF {marker:02X} runs past the end of the input")

        if marker == _SOS:
            # The scan header, the compressed image and everything after it,
            # untouched.
            if out is None:
                return data
            out += view[kept_from:]
            return bytes(out)
        if _is_stripped(marker):
            if out is None:
                out = bytearray()
            out += view[kept_from:segment_start]
            kept_from = end
        pos = end

    raise JpegError("ends before any SOS")
