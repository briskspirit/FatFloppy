"""RT-11 core layout tests: RAD50, date words, interleave views, directories.

Committed resources (tests/resources/RT11/, real DEC public-domain-era media):

- ``AS-5777C-BC_RT11_V03B_1-9.RX01`` -- RT-11 V03B system disk 1/9 (Apr 1979),
  raw RX01 physical sector order, 77x26x128 = 256,256 bytes.
  sha256 bbbc11c527cd7d5fc25cf27a3047ee96cfc9627d75a6bfff81d55ee17dc4c38f.
  First directory entry: SWAP.SYS, 24 blocks, 27-Mar-1979 (per the disk's
  DIR listing; see docs/superpowers/research/rt11/oracles/listings/).
- ``BA-P732B-BC.DSK`` / ``BA-P732B-BC.IMG`` -- RT-11 V5.01 AUTO distribution
  disk, the SAME physical floppy archived in BOTH conventions: .DSK is raw
  RX02 physical sector order, .IMG is logical block order. Both 512,512
  bytes, so the size cannot disambiguate them -- this pair is the
  view-resolver acid test. The archive's genuine DEC DIR listing
  (BA-P732B-BC.TXT) starts: "SWAP  .SYS    26P 01-Feb-84".
  sha256 .DSK 8cd1409199f02a2ec116fe4d6d607bead87902d6269b11375a9f98bd27882a14,
  sha256 .IMG df3a22aaab879d63a076b466344f7fee37199a697a021a77d6106c0bec661799.
- ``BASIC-11_V2.1_RX02.img`` -- BASIC-11 V2.1 distribution, raw RX02 physical
  sector order, 512,512 bytes. First entry BSOT0D.EAE, 12 blocks, 04-Apr-1983.
  sha256 10461781d61f2aff4f7b22b9e14071a2590ea4be757007352080d9b4b415950a.

The view-mapper expectation tables are derived INDEPENDENTLY in this file by
transcribing the DEC driver formulas quoted in PUTR.ASM (see
docs/superpowers/research/rt11/notes/putr_interleave_excerpts.txt), not by
importing the production module's mapping.
"""

import json
import struct
from pathlib import Path

import pytest

from fatfloppy.core.rt11_layout import (
    DEFAULT_DIR_START,
    E_EOS,
    E_MPTY,
    E_PERM,
    E_PROT,
    E_READ,
    RAD50_MAX_WORD,
    SCORE_THRESHOLD,
    VIEW_GEOMETRY,
    VIEWS,
    RT11Config,
    decode_date_word,
    encode_date_word,
    logical_block_to_chs,
    parse_home_block,
    parse_segment,
    rad50_decode,
    rad50_encode,
    rad50_is_valid,
    score_directory_structure,
)

RES = Path(__file__).parent.parent / "resources" / "RT11"
RX01_V03B = RES / "AS-5777C-BC_RT11_V03B_1-9.RX01"
V0501_DSK = RES / "BA-P732B-BC.DSK"  # physical RX02 sector order
V0501_IMG = RES / "BA-P732B-BC.IMG"  # logical block order, same disk
BASIC11_RX02 = RES / "BASIC-11_V2.1_RX02.img"

LOCAL_RT11 = Path(__file__).parent.parent.parent / "local_images" / "RT11"
CORPUS_INVENTORY = (
    Path(__file__).parent.parent.parent
    / "docs"
    / "superpowers"
    / "research"
    / "rt11"
    / "oracles"
    / "corpus_inventory.json"
)

BLOCK = 512


# ---------------------------------------------------------------------------
# Independent reference interleave functions, transcribed from the verbatim
# DEC driver excerpts in putr_interleave_excerpts.txt. These return DEC
# 1-based (track, sector); the production module returns 0-based
# (cylinder, head, sector), so tests convert before comparing.
# ---------------------------------------------------------------------------


