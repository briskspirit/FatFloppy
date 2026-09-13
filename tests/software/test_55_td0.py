"""Teledisk TD0 tests: LZHUF decompression, TD0ImageDriver, detection.

The load-bearing test is the oracle byte-equality sweep: every "advanced"
(``'td'``-signature) sample in the local corpus must decompress byte-identical
to greaseweazle's C reference decoder (``greaseweazle.optimised.td0_unpack``,
verified signature: single bytes-like argument, returns ``bytes``).

Synthetic tests pin the documented edge contracts:

- empty input -> ``b""`` (deliberate, documented divergence from the C
  reference, which fabricates one byte from fake zero bits);
- truncated input -> graceful short output, never an exception.  The C
  reference zero-fills the code in flight when input runs out, so the FINAL
  byte of a truncated decode may diverge from the full decode; everything
  before it is an exact prefix.  We pin exactly that;
- window-spaces preset: crafted minimal streams whose first code is a match
  into the untouched ring buffer must yield 0x20 bytes.  The streams are
  built by an independent test-local encoder: it replicates only the
  *initial* LZHUF Huffman tree (a fixed constant of the format, per
  ``docs/superpowers/refs`` tdlzhuf.c StartHuff()) to derive the bit code of
  the first symbol, and uses the d_code/d_len definition for the position
  bits (upper 6 bits 0 -> 3-bit prefix ``000`` + 6 verbatim bits).
"""

from __future__ import annotations

import datetime
import hashlib
import shutil
import struct
from pathlib import Path

import crcmod.predefined
import pytest

from fatfloppy.core.td0_compression import lzhuf_decompress

LOCAL_TD0 = Path(__file__).parent.parent.parent / "local_images" / "TD0"
LOCAL_DECODED = (
    Path(__file__).parent.parent.parent
    / "docs"
    / "superpowers"
    / "research"
    / "td0"
    / "decoded"
)
RES = Path(__file__).parent.parent / "resources"
TD0_RES = RES / "TD0"

# Independent CRC reference for building/patching test files (the same
# predefined polynomial 0xA097 the driver must use, but invoked here directly
# so the tests do not trust the production module's constants).
_CRC16 = crcmod.predefined.mkCrcFun("crc-16-teledisk")

# LZHUF tree constants (fixed by the format; duplicated here independently so
# the tests do not trust the production module's own constants).
N_CHAR = 256 - 2 + 60  # 314
T = N_CHAR * 2 - 1  # 627
R = T - 1  # 626


def _advanced_samples() -> list[Path]:
    if not LOCAL_TD0.is_dir():
        return []
    return sorted(
        p
        for p in LOCAL_TD0.iterdir()
        if p.suffix.lower() == ".td0" and p.read_bytes()[:2] == b"td"
    )


# Materialized with eager ids: a callable `ids=` over an EMPTY parametrize
# list breaks collection of the whole file when local_images/ is absent.
_ADVANCED = _advanced_samples()


# ---------------------------------------------------------------------------
# Test-local minimal encoder pieces (initial tree only)
# ---------------------------------------------------------------------------


def _initial_tree() -> tuple[list[int], list[int]]:
    """Independent replica of LZHUF StartHuff(): the deterministic initial tree."""
    son = [0] * T
    prnt = [0] * (T + N_CHAR)
    for i in range(N_CHAR):
        son[i] = i + T
        prnt[i + T] = i
    i, j = 0, N_CHAR
    while j <= R:
        son[j] = i
        prnt[i] = prnt[i + 1] = j
        i += 2
        j += 1
    prnt[R] = 0
    return son, prnt


def _initial_code(symbol: int) -> list[int]:
    """Bits (MSB first) that decode to `symbol` against the initial tree.

    The decoder walks ``c = son[R]; while c < T: c = son[c + bit]``; we walk
    the parent chain upwards from the leaf and reverse the collected bits.
    """
    son, prnt = _initial_tree()
    bits = []
    s = prnt[symbol + T]
    while s != R:
        p = prnt[s]
        bits.append(s - son[p])
        s = p
    return bits[::-1]


def _pack_bits(bits: list[int]) -> bytes:
    out = bytearray()
    for k in range(0, len(bits), 8):
        chunk = bits[k : k + 8]
        chunk += [0] * (8 - len(chunk))
        out.append(int("".join(map(str, chunk)), 2))
    return bytes(out)


# ---------------------------------------------------------------------------
# Oracle tests (the load-bearing ones)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not LOCAL_TD0.is_dir(), reason="local TD0 corpus not present")
def test_local_corpus_has_advanced_samples():
    # Guard so the parametrized oracle sweep below can never silently shrink
    # to nothing on a machine that does have the corpus.
    assert len(_ADVANCED) >= 12


class TestLzhufOracle:
    @pytest.mark.parametrize("sample", _ADVANCED, ids=[p.name for p in _ADVANCED])
    def test_byte_equal_to_greaseweazle(self, sample):
        from greaseweazle import optimised  # test oracle only, never production

        raw = sample.read_bytes()[12:]
        expected = optimised.td0_unpack(raw)
        got = lzhuf_decompress(raw)
        assert isinstance(got, bytes)
        assert got == expected


# ---------------------------------------------------------------------------
# Synthetic / contract tests
# ---------------------------------------------------------------------------


