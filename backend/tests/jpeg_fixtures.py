"""
Hand-built JPEG fixtures for photo tests — no imaging library needed.

`minimal_jpeg()` is a complete, decodable 8x8 greyscale baseline JPEG: SOI,
APP0/JFIF, DQT, SOF0, a DC and an AC Huffman table each holding one 1-bit code,
SOS, one byte of scan data (DC difference 0, then end-of-block) and EOI. Every
decoder renders it as a flat grey square. `variant` changes the quantisation
table so two uploads can carry different, equally valid bytes.

`segment(marker, payload)` builds one length-prefixed segment, for splicing
metadata (`APP1`/EXIF, `APP2`/ICC, `COM`, …) in front of the scan.
"""

from __future__ import annotations

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def segment(marker: int, payload: bytes) -> bytes:
    """`FF <marker>` plus a big-endian length that counts itself and `payload`."""
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


APP0_JFIF = segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
SOF0 = segment(0xC0, b"\x08\x00\x08\x00\x08\x01\x01\x11\x00")
DHT_DC = segment(0xC4, b"\x00" + b"\x01" + b"\x00" * 15 + b"\x00")
DHT_AC = segment(0xC4, b"\x10" + b"\x01" + b"\x00" * 15 + b"\x00")
SOS_HEADER = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
# DC code '0' (difference 0), AC code '0' (EOB), padded with 1-bits.
SCAN_DATA = b"\x3f"


def dqt(variant: int = 1) -> bytes:
    """A table-0 DQT whose 64 entries are all `variant` (1–255)."""
    return segment(0xDB, b"\x00" + bytes([variant]) * 64)


def header_segments(variant: int = 1) -> list[bytes]:
    """The kept segments between SOI and SOS, in order."""
    return [APP0_JFIF, dqt(variant), SOF0, DHT_DC, DHT_AC]


def minimal_jpeg(variant: int = 1) -> bytes:
    """A complete, decodable 8x8 JPEG with no metadata segments."""
    return SOI + b"".join(header_segments(variant)) + SOS_HEADER + SCAN_DATA + EOI


def jpeg_with_scan(scan: bytes) -> bytes:
    """
    A JPEG that walks cleanly to SOS and carries `scan` as its compressed data.

    Not decodable for arbitrary `scan`; for tests that care about size or exact
    stored bytes, not pixels. Every `FF` in `scan` is byte-stuffed to `FF 00`, as
    an encoder would, so the walker never reads a marker inside it. Carries no
    metadata, so storage keeps it byte-identical.
    """
    stuffed = scan.replace(b"\xff", b"\xff\x00")
    return SOI + b"".join(header_segments()) + SOS_HEADER + stuffed + EOI


# --------------------------------------------------------------------------
# Progressive and trailer fixtures (t-am-jpeg-after-sos, decision-log Entry 31)
# --------------------------------------------------------------------------

SOF2 = segment(0xC2, b"\x08\x00\x08\x00\x08\x01\x01\x11\x00")
# Spectral selection / successive approximation: DC first (Al=1), AC first, DC refinement.
SOS_DC_FIRST = segment(0xDA, b"\x01\x01\x00\x00\x00\x01")
SOS_AC_FIRST = segment(0xDA, b"\x01\x01\x00\x01\x3f\x00")
SOS_DC_REFINE = segment(0xDA, b"\x01\x01\x00\x00\x00\x10")
# Several bytes each, with FF00 stuffing and an RST, so truncations land inside scans.
PROGRESSIVE_SCANS = (
    b"\x7f\x12\xff\x00\x34",
    b"\x7f\x56\xff\xd3\x78\xff\x00",
    b"\x80\x9a\xff\x00\xbc",
)
ADOBE_APP14 = segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")  # 12 payload bytes


def progressive_jpeg(
    *,
    pre: bytes = b"",
    between: tuple[bytes, bytes] = (b"", b""),
    trailer: bytes = b"",
) -> bytes:
    """
    A three-scan progressive (SOF2) JPEG, with the AC table's DHT between scans 1 and 2.

    `pre` goes after APP0, before DQT; `between[0]` after scan 1 (before the
    DHT), `between[1]` after scan 2 (before the third SOS); `trailer` after EOI.
    With every insertion empty it carries no metadata and nothing after EOI.
    """
    s1, s2, s3 = PROGRESSIVE_SCANS
    return (
        SOI
        + APP0_JFIF
        + pre
        + dqt()
        + SOF2
        + DHT_DC
        + SOS_DC_FIRST
        + s1
        + between[0]
        + DHT_AC
        + SOS_AC_FIRST
        + s2
        + between[1]
        + SOS_DC_REFINE
        + s3
        + EOI
        + trailer
    )


def structure(data: bytes) -> list[tuple[int, bytes]]:
    """
    A test-side walk of a whole JPEG to its first EOI: `(marker, body)` per item.

    Length-bearing segments carry their payload; standalone markers carry b"";
    each scan's entropy data is reported as `(0x00, data)` straight after its
    SOS. The scan ends at the first `FF` followed by something other than
    `00`, `D0`-`D7` or `FF` (T.81 B.1.1.2 / B.1.1.5). Asserts if anything follows EOI.
    """
    assert data[:2] == SOI
    items: list[tuple[int, bytes]] = [(0xD8, b"")]
    pos = 2
    while True:
        assert data[pos] == 0xFF, f"no marker at offset {pos}"
        while data[pos + 1] == 0xFF:
            pos += 1
        marker = data[pos + 1]
        pos += 2
        if marker == 0xD9:
            items.append((marker, b""))
            assert pos == len(data), "bytes after EOI"
            return items
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            items.append((marker, b""))
            continue
        length = int.from_bytes(data[pos : pos + 2], "big")
        items.append((marker, data[pos + 2 : pos + length]))
        pos += length
        if marker == 0xDA:
            start = pos
            while not (data[pos] == 0xFF and data[pos + 1] not in (0x00, 0xFF, *range(0xD0, 0xD8))):
                pos += 1
            items.append((0x00, data[start:pos]))