def _ref_rx_interleave(lsn, nsect):
    """RX01/RX02 mapping, from RT-11 V04 DY.MAC (quoted in PUTR.ASM):

        ISEC=(ISEC-1)*2
        IF(ISEC.GE.26) ISEC=ISEC-25
        ISEC=MOD(ISEC+ITRK*6,26)+1
        ITRK=ITRK+1

    ``lsn`` is the sequential 0-based sector index of the logical block
    stream, so ``divmod`` directly yields 0-based ITRK and (ISEC-1)*1.
    2:1 interleave, 6-sector track-to-track skew, track offset 1
    (physical track 0 is not used by the filesystem).
    """
    itrk, isec = divmod(lsn, nsect)
    isec *= 2  # ISEC=(ISEC-1)*2 on the 0-based value
    if isec >= nsect:
        isec -= nsect - 1  # IF(ISEC.GE.26) ISEC=ISEC-25
    isec = (isec + itrk * 6) % nsect + 1  # ISEC=MOD(ISEC+ITRK*6,26)+1
    itrk += 1  # ITRK=ITRK+1
    return itrk, isec


def _ref_rx50_interleave(lsn):
    """RX50 mapping, from P/OS V3.2 DZDRV.MAC (quoted in PUTR.ASM):

        ISEC=(ISEC-1)*2
        IF(ISEC.GE.10) ISEC=ISEC-9
        ISEC=MOD(ISEC+ITRK*2,10)+1
        ITRK=MOD(ITRK+1,80)

    2:1 interleave, 2-sector track-to-track skew, track offset 1 WITH
    wraparound: physical track 0 is the LAST logical track, all 80 tracks
    are used.
    """
    itrk, isec = divmod(lsn, 10)
    isec *= 2
    if isec >= 10:
        isec -= 9
    isec = (isec + itrk * 2) % 10 + 1
    itrk = (itrk + 1) % 80
    return itrk, isec


_REF_VIEW = {
    # view -> (sectors_per_block, mapper)
    "rx01": (4, lambda lsn: _ref_rx_interleave(lsn, 26)),
    "rx02": (2, lambda lsn: _ref_rx_interleave(lsn, 26)),
    "rx50": (1, _ref_rx50_interleave),
}


def _ref_block_table(view, block):
    """(block -> [(track, 1-based sector), ...]) per the reference mapper."""
    per, mapper = _REF_VIEW[view]
    return [mapper(block * per + i) for i in range(per)]


def _read_block(data, view, block):
    """Read one 512-byte logical block from raw image bytes under a view.

    Uses the PRODUCTION mapper -- this is the function under test whenever a
    real-image literal is pinned.
    """
    if view == "logical":
        return data[block * BLOCK : (block + 1) * BLOCK]
    geom = VIEW_GEOMETRY[view]
    out = b""
    for cyl, _head, sec in logical_block_to_chs(view, block):
        pos = (cyl * geom.sectors_per_track + sec) * geom.bytes_per_sector
        out += data[pos : pos + geom.bytes_per_sector]
    return out


def _reader(data, view):
    return lambda block: _read_block(data, view, block)


# ---------------------------------------------------------------------------
# RAD50
# ---------------------------------------------------------------------------