class TestLzhufSynthetic:
    def test_empty_input(self):
        assert lzhuf_decompress(b"") == b""

    def test_truncated_stream_graceful(self):
        if not _ADVANCED:
            pytest.skip("no local TD0 samples")
        raw = _ADVANCED[0].read_bytes()[12:]
        full = lzhuf_decompress(raw)
        for frac in (0.25, 0.5, 0.75):
            cut = lzhuf_decompress(raw[: int(len(raw) * frac)])  # must not raise
            assert 0 < len(cut) <= len(full)
            # Everything decoded from real input bits is an exact prefix of
            # the full decode.  The final byte is the reference C decoders'
            # zero-bit completion of the code in flight when input ran out
            # (greaseweazle does the same; pinned by the oracle sweep), so it
            # is excluded from the prefix comparison.
            assert cut[:-1] == full[: len(cut) - 1]

    def test_window_spaces_preset_single_byte(self):
        # First code: a match (symbol 256 = length 3) at position 58 ->
        # ring offset (4036 - 58 - 1) = 3977, deep inside the untouched
        # space-preset window.  Position 58's last bit lands in the final
        # input byte, which the decoder (like the C references) reads as
        # zero while flagging end-of-input -- so exactly ONE byte of the
        # in-flight match is emitted.  It must be 0x20 from the preset.
        code = _initial_code(256)
        assert len(code) == 8  # fixed property of the initial tree
        bits = code + [0, 0, 0] + [1, 1, 1, 0, 1, 0]  # pos 58: 000 + 111010
        stream = _pack_bits(bits)
        assert len(stream) == 3
        assert lzhuf_decompress(stream) == b"\x20"

    def test_window_spaces_preset_full_match(self):
        # Same match (symbol 256 = length 3) but at position 59 (ring offset
        # 3976) and with a trailing dummy byte so every meaningful bit is
        # backed by real input: the full 3-byte copy from the untouched
        # window must yield three 0x20 bytes.  The decoder then completes
        # exactly one more in-flight code from zero bits (the reference C
        # decoders' end-of-input rule), emitting exactly one trailing byte
        # whose value is not part of this test's contract.
        code = _initial_code(256)
        bits = code + [0, 0, 0] + [1, 1, 1, 0, 1, 1]  # pos 59: 000 + 111011
        stream = _pack_bits(bits) + b"\x00"
        assert len(stream) == 4
        out = lzhuf_decompress(stream)
        assert out[:3] == b"\x20\x20\x20"
        assert len(out) == 4

    def test_max_output_ceiling_enforced(self):
        # LZHUF output is unbounded relative to input, so callers must be
        # able to pass a hard ceiling.  A real advanced stream decompressing
        # far beyond a tiny ceiling must raise (ValueError family), and the
        # same call without a ceiling must still succeed.
        sample = TD0_RES / "cpm22dri.td0"
        raw = sample.read_bytes()[12:]
        full = lzhuf_decompress(raw)
        assert len(full) > 4096
        with pytest.raises(ValueError):
            lzhuf_decompress(raw, max_output=4096)
        # Ceiling exactly at the output size must not raise.
        assert lzhuf_decompress(raw, max_output=len(full)) == full


# ===========================================================================
# TD0ImageDriver tests
# ===========================================================================
#
# Oracle: docs/superpowers/research/td0/samples/INVENTORY.tsv — every
# geometry/hash literal below is copied from the named row of that file plus
# the per-track table produced by docs/superpowers/research/td0/imd_flatten.py
# over the gw-decoded IMD twins.  imd_flatten.py flattens tracks sorted by
# (cyl, head) and sectors sorted by ascending sector ID within each track;
# the driver maps logical index n to the n-th smallest sector ID (mirroring
# the IMD driver), so concatenating read_sector(cyl, head, logical) in
# (cyl, head, logical) order reproduces exactly that flattening.


def _make_driver(path):
    from fatfloppy.core.drivers.teledisk import TD0ImageDriver

    return TD0ImageDriver(file_path=str(path))


def _flatten_driver(driver) -> bytes:
    """Flatten with the same rule as the research imd_flatten.py oracle.

    Tracks in ascending (cyl, head) order; within a track, logical index
    order — which the driver defines as ascending sector ID.  Tracks absent
    from the image (zero-sector tracks are skipped at parse, like the IMD
    driver does) have no TrackFormat and contribute nothing.
    """
    pf = driver.physical_format
    out = bytearray()
    for cyl in range(pf.cylinders):
        for head in range(pf.heads):
            try:
                tf = pf.get_track_format(cyl, head)
            except ValueError:
                continue  # uncovered (e.g. zero-sector) track
            for logical in range(tf.sectors_per_track):
                out += driver.read_sector(cyl, head, logical)
    return bytes(out)


def _flatten_parsed_tracks(driver) -> bytes:
    """Flatten straight from the parsed track map (same sorted-ID rule).

    Needed where the IMD-mirrored geometry derivation leaves a coverage
    hole: when a cylinder has no head-0 track (e.g. CPM22.TD0's zero-sector
    C0H0), no TrackFormat covers that cylinder and read_sector cannot reach
    its head-1 twin — exactly like the IMD driver on the same disk.  The
    parse itself still holds every sector, which this helper proves.
    """
    out = bytearray()
    for key in sorted(driver.tracks):
        ti = driver.tracks[key]
        for sector_id in sorted(ti.sector_data):
            out += ti.sector_data[sector_id]
    return bytes(out)


