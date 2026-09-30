"""
Unit tests for `app.core.jpeg.strip_metadata` — the walker on its own, no HTTP.

Task `t-am-photo-exif-strip`, obligation 12 (decision-log Entry 29): a stored
JPEG carries no APP1–APP15 or COM segment before SOS; APP0, DQT, SOF, DHT, SOS
and the image data are byte-identical; anything that is not a JPEG walking
cleanly to an SOS is refused.
"""

from __future__ import annotations

import time
import tracemalloc

import pytest
from jpeg_fixtures import (
    APP0_JFIF,
    DHT_AC,
    DHT_DC,
    EOI,
    SCAN_DATA,
    SOF0,
    SOI,
    SOS_HEADER,
    dqt,
    minimal_jpeg,
    segment,
)

from app.core.jpeg import JpegError, strip_metadata

EXIF_WITH_GPS = segment(
    0xE1,
    b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08" + b"GPSLatitude=-33.8688;GPSLongitude=151.2093",
)
XMP = segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>secret</x:xmpmeta>")
ICC = segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + b"\x00" * 40)
IPTC = segment(0xED, b"Photoshop 3.0\x008BIM\x04\x04" + b"caption")
APP15 = segment(0xEF, b"vendor")
COMMENT = segment(0xFE, b"taken at 12 Home Street")
DRI = segment(0xDD, b"\x00\x04")


def metadata_positions(data: bytes) -> list[tuple[int, int]]:
    """(offset, marker) of every APP1–APP15/COM marker the walk would meet before SOS."""
    found = []
    pos = 2
    while True:
        while data[pos] == 0xFF and data[pos + 1] == 0xFF:
            pos += 1
        marker = data[pos + 1]
        if marker == 0xDA:
            return found
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2
            continue
        if 0xE1 <= marker <= 0xEF or marker == 0xFE:
            found.append((pos, marker))
        pos += 2 + int.from_bytes(data[pos + 2 : pos + 4], "big")


def test_fixture_has_the_expected_layout():
    jpeg = minimal_jpeg()
    assert jpeg.startswith(SOI + APP0_JFIF)
    assert jpeg.endswith(SOS_HEADER + SCAN_DATA + EOI)


def test_a_clean_jpeg_comes_back_byte_identical():
    jpeg = minimal_jpeg()
    assert strip_metadata(jpeg) == jpeg


def test_every_metadata_segment_is_dropped_and_everything_else_kept():
    kept = [APP0_JFIF, dqt(), SOF0, DHT_DC, DHT_AC, DRI]
    dirty = (
        SOI
        + APP0_JFIF
        + EXIF_WITH_GPS
        + XMP
        + ICC
        + dqt()
        + IPTC
        + SOF0
        + COMMENT
        + DHT_DC
        + APP15
        + DHT_AC
        + DRI
        + SOS_HEADER
        + SCAN_DATA
        + EOI
    )
    assert metadata_positions(dirty)  # the fixture really carries metadata

    stripped = strip_metadata(dirty)

    assert stripped == SOI + b"".join(kept) + SOS_HEADER + SCAN_DATA + EOI
    assert metadata_positions(stripped) == []
    assert b"GPS" not in stripped
    assert b"Home Street" not in stripped


@pytest.mark.parametrize("marker", [*range(0xE1, 0xF0), 0xFE])
def test_each_stripped_marker_is_dropped(marker):
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + segment(marker, b"meta") + jpeg[2:]
    assert strip_metadata(dirty) == jpeg


def test_app0_is_kept():
    jpeg = minimal_jpeg()
    extra_app0 = segment(0xE0, b"JFXX\x00\x10")
    dirty = jpeg[:2] + extra_app0 + jpeg[2:]
    assert strip_metadata(dirty) == dirty


def test_bytes_after_sos_are_copied_untouched_even_if_they_look_like_metadata():
    # Entropy data with stuffing, RST markers, and APP1/COM-looking byte pairs:
    # none of it is parsed.
    scan = b"\x12\xff\x00\x34\xff\xd0\x56\xff\xd1\xff\xe1\x00\x04ab\xff\xfe\x00\x02" + EOI
    jpeg = SOI + APP0_JFIF + dqt() + SOF0 + DHT_DC + DHT_AC + SOS_HEADER + scan
    assert strip_metadata(jpeg) == jpeg


def test_trailing_bytes_after_eoi_are_copied():
    jpeg = minimal_jpeg() + b"trailer"
    assert strip_metadata(jpeg) == jpeg


def test_fill_bytes_before_a_kept_marker_are_kept():
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + b"\xff\xff\xff" + jpeg[2:]
    assert strip_metadata(dirty) == dirty


def test_fill_bytes_before_a_stripped_marker_go_with_it():
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + b"\xff\xff" + EXIF_WITH_GPS + jpeg[2:]
    assert strip_metadata(dirty) == jpeg


def test_fill_bytes_before_sos_are_kept():
    head = SOI + APP0_JFIF + dqt() + SOF0 + DHT_DC + DHT_AC
    jpeg = head + b"\xff\xff" + SOS_HEADER + SCAN_DATA + EOI
    assert strip_metadata(jpeg) == jpeg


def test_standalone_rst_and_tem_markers_before_sos_are_walked_past():
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + b"\xff\xd0" + b"\xff\x01" + COMMENT + b"\xff\xd7" + jpeg[2:]
    assert strip_metadata(dirty) == jpeg[:2] + b"\xff\xd0\xff\x01\xff\xd7" + jpeg[2:]