class TestRad50:
    def test_charset_special_codes(self):
        # Charset order: space=0, A-Z=1..26, $=27, .=28, %=29, 0-9=30..39.
        assert rad50_encode("$") == 27 * 1600
        assert rad50_encode(".") == 28 * 1600
        assert rad50_encode("%") == 29 * 1600
        assert rad50_decode(27 * 1600) == "$  "
        assert rad50_decode(28 * 1600) == ".  "
        # Code 29 decodes to the true '%' character (PUTR prints '?';
        # we deliberately keep the real DEC charset character).
        assert rad50_decode(29 * 1600) == "%  "
        assert rad50_decode(0) == "   "
        assert rad50_encode("A") == 1600
        assert rad50_encode("0") == 30 * 1600
        assert rad50_decode(30 * 1600 + 31 * 40 + 39) == "019"

    def test_round_trip_rt11sj_sys(self):
        # "RT11SJ" is two RAD50 words; "SYS" is one.
        w1, w2 = rad50_encode("RT1"), rad50_encode("1SJ")
        assert rad50_decode(w1) + rad50_decode(w2) == "RT11SJ"
        sys_word = rad50_encode("SYS")
        assert sys_word == 19 * 1600 + 25 * 40 + 19  # S=19, Y=25, S=19
        assert rad50_decode(sys_word) == "SYS"

    def test_word_validity_boundary(self):
        # Max valid word: "999" = 39*1600 + 39*40 + 39 = 63999.
        assert RAD50_MAX_WORD == 63999
        assert rad50_is_valid(63999)
        assert rad50_decode(63999) == "999"
        assert not rad50_is_valid(64000)
        assert not rad50_is_valid(0xFFFF)
        assert not rad50_is_valid(-1)
        with pytest.raises(ValueError):
            rad50_decode(64000)

    def test_encode_pads_and_rejects(self):
        assert rad50_encode("AB") == rad50_encode("AB ")
        assert rad50_encode("") == 0
        with pytest.raises(ValueError):
            rad50_encode("ABCD")  # too long
        with pytest.raises(ValueError):
            rad50_encode("A*B")  # '*' not in the RAD50 charset

    def test_decode_known_words_from_real_directory(self):
        # Segment 1 of the committed V03B RX01 starts at block 6 (under the
        # rx01 view); the first 7-word entry begins at byte 10. Its DIR
        # listing pins the first file as "SWAP  .SYS   24 blocks 27-Mar-79".
        data = RX01_V03B.read_bytes()
        seg = _read_block(data, "rx01", 6)
        name1, name2 = struct.unpack_from("<2H", seg, 12)
        assert rad50_decode(name1) + rad50_decode(name2) == "SWAP  "
        type_word, length = struct.unpack_from("<2H", seg, 16)
        assert rad50_decode(type_word) == "SYS"
        assert length == 24


# ---------------------------------------------------------------------------
# Date word
# ---------------------------------------------------------------------------


class TestDateWord:
    def test_known_dates_from_dir_listings(self):
        # 27-Mar-1979 (V03B disk 1, every file) and 01-Feb-84 (BA-P732B
        # DEC DIR listing). Formula: (age<<14)|(m<<10)|(d<<5)|(y-1972-32*age).
        w_1979 = (0 << 14) | (3 << 10) | (27 << 5) | (1979 - 1972)
        assert w_1979 == 3943
        assert encode_date_word(1979, 3, 27) == w_1979
        assert decode_date_word(w_1979) == (1979, 3, 27)

        w_1984 = (0 << 14) | (2 << 10) | (1 << 5) | (1984 - 1972)
        assert w_1984 == 2092
        assert encode_date_word(1984, 2, 1) == w_1984
        assert decode_date_word(w_1984) == (1984, 2, 1)

    def test_date_words_read_off_real_disks(self):
        # V03B RX01 first entry date word == 27-Mar-1979.
        seg = _read_block(RX01_V03B.read_bytes(), "rx01", 6)
        (date_word,) = struct.unpack_from("<H", seg, 22)
        assert date_word == 3943
        # V5.01 logical IMG first entry date word == 01-Feb-84, matching the
        # genuine DEC DIR listing archived with the disk.
        seg = _read_block(V0501_IMG.read_bytes(), "logical", 6)
        (date_word,) = struct.unpack_from("<H", seg, 22)
        assert date_word == 2092
        # BASIC-11 RX02 raw: first entry BSOT0D.EAE dated 04-Apr-1983.
        seg = _read_block(BASIC11_RX02.read_bytes(), "rx02", 6)
        (date_word,) = struct.unpack_from("<H", seg, 22)
        assert date_word == (4 << 10) | (4 << 5) | (1983 - 1972)
        assert decode_date_word(date_word) == (1983, 4, 4)

    def test_zero_means_no_date(self):
        assert decode_date_word(0) is None

    def test_age_bits_for_years_2004_plus(self):
        # Age bits extend the 1972 epoch in 32-year steps: 1972/2004/2036/2068.
        w_2005 = (1 << 14) | (6 << 10) | (15 << 5) | (2005 - 1972 - 32)
        assert encode_date_word(2005, 6, 15) == w_2005
        assert decode_date_word(w_2005) == (2005, 6, 15)
        w_2070 = (3 << 14) | (12 << 10) | (31 << 5) | (2070 - 1972 - 96)
        assert encode_date_word(2070, 12, 31) == w_2070
        assert decode_date_word(w_2070) == (2070, 12, 31)
        # Last year of each age band / boundary years.
        assert decode_date_word(encode_date_word(2003, 1, 1)) == (2003, 1, 1)
        assert decode_date_word(encode_date_word(2004, 1, 1)) == (2004, 1, 1)

    def test_decode_rejects_garbage_fields(self):
        assert decode_date_word((13 << 10) | (1 << 5) | 1) is None  # month 13
        assert decode_date_word((0 << 10) | (1 << 5) | 1) is None  # month 0
        assert decode_date_word((1 << 10) | (0 << 5) | 1) is None  # day 0

    def test_encode_validates_ranges(self):
        with pytest.raises(ValueError):
            encode_date_word(1971, 1, 1)
        with pytest.raises(ValueError):
            encode_date_word(2100, 1, 1)
        with pytest.raises(ValueError):
            encode_date_word(1980, 13, 1)
        with pytest.raises(ValueError):
            encode_date_word(1980, 0, 1)
        with pytest.raises(ValueError):
            encode_date_word(1980, 1, 32)
        with pytest.raises(ValueError):
            encode_date_word(1980, 1, 0)