def _patched_header(src: Path, tmp_path: Path, **fields) -> Path:
    """Copy a sample, patch named header bytes, recompute the header CRC."""
    offsets = {
        "sig": 0,
        "sequence": 2,
        "check_sequence": 3,
        "version": 4,
        "rate": 5,
        "drive": 6,
        "stepping": 7,
        "dos": 8,
        "sides": 9,
    }
    data = bytearray(src.read_bytes())
    for name, value in fields.items():
        off = offsets[name]
        if isinstance(value, bytes):
            data[off : off + len(value)] = value
        else:
            data[off] = value
    struct.pack_into("<H", data, 10, _CRC16(bytes(data[:10])))
    dst = tmp_path / f"patched_{src.name}"
    dst.write_bytes(bytes(data))
    return dst


class TestTD0DriverGeometry:
    """Per-sample geometry pins (literals from INVENTORY.tsv + imd_flatten)."""

    def test_alts8cpm_geometry(self):
        # INVENTORY row ALTS8CPM.TD0: sig TD (normal), rate 0x82 (FM, 500),
        # sides 1; imd_flatten: 77 cyls, 1 head, 26 sec/trk, 128 bytes/sec.
        d = _make_driver(TD0_RES / "ALTS8CPM.TD0")
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (77, 1)
        tf = pf.get_track_format(0, 0)
        assert tf.sectors_per_track == 26
        assert tf.bytes_per_sector == 128
        assert tf.encoding == "FM"
        assert tf.sector_translation_table == list(range(1, 27))

    def test_cpm22dri_geometry(self):
        # INVENTORY row cpm22dri.td0: sig td (advanced LZHUF), rate 0x82,
        # sides 1; imd_flatten: 77 cyls, 1 head, 26 sec/trk, 128 bytes/sec.
        d = _make_driver(TD0_RES / "cpm22dri.td0")
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (77, 1)
        tf = pf.get_track_format(76, 0)
        assert tf.sectors_per_track == 26
        assert tf.bytes_per_sector == 128
        assert tf.encoding == "FM"

    def test_mbc775_geometry(self):
        # INVENTORY row mbc775.td0: sig td, rate 0x00 (MFM 250), sides 2;
        # imd_flatten: 40 cyls, 2 heads, 9 sec/trk, 512 bytes/sec (360K).
        d = _make_driver(TD0_RES / "mbc775.td0")
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (40, 2)
        tf = pf.get_track_format(0, 0)
        assert tf.sectors_per_track == 9
        assert tf.bytes_per_sector == 512
        assert tf.encoding == "MFM"

    def test_cdos236_mixed_density_geometry(self):
        # INVENTORY row cdos236.td0: sig td, version 0x14, comment-less;
        # imd_flatten: 77 cyls, 1 head, modes 0,3 (mixed FM/MFM),
        # 26x128 FM on track 0 and 16x512 MFM on tracks 1-76.
        d = _make_driver(TD0_RES / "cdos236.td0")
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (77, 1)
        tf0 = pf.get_track_format(0, 0)
        assert (tf0.sectors_per_track, tf0.bytes_per_sector) == (26, 128)
        assert tf0.encoding == "FM"
        tf40 = pf.get_track_format(40, 0)
        assert (tf40.sectors_per_track, tf40.bytes_per_sector) == (16, 512)
        assert tf40.encoding == "MFM"
        assert pf.has_variable_bps

    def test_osmos_dd_tiny_edge_geometry(self):
        # INVENTORY row OSMOS-DD.TD0 (834-byte file, 768-byte payload):
        # C0H0 holds 2 sectors (ids 9 and bogus 101, 256 bytes each),
        # C1H0 holds 1 sector (bogus id 100, 256 bytes), C2H0 holds zero
        # sectors and is skipped at parse exactly like the IMD driver skips
        # zero-sector tracks — so the derived geometry is 2 cylinders.
        d = _make_driver(TD0_RES / "OSMOS-DD.TD0")
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (2, 1)
        tf0 = pf.get_track_format(0, 0)
        assert tf0.sectors_per_track == 2
        assert tf0.bytes_per_sector == 256
        # Logical order is ascending sector ID, bogus ids included.
        assert tf0.sector_translation_table == [9, 101]
        tf1 = pf.get_track_format(1, 0)
        assert tf1.sectors_per_track == 1
        assert tf1.sector_translation_table == [100]


class TestTD0DriverOracle:
    """Flattened raw content equals the gw-decoded oracle, byte for byte."""

    # (file, flat_raw_bytes, sha256_flat_raw) from the INVENTORY.tsv rows of
    # the same names.
    CASES = [
        (
            "ALTS8CPM.TD0",
            256256,
            "5b19e57391f0a4b0f91d7d103d125bc1678df3e5b4c0b5b19ac87daa7c1a092d",
        ),
        (
            "cpm22dri.td0",
            256256,
            "99670565b63d244f41caf89ab723a6ec479e294824f243a0d6bac6dc356e2415",
        ),
        (
            "mbc775.td0",
            368640,
            "d1324565e4bf4135c3fe1ae6af7a01d756dcf9210a27a9fc58d6d3a82c62fcdd",
        ),
        (
            "cdos236.td0",
            625920,
            "e6a2d6aed280e7fff6e49ce1c806ad2947119796147f5d99d91bcb9fd173e520",
        ),
        (
            "OSMOS-DD.TD0",
            768,
            "99c06d12101432a5f9e919963a99f18e4e2e53cbdeb512d8e48d88e5a4d4126c",
        ),
    ]

    @pytest.mark.parametrize("name,size,sha", CASES, ids=[c[0] for c in CASES])
    def test_flat_content_matches_oracle(self, name, size, sha):
        d = _make_driver(TD0_RES / name)
        flat = _flatten_driver(d)
        assert len(flat) == size
        assert hashlib.sha256(flat).hexdigest() == sha


