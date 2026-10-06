"""
Unit tests for `app.core.jpeg.strip_metadata` — the walker on its own, no HTTP.

Task `t-am-photo-exif-strip`, obligation 12 (decision-log Entry 29), widened by
`t-am-jpeg-after-sos` (Entry 31): a stored JPEG carries no COM or metadata APPn
segment anywhere and nothing after its first EOI; plain JFIF APP0, Adobe APP14,
DQT, SOF, DHT, SOS and the scan data are byte-identical; anything that is not a
JPEG walking cleanly to an EOI after an SOS is refused.
"""

from __future__ import annotations

import time
import tracemalloc

import pytest
from jpeg_fixtures import (
    ADOBE_APP14,
    APP0_JFIF,
    DHT_AC,
    DHT_DC,
    EOI,
    PROGRESSIVE_SCANS,
    SCAN_DATA,
    SOF0,
    SOI,
    SOS_HEADER,
    dqt,
    minimal_jpeg,
    progressive_jpeg,
    segment,
    structure,
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


def test_plain_jfif_app0_is_kept():
    jpeg = minimal_jpeg()
    assert len(APP0_JFIF) == 2 + 16
    assert strip_metadata(jpeg) is jpeg


def test_jfxx_app0_is_dropped():
    jpeg = minimal_jpeg()
    extra_app0 = segment(0xE0, b"JFXX\x00\x10")
    dirty = jpeg[:2] + extra_app0 + jpeg[2:]
    assert strip_metadata(dirty) == jpeg


def test_scan_stuffing_rst_and_fill_are_copied_untouched():
    # FF00 stuffing, RST0-7 and fill FFs inside entropy data are not markers.
    scan = b"\x12\xff\x00\x34\xff\xd0\x56\xff\xd7\x78\xff\xff\xff"
    jpeg = SOI + APP0_JFIF + dqt() + SOF0 + DHT_DC + DHT_AC + SOS_HEADER + scan + EOI
    assert strip_metadata(jpeg) is jpeg


def test_metadata_between_scans_is_dropped():
    head = SOI + APP0_JFIF + dqt() + SOF0 + DHT_DC + DHT_AC
    tail = SOS_HEADER + SCAN_DATA + DHT_AC + SOS_HEADER + SCAN_DATA + EOI
    dirty = head + SOS_HEADER + SCAN_DATA + EXIF_WITH_GPS + COMMENT + tail
    assert strip_metadata(dirty) == head + SOS_HEADER + SCAN_DATA + tail


def test_trailing_bytes_after_eoi_are_discarded():
    jpeg = minimal_jpeg()
    assert strip_metadata(jpeg + b"trailer") == jpeg


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


@pytest.mark.parametrize("cut", range(len(minimal_jpeg())))
def test_every_truncation_is_rejected(cut):
    jpeg = minimal_jpeg()
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


# --------------------------------------------------------------------------
# t-am-jpeg-after-sos (decision-log Entry 31): progressive files, trailers,
# the APP0/APP14 keep rules, and full-file rejection. Written from the contract
# section "Photo upload: JPEG only, metadata stripped, 15 MiB cap" and the AC.
# --------------------------------------------------------------------------

CANARY = b"CANARY-7f3a9c"


def canary_app1(where: str) -> bytes:
    return segment(0xE1, b"Exif\x00\x00MM\x00\x2a" + CANARY + where.encode())


def canary_com(where: str) -> bytes:
    return segment(0xFE, CANARY + where.encode())


MPF_APP2 = segment(0xE2, b"MPF\x00MM\x00\x2a\x00\x00\x00\x08" + CANARY + b"mpf-index")


def secondary_jpeg(label: str) -> bytes:
    """A complete second JPEG, as an MPF / gain-map image appended after EOI."""
    body = minimal_jpeg(2)
    return body[:2] + canary_app1(f"secondary-{label}") + body[2:]


def test_progressive_fixture_has_the_expected_structure():
    markers = [m for m, _ in structure(progressive_jpeg())]
    assert markers == [
        0xD8, 0xE0, 0xDB, 0xC2, 0xC4, 0xDA, 0x00, 0xC4, 0xDA, 0x00, 0xDA, 0x00, 0xD9,
    ]  # fmt: skip


def test_a_clean_progressive_jpeg_comes_back_as_the_same_object():
    jpeg = progressive_jpeg()
    assert strip_metadata(jpeg) is jpeg


def test_app1_and_com_between_progressive_scans_are_dropped_scans_and_dht_kept():
    """AC1: between-scan metadata goes; every scan, SOS header and DHT is byte-identical."""
    dirty = progressive_jpeg(
        pre=canary_app1("pre"),
        between=(
            canary_app1("between-1") + canary_com("between-1"),
            canary_com("between-2") + canary_app1("between-2"),
        ),
    )
    out = strip_metadata(dirty)
    assert out == progressive_jpeg()
    assert structure(out) == structure(progressive_jpeg())
    scans = [body for marker, body in structure(out) if marker == 0x00]
    assert scans == list(PROGRESSIVE_SCANS)
    assert CANARY not in out


def test_mpf_app2_and_secondary_jpeg_after_eoi_are_cut_to_exactly_the_primary():
    """AC2: MPF index dropped, secondary image (with its own EXIF) discarded."""
    primary = progressive_jpeg()
    dirty = progressive_jpeg(pre=MPF_APP2, trailer=secondary_jpeg("mpf"))
    out = strip_metadata(dirty)
    assert out == primary
    assert CANARY not in out
    assert b"MPF" not in out


@pytest.mark.parametrize(
    "trailer",
    [
        pytest.param(
            b"\x00\x00\x00\x1cftypmp42\x00\x00\x00\x00isommp42" + CANARY + b"\x00" * 64,
            id="motion-photo-mp4",
        ),
        pytest.param(
            minimal_jpeg(3)[:2]
            + segment(0xE2, b"urn:iso:std:iso:ts:21496:-1\x00" + CANARY)
            + canary_app1("gain-map")
            + minimal_jpeg(3)[2:],
            id="ultra-hdr-gain-map",
        ),
        pytest.param(b"\xff\xd9\xff\xe1" + CANARY, id="marker-lookalikes"),
    ],
)
def test_arbitrary_bytes_after_eoi_are_discarded(trailer):
    """AC3."""
    for jpeg in (minimal_jpeg(), progressive_jpeg()):
        out = strip_metadata(jpeg + trailer)
        assert out == jpeg
        assert CANARY not in out


@pytest.mark.parametrize("rst", range(0xD0, 0xD8), ids=lambda m: f"RST{m - 0xD0}")
def test_every_rst_marker_inside_a_scan_is_copied_unchanged(rst):
    """AC4: RST0-7 are part of the entropy-coded data, never a segment boundary."""
    head = minimal_jpeg()[: -len(SCAN_DATA + EOI)]
    jpeg = head + b"\x12\xff\x00\x34\xff" + bytes([rst]) + b"\x56\xff\x00" + EOI
    assert strip_metadata(jpeg) is jpeg


def test_fill_bytes_after_a_scan_before_a_kept_marker_are_copied_unchanged():
    """AC4: fill FFs ahead of the between-scan DHT stay with the scan."""
    jpeg = progressive_jpeg(between=(b"\xff\xff\xff", b"\xff\xff"))
    assert strip_metadata(jpeg) is jpeg


def test_fill_bytes_after_a_scan_stay_when_the_following_marker_is_dropped():
    """Entry 31 point 3: fill before the marker stays in the kept scan run."""
    dirty = progressive_jpeg(between=(b"\xff\xff" + canary_com("after-fill"), b""))
    out = strip_metadata(dirty)
    assert out == progressive_jpeg(between=(b"\xff\xff", b""))
    assert CANARY not in out


def test_adobe_app14_of_length_14_is_kept():
    """AC5: decoders need its colour-transform flag."""
    assert len(ADOBE_APP14) == 2 + 14
    for jpeg in (minimal_jpeg(), progressive_jpeg()):
        with_adobe = jpeg[:2] + ADOBE_APP14 + jpeg[2:]
        assert strip_metadata(with_adobe) is with_adobe
        dirty = jpeg[:2] + canary_app1("pre") + ADOBE_APP14 + jpeg[2:]
        assert strip_metadata(dirty) == with_adobe


@pytest.mark.parametrize(
    "app14",
    [
        pytest.param(segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00"), id="adobe-len-13"),
        pytest.param(segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01\x00"), id="adobe-len-15"),
        pytest.param(segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01" + CANARY), id="adobe-long"),
        pytest.param(segment(0xEE, b"Adobf\x00\x64\x00\x00\x00\x00\x01"), id="other-id-len-14"),
        pytest.param(segment(0xEE, b"ADOBE\x00\x64\x00\x00\x00\x00\x01"), id="uppercase-id"),
        pytest.param(segment(0xEE, b"Ducky\x00\x64\x00\x00\x00\x00\x01"), id="ducky-len-14"),
    ],
)
def test_any_other_app14_is_dropped(app14):
    for jpeg in (minimal_jpeg(), progressive_jpeg()):
        assert strip_metadata(jpeg[:2] + app14 + jpeg[2:]) == jpeg


@pytest.mark.parametrize(
    "app0",
    [
        pytest.param(
            segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x01\x01" + b"\x10\x20\x30"),
            id="jfif-with-1x1-thumbnail",
        ),
        pytest.param(
            segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + CANARY),
            id="jfif-with-trailing-bytes",
        ),
        pytest.param(segment(0xE0, b"JFXX\x00\x10" + CANARY), id="jfxx"),
        pytest.param(
            segment(0xE0, b"JFIX\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"), id="len-16-not-jfif"
        ),
    ],
)
def test_app0_other_than_a_plain_16_byte_jfif_is_dropped(app0):
    """AC5: JFXX and thumbnail-bearing JFIF go; the plain JFIF APP0 after it stays."""
    for jpeg in (minimal_jpeg(), progressive_jpeg()):
        out = strip_metadata(jpeg[:2] + app0 + jpeg[2:])
        assert out == jpeg
        assert CANARY not in out


def test_a_canary_in_every_droppable_location_never_survives():
    """Privacy check across the whole file: pre-scan, between scans, MPF, trailer."""
    dirty = progressive_jpeg(
        pre=canary_app1("pre") + canary_com("pre") + MPF_APP2
        + segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + CANARY)
        + segment(0xED, b"Photoshop 3.0\x008BIM" + CANARY)
        + segment(0xE0, b"JFXX\x00\x10" + CANARY),
        between=(
            canary_app1("between-1") + canary_com("between-1"),
            segment(0xEF, CANARY) + canary_app1("between-2"),
        ),
        trailer=secondary_jpeg("mpf") + b"\x00\x00\x00\x1cftypmp42" + CANARY,
    )  # fmt: skip
    out = strip_metadata(dirty)
    assert CANARY not in out
    assert out == progressive_jpeg()


@pytest.mark.parametrize("cut", range(len(progressive_jpeg())))
def test_every_truncation_of_a_progressive_jpeg_is_rejected(cut):
    """AC6: at any offset, including inside and between scans."""
    with pytest.raises(JpegError):
        strip_metadata(progressive_jpeg()[:cut])


_PROGRESSIVE_HEAD = SOI + APP0_JFIF + dqt()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(progressive_jpeg()[:-2], id="no-eoi"),
        pytest.param(progressive_jpeg()[:-2] + b"\xff\xff\xff", id="only-fill-at-end"),
        pytest.param(progressive_jpeg()[:-2] + b"\x00" * 64, id="scan-runs-to-end"),
        pytest.param(progressive_jpeg(between=(SOI, b"")), id="soi-after-scan"),
        pytest.param(progressive_jpeg(between=(b"\xff\x01junk", b"")), id="tem-then-junk"),
        pytest.param(progressive_jpeg(between=(b"\xff\x01\xff\x00", b"")), id="tem-then-ff00"),
        pytest.param(progressive_jpeg(between=(b"\xff\xe1\xff\xff", b"")), id="length-past-end"),
        pytest.param(progressive_jpeg(between=(b"\xff\xfe\x00\x01", b"")), id="length-one"),
        pytest.param(progressive_jpeg(between=(b"\xff\xfe\x00\x00", b"")), id="length-zero"),
        pytest.param(
            progressive_jpeg()[:-2] + b"\xff\xe1\x00\x10" + CANARY, id="length-past-end-at-tail"
        ),
        pytest.param(
            _PROGRESSIVE_HEAD + EOI + progressive_jpeg()[len(_PROGRESSIVE_HEAD) :],
            id="eoi-before-sos",
        ),
    ],
)
def test_malformed_progressive_input_is_rejected(data):
    """AC6: never repaired, never cut short to a 'good enough' prefix."""
    with pytest.raises(JpegError):
        strip_metadata(data)