# ---------------------------------------------------------------------------
# View mappers
# ---------------------------------------------------------------------------

SAMPLE_BLOCKS = (0, 1, 25, 100, 493)


class TestViewMappers:
    def test_views_constant(self):
        assert VIEWS == ("logical", "rx01", "rx02", "rx50")

    @pytest.mark.parametrize("view", ["rx01", "rx02", "rx50"])
    def test_tables_match_independent_reference(self, view):
        for block in SAMPLE_BLOCKS:
            expected = [
                (track, 0, sector - 1)  # DEC 1-based -> module 0-based CHS
                for track, sector in _ref_block_table(view, block)
            ]
            assert logical_block_to_chs(view, block) == expected, (
                f"{view} block {block}"
            )

    def test_hardcoded_anchor_values(self):
        # Spot literals, hand-derived from the DY.MAC/DZDRV.MAC formulas
        # (0-based cylinder/head/sector as returned by the module).
        assert logical_block_to_chs("rx01", 0) == [
            (1, 0, 0),
            (1, 0, 2),
            (1, 0, 4),
            (1, 0, 6),
        ]
        assert logical_block_to_chs("rx01", 493) == [
            (76, 0, 1),
            (76, 0, 3),
            (76, 0, 5),
            (76, 0, 7),
        ]
        assert logical_block_to_chs("rx02", 100) == [(8, 0, 1), (8, 0, 3)]
        assert logical_block_to_chs("rx50", 0) == [(1, 0, 0)]
        assert logical_block_to_chs("rx50", 10) == [(2, 0, 2)]
        # RX50 wraparound: the last ten blocks land on physical track 0.
        assert logical_block_to_chs("rx50", 790) == [(0, 0, 8)]
        assert logical_block_to_chs("rx50", 799) == [(0, 0, 7)]

    @pytest.mark.parametrize("view", ["rx01", "rx02", "rx50"])
    def test_full_device_invariants(self, view):
        geom = VIEW_GEOMETRY[view]
        used = []
        for block in range(geom.total_blocks):
            used.extend(
                (cyl, sec) for cyl, _head, sec in logical_block_to_chs(view, block)
            )
        # Every (track, sector) used exactly once across all blocks.
        assert len(used) == len(set(used))
        tracks = {cyl for cyl, _sec in used}
        if view in ("rx01", "rx02"):
            # The DEC scheme skips physical track 0 entirely.
            assert 0 not in tracks
            assert tracks == set(range(1, 77))
        else:
            # RX50 wraps: all 80 tracks are used, incl. physical track 0.
            assert tracks == set(range(80))
            # Complete coverage: 800 blocks x 1 sector = every sector once.
            assert len(used) == 80 * 10
        for _cyl, sec in used:
            assert 0 <= sec < geom.sectors_per_track

    def test_device_capacities(self):
        assert VIEW_GEOMETRY["rx01"].total_blocks == 494
        assert VIEW_GEOMETRY["rx02"].total_blocks == 988
        assert VIEW_GEOMETRY["rx50"].total_blocks == 800

    @pytest.mark.parametrize(
        "view,bad_block", [("rx01", 494), ("rx02", 988), ("rx50", 800), ("rx01", -1)]
    )
    def test_out_of_range_block_raises(self, view, bad_block):
        with pytest.raises(ValueError):
            logical_block_to_chs(view, bad_block)

    def test_unknown_view_raises(self):
        with pytest.raises(ValueError):
            logical_block_to_chs("rx99", 0)

    def test_logical_view_identity(self):
        # Identity over a uniform 512-byte geometry (needs explicit geometry).
        assert logical_block_to_chs("logical", 0, sectors_per_track=10) == [(0, 0, 0)]
        assert logical_block_to_chs("logical", 25, sectors_per_track=10) == [(2, 0, 5)]
        # Two-headed geometry: sectors fill head 0 then head 1 per cylinder.
        assert logical_block_to_chs("logical", 9, sectors_per_track=9, heads=2) == [
            (0, 1, 0)
        ]
        assert logical_block_to_chs("logical", 19, sectors_per_track=9, heads=2) == [
            (1, 0, 1)
        ]
        with pytest.raises(ValueError):
            logical_block_to_chs("logical", 0)  # geometry required
        with pytest.raises(ValueError):
            logical_block_to_chs("logical", -1, sectors_per_track=10)