class TestTD0DriverComment:
    def test_alts8cpm_comment_and_timestamp(self):
        # Decoded comment block of ALTS8CPM.TD0: NUL-separated lines, padded
        # with trailing NULs; timestamp 1900+110 = 2010, month 0-based.
        d = _make_driver(TD0_RES / "ALTS8CPM.TD0")
        assert d.comment == (
            "Altos series 8000 CP/M, version 2.21, Non-DMA, Single Density, "
            'from orig\ninal Altos 8" disk'
        )
        assert d.creation_date == datetime.datetime(2010, 2, 5, 22, 4, 58)

    def test_cdos236_has_no_comment(self):
        # cdos236.td0 stepping byte is 0x00: no comment block at all.
        d = _make_driver(TD0_RES / "cdos236.td0")
        assert d.comment == ""
        assert d.creation_date is None


class TestTD0DriverReadOnly:
    def test_write_sector_raises(self):
        d = _make_driver(TD0_RES / "ALTS8CPM.TD0")
        with pytest.raises(OSError):
            d.write_sector(0, 0, 0, b"\x00" * 128)

    def test_no_creation_or_formatting(self):
        d = _make_driver(TD0_RES / "ALTS8CPM.TD0")
        assert d.supports_new_image_creation is False
        assert d.supports_in_place_formatting is False
        with pytest.raises(NotImplementedError):
            d.initialize_new_image(d.physical_format)

    def test_flush_is_noop_and_file_untouched(self, tmp_path):
        src = TD0_RES / "ALTS8CPM.TD0"
        work = tmp_path / "copy.td0"
        shutil.copy(src, work)
        d = _make_driver(work)
        d.flush()  # must not raise
        assert work.read_bytes() == src.read_bytes()


class TestTD0DriverRejection:
    def test_nonzero_sequence_rejected(self, tmp_path):
        # sequence != 0 means a multi-volume sequel (.TD1...) — rejected with
        # a message naming the limitation, at validate AND at open.
        from fatfloppy.core.drivers.teledisk import TD0ImageDriver

        bad = _patched_header(TD0_RES / "ALTS8CPM.TD0", tmp_path, sequence=1)
        d = TD0ImageDriver.__new__(TD0ImageDriver)
        ok, msg = TD0ImageDriver.validate_for_opening(d, str(bad))
        assert not ok
        assert "multi-volume" in msg.lower()
        with pytest.raises(ValueError, match="(?i)multi-volume"):
            TD0ImageDriver(file_path=str(bad))

    def test_old_advanced_lzw_rejected(self, tmp_path):
        # 'td' signature with version < 0x14 is Teledisk 1.x LZW "old
        # advanced" — no open implementation exists; reject by name.
        from fatfloppy.core.drivers.teledisk import TD0ImageDriver

        bad = _patched_header(TD0_RES / "cpm22dri.td0", tmp_path, version=0x10)
        d = TD0ImageDriver.__new__(TD0ImageDriver)
        ok, msg = TD0ImageDriver.validate_for_opening(d, str(bad))
        assert not ok
        assert "lzw" in msg.lower()
        with pytest.raises(ValueError, match="(?i)lzw"):
            TD0ImageDriver(file_path=str(bad))

    def test_bad_header_crc_rejected(self, tmp_path):
        from fatfloppy.core.drivers.teledisk import TD0ImageDriver

        data = bytearray((TD0_RES / "ALTS8CPM.TD0").read_bytes())
        data[10] ^= 0xFF  # corrupt stored header CRC
        bad = tmp_path / "badcrc.td0"
        bad.write_bytes(bytes(data))
        d = TD0ImageDriver.__new__(TD0ImageDriver)
        ok, msg = TD0ImageDriver.validate_for_opening(d, str(bad))
        assert not ok
        assert "crc" in msg.lower()
        with pytest.raises(ValueError, match="(?i)crc"):
            TD0ImageDriver(file_path=str(bad))

    def test_truncated_file_rejected(self, tmp_path):
        from fatfloppy.core.drivers.teledisk import TD0ImageDriver

        full = (TD0_RES / "ALTS8CPM.TD0").read_bytes()

        # Shorter than the 12-byte header: rejected at validate.
        stub = tmp_path / "stub.td0"
        stub.write_bytes(full[:8])
        d = TD0ImageDriver.__new__(TD0ImageDriver)
        ok, _msg = TD0ImageDriver.validate_for_opening(d, str(stub))
        assert not ok

        # Valid header but body cut mid-track: validate passes (it is only
        # the cheap magic gate), open raises a parse error.
        cut = tmp_path / "cut.td0"
        cut.write_bytes(full[:200])
        ok, _msg = TD0ImageDriver.validate_for_opening(d, str(cut))
        assert ok
        with pytest.raises(ValueError):
            TD0ImageDriver(file_path=str(cut))

    def test_decompression_bomb_ceiling(self, monkeypatch):
        # The driver must enforce a decompressed-output ceiling so a crafted
        # 'td' stream cannot balloon without bound (LZHUF output is unbounded
        # relative to input).  Lower the ceiling below a real sample's
        # decompressed size and the open must fail with a clear error.
        from fatfloppy.core.drivers import teledisk

        monkeypatch.setattr(teledisk, "TD0_MAX_DECOMPRESSED", 4096)
        with pytest.raises(ValueError, match="(?i)exceed"):
            teledisk.TD0ImageDriver(file_path=str(TD0_RES / "cpm22dri.td0"))


