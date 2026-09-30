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
    stored bytes, not pixels. Carries no metadata, so storage keeps it byte-identical.
    """
    return SOI + b"".join(header_segments()) + SOS_HEADER + scan + EOI