# ---------------------------------------------------------------------------
# Segment / home block parsing
# ---------------------------------------------------------------------------


class TestSegmentParsing:
    def test_v03b_segment_one(self):
        data = RX01_V03B.read_bytes()
        seg = _read_block(data, "rx01", 6) + _read_block(data, "rx01", 7)
        parsed = parse_segment(seg)
        hdr = parsed.header
        assert hdr.total_segments == 4
        assert hdr.next_segment == 0
        assert hdr.highest_in_use == 1
        assert hdr.extra_bytes == 0
        assert hdr.data_start_block == 14
        assert parsed.eos_found
        # 33 permanent files + 1 trailing empty, per the disk's DIR listing.
        assert len(parsed.entries) == 34
        first = parsed.entries[0]
        assert first.status == E_PERM
        assert first.is_permanent and not first.is_empty
        assert not first.is_tentative and not first.is_protected
        assert first.name == "SWAP  "
        assert first.file_type == "SYS"
        assert first.length == 24
        assert first.start_block == 14
        assert decode_date_word(first.date_word) == (1979, 3, 27)
        # Start blocks are implicit: header word 5 + cumulative lengths.
        third = parsed.entries[2]
        assert third.name == "PDMNSJ"
        assert third.start_block == 14 + 24 + 63  # SWAP(24) + DXMNSJ(63)
        last_perm = parsed.entries[32]
        assert (last_perm.name, last_perm.file_type) == ("STARTF", "COM")
        assert last_perm.start_block == 450
        empty = parsed.entries[33]
        assert empty.status & E_MPTY
        assert empty.length == 43
        assert empty.start_block == 451
        # 451 + 43 == 494 == full RX01 device capacity.
        assert empty.start_block + empty.length == VIEW_GEOMETRY["rx01"].total_blocks

    def test_v0501_pair_protected_entries(self):
        # The same segment bytes must come out of both archive conventions.
        seg_dsk = _read_block(V0501_DSK.read_bytes(), "rx02", 6)
        seg_img = _read_block(V0501_IMG.read_bytes(), "logical", 6)
        assert seg_dsk == seg_img
        parsed = parse_segment(
            seg_img + _read_block(V0501_IMG.read_bytes(), "logical", 7)
        )
        first = parsed.entries[0]
        assert first.status == E_PROT | E_PERM  # protected permanent file
        assert first.is_permanent and first.is_protected
        assert (first.name, first.file_type) == ("SWAP  ", "SYS")
        assert first.length == 26  # "SWAP  .SYS  26P 01-Feb-84" in DEC DIR
        assert decode_date_word(first.date_word) == (1984, 2, 1)

    def test_synthetic_bare_eos_and_extra_bytes(self):
        # E.EOS may be a bare final word; extra bytes enlarge each entry.
        hdr = struct.pack("<5H", 1, 0, 1, 2, 100)
        entry = (
            struct.pack(
                "<7H",
                E_PERM,
                rad50_encode("FOO"),
                rad50_encode("   "),
                rad50_encode("TXT"),
                5,
                0,
                encode_date_word(1985, 1, 2),
            )
            + b"\xaa\xbb"
        )  # 2 extra bytes
        eos = struct.pack("<H", E_EOS)
        seg = hdr + entry + eos
        seg += b"\x00" * (1024 - len(seg))
        parsed = parse_segment(seg)
        assert parsed.header.extra_bytes == 2
        assert parsed.eos_found
        assert len(parsed.entries) == 1
        entry0 = parsed.entries[0]
        assert (entry0.name, entry0.file_type) == ("FOO   ", "TXT")
        assert entry0.start_block == 100
        assert entry0.extra == b"\xaa\xbb"

    def test_segment_without_eos_stops_at_end(self):
        seg = struct.pack("<5H", 1, 0, 1, 0, 50) + b"\x00" * (1024 - 10)
        parsed = parse_segment(seg)
        assert not parsed.eos_found  # status 0 entries are not EOS markers

    def test_parse_segment_rejects_short_input(self):
        with pytest.raises(ValueError):
            parse_segment(b"\x00" * 1023)
        with pytest.raises(ValueError):
            parse_home_block(b"\x00" * 511)

    def test_home_block_fields(self):
        home = parse_home_block(_read_block(RX01_V03B.read_bytes(), "rx01", 1))
        assert home.pack_cluster_size == 1
        assert home.dir_start == 6
        assert home.system_version == "V3A"
        assert home.volume_id == "AS-5777C-BC "
        assert home.owner == "DX1 DISTRIB "
        assert home.system_id == "DECRT11A    "
        # Real DEC factory disks ship with a WRONG home-block checksum;
        # the field is exposed but must never be load-bearing.
        assert home.checksum_stored != home.checksum_computed

        home = parse_home_block(_read_block(V0501_IMG.read_bytes(), "logical", 1))
        assert home.volume_id == "BA-P732B-BC "
        assert home.owner == "RX2 AUTO    "  # matches the DEC DIR listing
        assert home.system_id == "DECRT11A    "
        assert home.system_version == "V05"