def test_marker_lookalikes_inside_a_segment_payload_are_not_parsed():
    # A metadata payload that itself contains FF D8 / FF DA must not be parsed.
    payload = b"\xff\xda\x00\x02\xff\xd8\xff\xe0"
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + segment(0xE1, payload) + jpeg[2:]
    assert strip_metadata(dirty) == jpeg


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\xff", id="one-byte"),
        pytest.param(b"\xff\xd8", id="soi-only"),
        pytest.param(b"\xff\xd8\xff", id="soi-then-ff"),
        pytest.param(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20, id="png"),
        pytest.param(b"GIF89a\x01\x00\x01\x00\x00\x00\x00;", id="gif"),
        pytest.param(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", id="heic"),
        pytest.param(b"\xff\xd8\x00\x10JFIF", id="no-marker-after-soi"),
        pytest.param(SOI + APP0_JFIF + dqt() + SOF0 + EOI, id="eoi-before-sos"),
        pytest.param(SOI + APP0_JFIF + dqt() + SOF0 + DHT_DC + DHT_AC, id="no-sos"),
        pytest.param(SOI + APP0_JFIF + b"junk" + SOS_HEADER + EOI, id="garbage-between"),
        pytest.param(SOI + SOI + APP0_JFIF + SOS_HEADER + EOI, id="second-soi"),
        pytest.param(SOI + b"\xff\x00" + SOS_HEADER + EOI, id="stuffed-zero-marker"),
        pytest.param(SOI + b"\xff\xe1\x00\x00" + SOS_HEADER + EOI, id="length-zero"),
        pytest.param(SOI + b"\xff\xe1\x00\x01" + SOS_HEADER + EOI, id="length-one"),
        pytest.param(SOI + b"\xff\xe1\xff\xff" + b"short", id="length-past-eof"),
        pytest.param(SOI + b"\xff\xe1\x00", id="truncated-length"),
        pytest.param(SOI + APP0_JFIF + b"\xff", id="truncated-marker"),
        pytest.param(SOI + APP0_JFIF + b"\xff\xff\xff", id="only-fill-at-end"),
        pytest.param(SOI + APP0_JFIF + b"\xff\xda\x00\x08\x01", id="truncated-sos-header"),
    ],
)
def test_non_jpeg_malformed_and_truncated_inputs_are_rejected(data):
    with pytest.raises(JpegError):
        strip_metadata(data)


@pytest.mark.parametrize("cut", range(2, len(minimal_jpeg()) - 3))
def test_every_truncation_before_the_scan_is_rejected(cut):
    jpeg = minimal_jpeg()
    sos_end = len(jpeg) - len(SCAN_DATA + EOI)
    if cut >= sos_end:
        pytest.skip("cut is inside the scan data, which is copied without parsing")
    with pytest.raises(JpegError):
        strip_metadata(jpeg[:cut])


def test_jpeg_error_is_a_value_error():
    assert issubclass(JpegError, ValueError)


# Adversarial sizes: the walk must stay linear. The bounds are generous on
# purpose; a quadratic walk over these inputs would take minutes, not seconds.
FIFTEEN_MIB = 15 * 1024 * 1024


def _timed(data: bytes) -> float:
    start = time.perf_counter()
    try:
        strip_metadata(data)
    except JpegError:
        pass
    return time.perf_counter() - start


def test_a_megabytes_long_fill_run_is_linear():
    assert _timed(SOI + b"\xff" * FIFTEEN_MIB) < 5


def test_a_long_scan_is_copied_without_walking_it():
    jpeg = minimal_jpeg()
    big = jpeg[:-2] + b"\x00" * FIFTEEN_MIB + EOI
    assert _timed(big) < 5
    assert strip_metadata(big) == big


def test_many_tiny_metadata_segments_are_linear():
    tiny = segment(0xFE, b"")  # 4 bytes, the smallest legal segment
    count = 1_000_000
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + tiny * count + jpeg[2:]
    assert _timed(dirty) < 10
    assert strip_metadata(dirty) == jpeg


def test_many_standalone_markers_are_linear():
    count = 1_000_000
    jpeg = minimal_jpeg()
    dirty = jpeg[:2] + b"\xff\xd0" * count + jpeg[2:]
    assert _timed(dirty) < 10


def test_back_to_back_standalone_markers_do_not_blow_up_memory():
    """
    512 KiB of `FF D0`: per-segment copying peaked near 64x the input (about
    1 GiB at 15 MiB). Nothing is dropped here, so the walk should hand back
    the input itself; the bound still allows a full copy plus slack.
    """
    jpeg = minimal_jpeg()
    size = 512 * 1024
    crafted = jpeg[:2] + b"\xff\xd0" * (size // 2) + jpeg[2:]

    tracemalloc.start()
    try:
        out = strip_metadata(crafted)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert out == crafted
    assert peak < 3 * len(crafted)


def test_alternating_kept_and_dropped_segments_stay_within_twice_the_output():
    """A list of kept runs would still be millions of tiny objects here."""
    jpeg = minimal_jpeg()
    unit = b"\xff\xd0" + segment(0xFE, b"")
    crafted = jpeg[:2] + unit * (256 * 1024 // len(unit)) + jpeg[2:]

    tracemalloc.start()
    try:
        out = strip_metadata(crafted)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert out == jpeg[:2] + b"\xff\xd0" * (256 * 1024 // len(unit)) + jpeg[2:]
    assert peak < 3 * len(out) + 64 * 1024