# AC7: 15 MiB adversarial inputs, each well under 5 s, and bounded memory.

_BASE_HEAD = minimal_jpeg()[: -len(SCAN_DATA + EOI)]  # SOI .. SOS header
_COM_EMPTY = segment(0xFE, b"")
_RST_CYCLE = b"".join(bytes([0xFF, m]) for m in range(0xD0, 0xD8))
_SCAN_UNIT = SOS_HEADER + SCAN_DATA


def _all_ff00(size: int) -> tuple[bytes, bytes]:
    data = _BASE_HEAD + b"\xff\x00" * (size // 2) + EOI
    return data, data


def _all_rst(size: int) -> tuple[bytes, bytes]:
    data = _BASE_HEAD + _RST_CYCLE * (size // len(_RST_CYCLE)) + EOI
    return data, data


def _many_scans(size: int) -> tuple[bytes, bytes]:
    data = _BASE_HEAD + SCAN_DATA + _SCAN_UNIT * (size // len(_SCAN_UNIT)) + EOI
    return data, data


def _scans_and_com(size: int) -> tuple[bytes, bytes]:
    n = size // len(_COM_EMPTY + _SCAN_UNIT)
    dirty = _BASE_HEAD + SCAN_DATA + (_COM_EMPTY + _SCAN_UNIT) * n + EOI
    clean = _BASE_HEAD + SCAN_DATA + _SCAN_UNIT * n + EOI
    return dirty, clean


_ADVERSARIAL = [
    pytest.param(_all_ff00, id="all-ff00"),
    pytest.param(_all_rst, id="all-rst"),
    pytest.param(_many_scans, id="many-minimal-scans"),
    pytest.param(_scans_and_com, id="scans-alternating-with-com"),
]


@pytest.mark.parametrize("build", _ADVERSARIAL)
def test_fifteen_mib_adversarial_scans_are_stripped_in_under_five_seconds(build):
    dirty, clean = build(FIFTEEN_MIB)
    start = time.perf_counter()
    out = strip_metadata(dirty)
    elapsed = time.perf_counter() - start
    assert elapsed < 5
    assert out == clean


def test_fifteen_mib_of_fill_in_a_scan_with_no_eoi_is_rejected_in_under_five_seconds():
    data = _BASE_HEAD + SCAN_DATA + b"\xff" * FIFTEEN_MIB
    start = time.perf_counter()
    with pytest.raises(JpegError):
        strip_metadata(data)
    assert time.perf_counter() - start < 5


@pytest.mark.parametrize("build", _ADVERSARIAL)
def test_adversarial_scans_stay_within_three_times_the_output(build):
    dirty, clean = build(512 * 1024)
    tracemalloc.start()
    try:
        out = strip_metadata(dirty)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out == clean
    assert peak < 3 * len(out) + 64 * 1024