# ---------------------------------------------------------------------------
# RT11Config
# ---------------------------------------------------------------------------


class TestRT11Config:
    def test_defaults_and_matches(self):
        cfg = RT11Config(view="rx01", total_blocks=494)
        assert cfg.dir_start == DEFAULT_DIR_START == 6
        same = RT11Config(
            view="rx01", total_blocks=494, volume_id="RT11A", owner="X", system_id="Y"
        )
        # matches() keys on view + total_blocks only.
        assert RT11Config.matches(cfg, same)
        assert not RT11Config.matches(cfg, RT11Config(view="rx02", total_blocks=494))
        assert not RT11Config.matches(cfg, RT11Config(view="rx01", total_blocks=988))
        assert not RT11Config.matches(cfg, None)
        assert not RT11Config.matches(cfg, object())


# ---------------------------------------------------------------------------
# Structure scorer
# ---------------------------------------------------------------------------


class TestStructureScorer:
    def test_rx01_view_separation(self):
        # The load-bearing detection pin: a raw RX01 image must score >=
        # threshold under the rx01 view and ~0 under the logical view of the
        # exact same bytes.
        data = RX01_V03B.read_bytes()
        assert score_directory_structure(_reader(data, "rx01"), 494) == 90
        flat = score_directory_structure(_reader(data, "logical"), len(data) // BLOCK)
        assert flat < SCORE_THRESHOLD
        assert flat == 0

    def test_v0501_twin_pair_separation(self):
        # Same disk, both conventions, identical file size (512,512): only
        # the directory structure can resolve the view.
        dsk = V0501_DSK.read_bytes()
        img = V0501_IMG.read_bytes()
        assert score_directory_structure(_reader(dsk, "rx02"), 988) == 90
        assert (
            score_directory_structure(_reader(dsk, "logical"), len(dsk) // BLOCK)
            < SCORE_THRESHOLD
        )
        assert (
            score_directory_structure(_reader(img, "logical"), len(img) // BLOCK) == 90
        )
        assert score_directory_structure(_reader(img, "rx02"), 988) < SCORE_THRESHOLD

    def test_basic11_rx02_raw(self):
        data = BASIC11_RX02.read_bytes()
        assert score_directory_structure(_reader(data, "rx02"), 988) >= SCORE_THRESHOLD
        assert (
            score_directory_structure(_reader(data, "logical"), len(data) // BLOCK)
            < SCORE_THRESHOLD
        )

    def test_threshold_value(self):
        assert SCORE_THRESHOLD == 40

    def test_scorer_never_raises(self):
        def boom(_block):
            raise OSError("hardware error")

        assert score_directory_structure(boom, 494) == 0

        def short(_block):
            return b"\x00" * 17

        assert score_directory_structure(short, 494) == 0
        assert score_directory_structure(lambda _b: b"\x00" * BLOCK, 494) == 0
        # Pathological dir_start beyond the device.
        data = RX01_V03B.read_bytes()
        assert (
            score_directory_structure(_reader(data, "rx01"), 494, dir_start=9000) == 0
        )

    def test_home_block_alone_is_below_threshold(self):
        # An image with a DECRT11A home block but a destroyed directory must
        # stay below the detection threshold (home signals are WEAK).
        data = bytearray(RX01_V03B.read_bytes())
        view_reader = _reader(bytes(data), "rx01")
        good_home = view_reader(1)

        def reader(block):
            if block == 1:
                return good_home
            return b"\xff" * BLOCK

        score = score_directory_structure(reader, 494)
        assert score == 15
        assert score < SCORE_THRESHOLD

    def _synthetic_volume(self, *segments, total_segments=None):
        """A minimal flat volume: empty home block + directory segments.

        Each segment is (next_segment_link, entries_blob); segment N
        occupies blocks 6+2(N-1) and 6+2(N-1)+1.
        """
        if total_segments is None:
            total_segments = len(segments)
        blocks = {}
        for index, (next_segment, entries_blob) in enumerate(segments):
            seg = struct.pack("<5H", total_segments, next_segment, 1, 0, 20)
            seg += entries_blob
            seg += b"\x00" * (1024 - len(seg))
            blocks[6 + 2 * index] = seg[:512]
            blocks[6 + 2 * index + 1] = seg[512:]
        return lambda block: blocks.get(block, b"\x00" * BLOCK)

    def test_valid_header_with_bad_entries_scores_header_only(self):
        # No DECRT11A, valid header (+50), but the entry chain must NOT
        # earn its +25 when entries are structurally broken.
        good_entry = struct.pack(
            "<7H", E_PERM, rad50_encode("FOO"), 0, rad50_encode("TXT"), 5, 0, 0
        )
        eos = struct.pack("<H", E_EOS)

        # Unknown high status bit (0o20000 is undefined).
        bad_status = struct.pack("<7H", 0o20000, 0, 0, 0, 5, 0, 0)
        reader = self._synthetic_volume((0, bad_status + eos))
        assert score_directory_structure(reader, 494) == 50

        # Live (permanent) entry whose name word is junk RAD50.
        junk_name = struct.pack("<7H", E_PERM, 64000, 0, rad50_encode("TXT"), 5, 0, 0)
        reader = self._synthetic_volume((0, junk_name + eos))
        assert score_directory_structure(reader, 494) == 50

        # Entry lengths overrun the device capacity.
        huge = struct.pack(
            "<7H", E_PERM, rad50_encode("BIG"), 0, rad50_encode("DAT"), 60000, 0, 0
        )
        reader = self._synthetic_volume((0, huge + eos))
        assert score_directory_structure(reader, 494) == 50

        # Known status bit (E_READ) but no entry-type bit set.
        no_type = struct.pack("<7H", E_READ, 0, 0, 0, 5, 0, 0)
        reader = self._synthetic_volume((0, no_type + eos))
        assert score_directory_structure(reader, 494) == 50

        # A linked second segment without an end-of-segment marker.
        reader = self._synthetic_volume((2, good_entry + eos), (0, b""))
        assert score_directory_structure(reader, 494) == 50

        # Segment-1 link beyond total_segments invalidates the header itself.
        reader = self._synthetic_volume((9, good_entry + eos))
        assert score_directory_structure(reader, 494) == 0

        # A second segment whose own link is insane breaks the chain walk.
        reader = self._synthetic_volume((2, good_entry + eos), (9, good_entry + eos))
        assert score_directory_structure(reader, 494) == 50

        # The same single good entry with a sane chain earns the +25.
        reader = self._synthetic_volume((0, good_entry + eos))
        assert score_directory_structure(reader, 494) == 75

        # And a sane two-segment chain earns it too.
        reader = self._synthetic_volume((2, good_entry + eos), (0, good_entry + eos))
        assert score_directory_structure(reader, 494) == 75

    def test_self_linked_segment_chain_terminates(self):
        # Segments linking in a cycle must not loop the scorer forever.
        entry = struct.pack(
            "<7H", E_PERM, rad50_encode("FOO"), 0, rad50_encode("TXT"), 5, 0, 0
        )
        eos = struct.pack("<H", E_EOS)
        reader = self._synthetic_volume((2, entry + eos), (1, entry + eos))
        assert score_directory_structure(reader, 494) == 50


# ---------------------------------------------------------------------------
# Optional corpus sweep (requires the local, uncommitted image corpus)
# ---------------------------------------------------------------------------


def _corpus_entries():
    if not (LOCAL_RT11.is_dir() and CORPUS_INVENTORY.is_file()):
        return []
    with CORPUS_INVENTORY.open() as fh:
        return json.load(fh)


@pytest.mark.skipif(
    not (LOCAL_RT11.is_dir() and CORPUS_INVENTORY.is_file()),
    reason="local RT-11 corpus not present",
)
class TestCorpusSweep:
    @pytest.mark.parametrize(
        "entry", _corpus_entries(), ids=lambda e: Path(e["path"]).name
    )
    def test_scorer_agrees_with_inventory_view(self, entry):
        path = CORPUS_INVENTORY.parent.parent / entry["path"]
        if not path.is_file():
            pytest.skip(f"missing corpus file {path}")
        data = path.read_bytes()
        inv_view = "logical" if entry["view"] == "flat" else entry["view"]
        for view in ("logical", "rx01", "rx02"):
            if view != "logical":
                geom = VIEW_GEOMETRY[view]
                if len(data) != geom.image_size:
                    continue
                total = geom.total_blocks
            else:
                total = len(data) // BLOCK
            score = score_directory_structure(_reader(data, view), total)
            if view == inv_view:
                assert score >= SCORE_THRESHOLD, f"{path.name} under {view}: {score}"
            else:
                assert score < SCORE_THRESHOLD, f"{path.name} under {view}: {score}"