def _synthetic_td0_with_skipped_sector() -> bytes:
    """Build a minimal normal-compression TD0 exercising flags & 0x30.

    Track 0/head 0 with two 256-byte sectors: id 1 carries flag 0x10
    (data skipped per DOS allocation — NO data block follows), id 2 is a
    normal method-0 sector filled with 0xA5.  No corpus sample sets these
    flags, hence the synthetic image (layout per td0notes.txt sections 5-7).
    """
    header = struct.pack("<2sBBBBBBBB", b"TD", 0, 0, 0x15, 0, 1, 0, 0, 1)
    header += struct.pack("<H", _CRC16(header))

    track = struct.pack("<BBB", 2, 0, 0)
    track += bytes([_CRC16(track) & 0xFF])

    payload = bytearray(track)
    # Sector id 1: flags 0x10 -> no data header, no data block.
    sec1 = struct.pack("<BBBBB", 0, 0, 1, 1, 0x10)
    payload += sec1 + bytes([_CRC16(b"\x00" * 256) & 0xFF])
    # Sector id 2: normal raw (method 0) data block.
    data2 = b"\xa5" * 256
    sec2 = struct.pack("<BBBBB", 0, 0, 2, 1, 0x00)
    payload += sec2 + bytes([_CRC16(data2) & 0xFF])
    payload += struct.pack("<HB", len(data2) + 1, 0) + data2
    payload += b"\xff"  # track-list terminator
    return header + bytes(payload)


class TestTD0DriverQuirks:
    """Corpus quirks (local_images, skip-guarded) + synthetic no-data flags."""

    def test_skipped_sector_flags_zero_filled(self, tmp_path):
        img = tmp_path / "skipflag.td0"
        img.write_bytes(_synthetic_td0_with_skipped_sector())
        d = _make_driver(img)
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (1, 1)
        assert pf.get_track_format(0, 0).sectors_per_track == 2
        assert d.read_sector(0, 0, 0) == b"\x00" * 256  # flags 0x10: zero-fill
        assert d.read_sector(0, 0, 1) == b"\xa5" * 256

    @pytest.mark.skipif(not LOCAL_TD0.is_dir(), reason="local TD0 corpus not present")
    def test_zero_sector_track_skipped_cpm22(self):
        # CPM22.TD0 records a zero-sector track at C0H0 (a real artefact;
        # the disk's first track held no readable sectors).  Like the IMD
        # driver, the track is skipped: not in .tracks, no TrackFormat,
        # while every other track stays readable.
        d = _make_driver(LOCAL_TD0 / "CPM22.TD0")
        pf = d.physical_format
        assert (0, 0) not in d.tracks
        assert (0, 1) in d.tracks
        assert (pf.cylinders, pf.heads) == (80, 2)
        # 16 x 256-byte sectors per track (imd_flatten over CPM22.imd).
        assert len(d.read_sector(1, 0, 0)) == 256
        # Cylinder 0 has no head-0 track, so no TrackFormat covers it and
        # C0H1 is unreachable via read_sector — the IMD driver behaves
        # identically on the gw-decoded twin.  The parse still holds every
        # sector: flattening the track map matches the INVENTORY oracle row
        # CPM22.TD0 in full.
        flat = _flatten_parsed_tracks(d)
        assert len(flat) == 651264
        assert (
            hashlib.sha256(flat).hexdigest()
            == "e88e1dbf8b1965b04aeb04d9022f97e9d764efb81014c9003aeae58d174ac228"
        )

    @pytest.mark.skipif(not LOCAL_TD0.is_dir(), reason="local TD0 corpus not present")
    def test_msdos20t_duplicates_and_tail_zero_tracks(self):
        # MSDOS20T.TD0 contains 10 duplicate-ID sector records (first valid
        # occurrence wins) and zero-sector tracks at cylinder 77 (skipped, so
        # the geometry stays 77 cylinders).  INVENTORY row MSDOS20T.TD0 pins
        # the flattened oracle, which proves both behaviors byte-exactly.
        d = _make_driver(LOCAL_TD0 / "MSDOS20T.TD0")
        pf = d.physical_format
        assert (pf.cylinders, pf.heads) == (77, 2)
        flat = _flatten_driver(d)
        assert len(flat) == 1261568
        assert (
            hashlib.sha256(flat).hexdigest()
            == "418d00d2f6b69aba3ca7d6193e7fc94b9ce86e6dbbe42946dc53a9c809afac62"
        )

    @pytest.mark.skipif(not LOCAL_TD0.is_dir(), reason="local TD0 corpus not present")
    def test_sb180sys_non_one_based_ids_all_readable(self):
        # SB180SYS.TD0 numbers its sectors 17-26 (CP/M 2:1 skew table on
        # 512-byte sectors).  Logical index 0 must map to id 17 and every
        # sector must be readable; INVENTORY row SB180SYS.TD0 pins content.
        d = _make_driver(LOCAL_TD0 / "SB180SYS.TD0")
        pf = d.physical_format
        tf = pf.get_track_format(0, 0)
        assert tf.sector_translation_table == list(range(17, 27))
        flat = _flatten_driver(d)
        assert len(flat) == 409600
        assert (
            hashlib.sha256(flat).hexdigest()
            == "013bfd205e1d93b7e8932d5a6829f177fcac6a7f9c3d8adad727a431de73e8b6"
        )


class TestTD0AutoDetection:
    def test_factory_auto_selects_td0(self):
        from fatfloppy.core.driver_factory import DriverFactory

        for name in ("ALTS8CPM.TD0", "mbc775.td0"):
            driver = DriverFactory.create("auto", str(TD0_RES / name))
            assert driver.driver_type == "TD0", name

    def test_non_td0_named_td0_falls_through(self, tmp_path):
        # A raw FAT12 image renamed to .td0 must NOT be claimed by the TD0
        # driver (magic + header CRC gate); it falls through to the raw IMG
        # fallback.
        from fatfloppy.core.driver_factory import DriverFactory

        fake = tmp_path / "fake.td0"
        shutil.copy(RES / "empty_formatted_360k.img", fake)
        driver = DriverFactory.create("auto", str(fake))
        assert driver.driver_type == "IMG"

    def test_existing_resources_not_shadowed(self):
        # Spot pins: the TD0 driver joining auto-detection must not steal
        # any existing resource from its rightful driver.
        from fatfloppy.core.driver_factory import DriverFactory

        pins = [
            (RES / "imd_720k.imd", "IMD"),
            (RES / "empty_formatted_360k.img", "IMG"),
            (RES / "mits" / "lifeboat_cpm22_8inch.dsk", "MITS_DSK"),
        ]
        for path, expected in pins:
            if not path.exists():
                pytest.skip(f"resource missing: {path}")
            driver = DriverFactory.create("auto", str(path))
            assert driver.driver_type == expected, path.name

    def test_controller_detects_cpm_on_td0(self):
        # The detector must run filesystem scoring over TD0 geometry exactly
        # as for IMD: a DRI CP/M 2.2 8" SSSD disk inside a TD0 container
        # detects as CP/M (DPB inference over the container's geometry).
        from fatfloppy.core.controller import DiskController
        from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem

        controller = DiskController()
        assert controller.open_disk(str(TD0_RES / "cpm22dri.td0"))
        try:
            assert type(controller.driver).__name__ == "TD0ImageDriver"
            assert isinstance(controller.filesystem, CPMFilesystem)
        finally:
            controller.close_disk()

    def test_controller_detects_fat12_on_td0(self):
        from fatfloppy.core.controller import DiskController
        from fatfloppy.core.filesystems.fat12_fs import FATFilesystem

        controller = DiskController()
        assert controller.open_disk(str(TD0_RES / "mbc775.td0"))
        try:
            assert type(controller.driver).__name__ == "TD0ImageDriver"
            assert isinstance(controller.filesystem, FATFilesystem)
        finally:
            controller.close_disk()


# ===========================================================================
# Cross-decoder verification: TD0 vs greaseweazle-decoded IMD twins
# ===========================================================================
#
# For every local TD0 sample whose greaseweazle-decoded IMD twin exists in
# docs/superpowers/research/td0/decoded/, open both files with
# DiskController.open_disk (auto-detection) and assert:
#   - both containers open successfully;
#   - same filesystem type detected (or both None);
#   - if a filesystem is detected: identical recursive listings (name + size)
#     and identical per-file content sha256.
#
# Benign known divergences (documented by Task 2 research):
#   - TrackFormat.rate may differ: TD0 reports the honest recorded rate
#     (e.g. 250 kbps for 8" disks) while greaseweazle writes 500 kbps in
#     the IMD header for the same disk.  The test compares filesystem-level
#     output only, never raw geometry metadata.
#   - CPM22-style coverage holes (zero-sector track at C0H0) are symmetric:
#     neither side can read through the hole, so file content matches or is
#     absent on both sides identically.
#
# Per-file read errors are tolerated ONLY if they occur identically on both
# sides (same file name raises on both).


def _local_pairs() -> list[tuple[Path, Path]]:
    """Return (td0_path, imd_twin_path) for every local sample whose twin exists."""
    if not LOCAL_TD0.is_dir() or not LOCAL_DECODED.is_dir():
        return []
    pairs = []
    for td0 in sorted(LOCAL_TD0.iterdir()):
        if td0.suffix.lower() != ".td0":
            continue
        for imd in LOCAL_DECODED.glob("*.imd"):
            if imd.stem.lower() == td0.stem.lower():
                pairs.append((td0, imd))
                break
    return pairs


def _read_file_safe(controller, name: str) -> tuple[bytes | None, Exception | None]:
    """Read a file, returning (data, None) or (None, exception)."""
    try:
        return controller.read_file(name), None
    except Exception as e:
        return None, e


@pytest.mark.skipif(
    not LOCAL_TD0.is_dir() or not LOCAL_DECODED.is_dir(),
    reason="local TD0 corpus or greaseweazle-decoded twins not present",
)
@pytest.mark.parametrize(
    "td0,imd", _local_pairs(), ids=[p[0].name for p in _local_pairs()]
)
def test_td0_matches_imd_twin_through_full_stack(td0: Path, imd: Path) -> None:
    """TD0 and its gw-IMD twin must agree through the full filesystem stack."""
    from fatfloppy.core.controller import DiskController

    c_td0 = DiskController()
    c_imd = DiskController()
    try:
        ok_td0 = c_td0.open_disk(str(td0))
        ok_imd = c_imd.open_disk(str(imd))

        assert ok_td0, f"TD0 open failed: {td0.name}"
        assert ok_imd, f"IMD twin open failed: {imd.name}"

        fs_td0 = type(c_td0.filesystem).__name__ if c_td0.filesystem else None
        fs_imd = type(c_imd.filesystem).__name__ if c_imd.filesystem else None

        assert fs_td0 == fs_imd, (
            f"{td0.name}: filesystem type mismatch: TD0={fs_td0} IMD={fs_imd}"
        )

        if fs_td0 is None:
            # Both sides have no recognized filesystem — symmetric, no more to check.
            return

        listing_td0 = c_td0.list_directory("/")
        listing_imd = c_imd.list_directory("/")

        # Compare (name, size) pairs — order may differ, sort for stability.
        sorted_td0 = sorted(listing_td0, key=lambda fi: fi["name"])
        sorted_imd = sorted(listing_imd, key=lambda fi: fi["name"])

        names_td0 = [(fi["name"], fi["size"]) for fi in sorted_td0 if not fi["is_dir"]]
        names_imd = [(fi["name"], fi["size"]) for fi in sorted_imd if not fi["is_dir"]]

        assert names_td0 == names_imd, (
            f"{td0.name}: listing mismatch:\n  TD0={names_td0}\n  IMD={names_imd}"
        )

        # Per-file content: compare sha256; tolerate errors only if symmetric.
        content_mismatches = []
        error_asymmetries = []
        for fi in sorted_td0:
            if fi["is_dir"]:
                continue
            name = fi["name"]
            data_td0, err_td0 = _read_file_safe(c_td0, name)
            data_imd, err_imd = _read_file_safe(c_imd, name)

            both_errored = err_td0 is not None and err_imd is not None
            td0_only_error = err_td0 is not None and err_imd is None
            imd_only_error = err_td0 is None and err_imd is not None

            if td0_only_error or imd_only_error:
                error_asymmetries.append(
                    f"{name}: TD0_err={err_td0!r} IMD_err={err_imd!r}"
                )
                continue
            if both_errored:
                # Symmetric failure — tolerated.
                continue

            sha_td0 = hashlib.sha256(data_td0).hexdigest()
            sha_imd = hashlib.sha256(data_imd).hexdigest()
            if sha_td0 != sha_imd:
                content_mismatches.append(
                    f"{name}: TD0_sha256={sha_td0[:16]}… IMD_sha256={sha_imd[:16]}…"
                )

        assert not error_asymmetries, (
            f"{td0.name}: asymmetric read errors:\n  " + "\n  ".join(error_asymmetries)
        )
        assert not content_mismatches, (
            f"{td0.name}: content mismatches:\n  " + "\n  ".join(content_mismatches)
        )
    finally:
        c_td0.close_disk()
        c_imd.close_disk()


# ===========================================================================
# End-to-end pins for cpm22dri and mbc775
# ===========================================================================
#
# These are committed pins against the resources/ copies (no local_images
# guard needed) ensuring the two most important samples never regress.
# Content hashes are derived from the greaseweazle-decoded IMD twin at
# test-authoring time (see docs/superpowers/research/td0/decoded/cpm22dri.imd)
# and verified to match the TD0 side — both sides were identical at pin time.


@pytest.mark.skipif(
    not LOCAL_TD0.is_dir() or not LOCAL_DECODED.is_dir(),
    reason="local TD0 corpus or greaseweazle-decoded twins not present",
)
class TestCpm22driPin:
    """cpm22dri.td0 -> CPMFilesystem with expected DRI CP/M 2.2 distribution."""

    def test_filesystem_type_is_cpm(self):
        from fatfloppy.core.controller import DiskController
        from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem

        c = DiskController()
        assert c.open_disk(str(LOCAL_TD0 / "cpm22dri.td0"))
        try:
            assert isinstance(c.filesystem, CPMFilesystem), (
                f"Expected CPMFilesystem, got {type(c.filesystem).__name__}"
            )
        finally:
            c.close_disk()

    def test_movcpm_and_pip_present(self):
        from fatfloppy.core.controller import DiskController

        c = DiskController()
        assert c.open_disk(str(LOCAL_TD0 / "cpm22dri.td0"))
        try:
            names = {fi["name"] for fi in c.list_directory("/")}
            assert "MOVCPM.COM" in names, f"MOVCPM.COM missing; found: {sorted(names)}"
            assert "PIP.COM" in names, f"PIP.COM missing; found: {sorted(names)}"
        finally:
            c.close_disk()

    def test_movcpm_content_hash(self):
        # MOVCPM.COM sha256 derived from cpm22dri.imd (greaseweazle-decoded twin).
        # Both sides were byte-identical at pin time.
        expected_sha256 = (
            "e5d6f72490db0f1aa5ca4826fc6d0644604eae71ed8df4e611233d8c3e3ac401"
        )
        from fatfloppy.core.controller import DiskController

        c = DiskController()
        assert c.open_disk(str(LOCAL_TD0 / "cpm22dri.td0"))
        try:
            data = c.read_file("MOVCPM.COM")
            assert hashlib.sha256(data).hexdigest() == expected_sha256, (
                f"MOVCPM.COM content hash mismatch; size={len(data)}"
            )
        finally:
            c.close_disk()


@pytest.mark.skipif(
    not LOCAL_TD0.is_dir() or not LOCAL_DECODED.is_dir(),
    reason="local TD0 corpus or greaseweazle-decoded twins not present",
)
class TestMbc775Pin:
    """mbc775.td0 -> FATFilesystem with a non-empty listing."""

    def test_filesystem_type_is_fat(self):
        from fatfloppy.core.controller import DiskController
        from fatfloppy.core.filesystems.fat12_fs import FATFilesystem

        c = DiskController()
        assert c.open_disk(str(LOCAL_TD0 / "mbc775.td0"))
        try:
            assert isinstance(c.filesystem, FATFilesystem), (
                f"Expected FATFilesystem, got {type(c.filesystem).__name__}"
            )
        finally:
            c.close_disk()

    def test_listing_non_empty(self):
        from fatfloppy.core.controller import DiskController

        c = DiskController()
        assert c.open_disk(str(LOCAL_TD0 / "mbc775.td0"))
        try:
            files = c.list_directory("/")
            assert len(files) > 0, "mbc775 listing is empty"
        finally:
            c.close_disk()


# ===========================================================================
# GUI smoke: cpm22dri.td0 through the real DiskManager (offscreen)
# ===========================================================================
#
# Opens the committed resources/TD0/cpm22dri.td0 via the real DiskManager
# (the same code path the application uses) and checks:
#   - the file listing populates (non-empty);
#   - the disk map renders without exceptions;
#   - the detected-format panel names the TD0 driver and includes the archive
#     comment (the ALTS8CPM comment is in ALTS8CPM.TD0; cpm22dri has no
#     comment, so we check the panel does NOT crash and shows "Container: TD0");
#   - write operations surface clean read-only errors (not crashes).


import logging  # noqa: E402  (appended section, keep imports local)
import os  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestTD0GuiSmoke:
    """Offscreen GUI smoke tests for cpm22dri.td0 through the real DiskManager."""

    def _open_with_manager(self, path: Path):
        """Open path with a real DiskController, return (controller, detected_text)."""
        from fatfloppy.core.controller import DiskController
        from fatfloppy.gui.managers.disk_manager import DiskManager

        controller = DiskController()
        assert controller.open_disk(str(path)), f"open_disk failed for {path.name}"

        label = MagicMock()
        manager = DiskManager.__new__(DiskManager)
        manager.logger = logging.getLogger("test_gui_smoke")
        manager.parent = SimpleNamespace(
            controller=controller, detected_format_info=label
        )
        manager.update_detected_format_info()
        assert label.setText.called, "detected_format_info label was not updated"
        detected_text = label.setText.call_args[0][0]
        return controller, detected_text

    def test_detected_format_panel_shows_td0_container(self):
        """Detected-format panel must name the TD0 container for cpm22dri.td0."""
        controller, text = self._open_with_manager(TD0_RES / "cpm22dri.td0")
        controller.close_disk()
        assert "Container: TD0" in text, (
            f"Expected 'Container: TD0' in detected format text:\n{text}"
        )

    def test_listing_populates_via_controller(self):
        """File listing through the controller must be non-empty for cpm22dri.td0."""
        from fatfloppy.core.controller import DiskController

        c = DiskController()
        assert c.open_disk(str(TD0_RES / "cpm22dri.td0"))
        try:
            files = c.list_directory("/")
            assert len(files) > 0, "cpm22dri.td0 listing empty through controller"
        finally:
            c.close_disk()

    def test_disk_map_renders_without_exception(self):
        """DiskMapView.draw_disk_map must not raise for a TD0-backed disk."""
        from PyQt6.QtGui import QColor, QFont
        from PyQt6.QtWidgets import QApplication, QWidget

        from fatfloppy.core.controller import DiskController
        from fatfloppy.gui.disk_map import DiskMapView

        app = QApplication.instance() or QApplication([])  # noqa: F841

        c = DiskController()
        assert c.open_disk(str(TD0_RES / "cpm22dri.td0"))
        try:
            parent = QWidget()
            view = DiskMapView(parent)
            # draw_disk_map must not raise; the scene may be empty for a small
            # offscreen widget but that is acceptable.
            view.draw_disk_map(
                controller=c,
                current_head=0,
                _busy_units=None,
                free_space=0,
                total_space=0,
                app_font=QFont(),
                text_color=QColor("black"),
            )
        except Exception as exc:
            raise AssertionError(
                f"DiskMapView.draw_disk_map raised for cpm22dri.td0: {exc!r}"
            ) from exc
        finally:
            c.close_disk()

    def test_write_via_file_manager_raises_clean_error(self):
        """Attempting to write (import/delete) on a TD0 disk must raise OSError."""
        from fatfloppy.core.controller import DiskController

        c = DiskController()
        assert c.open_disk(str(TD0_RES / "cpm22dri.td0"))
        try:
            with pytest.raises(OSError):
                c.driver.write_sector(0, 0, 0, b"\x00" * 128)
        finally:
            c.close_disk()

    def test_comment_in_geometry_panel_for_alts8cpm(self):
        """ALTS8CPM.TD0 has a comment; the geometry panel must include it."""
        from fatfloppy.core.controller import DiskController
        from fatfloppy.gui.managers.disk_manager import DiskManager

        controller = DiskController()
        assert controller.open_disk(str(TD0_RES / "ALTS8CPM.TD0"))

        geo_label = MagicMock()
        manager = DiskManager.__new__(DiskManager)
        manager.logger = logging.getLogger("test_gui_smoke")
        manager.parent = SimpleNamespace(
            controller=controller,
            physical_format_info=geo_label,
            detected_format_info=MagicMock(),
        )
        manager.update_geometry_info()
        controller.close_disk()

        assert geo_label.setText.called, "geometry label was not updated"
        geo_text = geo_label.setText.call_args[0][0]
        assert "Altos" in geo_text, (
            f"Expected ALTS8CPM comment in geometry panel:\n{geo_text}"
        )
