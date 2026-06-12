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

import copy
import datetime
import hashlib
import json
import logging
import os
import random
import re
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Must be set before any Qt import (GUI smoke tests at the bottom).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import crcmod.predefined  # noqa: E402
import pytest  # noqa: E402

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.disk import Disk  # noqa: E402
from fatfloppy.core.drivers.img import IMGImageDriver  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402
from fatfloppy.core.filesystems.formats.cpm_formats import CPM_FORMATS  # noqa: E402
from fatfloppy.core.filesystems.formats.rt11_formats import RT11_FORMATS  # noqa: E402
from fatfloppy.core.filesystems.rt11_fs import RT11Filesystem  # noqa: E402
from fatfloppy.core.rt11_layout import (  # noqa: E402
    DEFAULT_DIR_START,
    E_EOS,
    E_MPTY,
    E_PERM,
    E_PROT,
    E_READ,
    E_TENT,
    MAX_SEGMENTS,
    RAD50_MAX_WORD,
    SCORE_THRESHOLD,
    SEGMENT_SIZE,
    VIEW_GEOMETRY,
    VIEWS,
    DirEntry,
    ParsedSegment,
    RT11Config,
    SegmentHeader,
    decode_date_word,
    default_segment_count,
    encode_date_word,
    encode_home_block,
    logical_block_to_chs,
    parse_home_block,
    parse_segment,
    rad50_decode,
    rad50_encode,
    rad50_is_valid,
    score_directory_structure,
    segment_max_entries,
    serialize_segment,
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
# Segment serialization (inverse of parse_segment) and home block encoding
# ---------------------------------------------------------------------------


class TestSegmentSerialization:
    @pytest.mark.parametrize(
        "path,view",
        [(RX01_V03B, "rx01"), (V0501_IMG, "logical"), (BASIC11_RX02, "rx02")],
        ids=["v03b-0x20-tail", "v0501-0x88-tail", "basic11-full-segment"],
    )
    def test_real_segment_round_trips_byte_identically(self, path, view):
        # Lossless-edit pin: the three committed images carry three distinct
        # post-EOS tails (spaces, 0x88 fill, zeros) and BASIC-11 segment 1 is
        # a completely full 72-entry segment. parse -> serialize must
        # reproduce every byte.
        data = path.read_bytes()
        original = _read_block(data, view, 6) + _read_block(data, view, 7)
        assert serialize_segment(parse_segment(original)) == original

    def test_extra_bytes_round_trip(self):
        # A volume initialized with extra bytes per entry must survive a
        # parse -> serialize cycle losslessly, including the extra payload.
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
        )
        seg = hdr + entry + struct.pack("<H", E_EOS)
        seg += b"\xcc" * (SEGMENT_SIZE - len(seg))  # junk tail, must survive
        assert serialize_segment(parse_segment(seg)) == seg

    def test_serializer_rejects_unterminated_segment(self):
        # We only ever write EOS-terminated segments; a parse without an EOS
        # marker is not serializable.
        seg = struct.pack("<5H", 1, 0, 1, 0, 50) + b"\x00" * (SEGMENT_SIZE - 10)
        parsed = parse_segment(seg)
        assert not parsed.eos_found
        with pytest.raises(ValueError):
            serialize_segment(parsed)

    def test_serializer_rejects_overflow(self):
        # 73 entries (extra_bytes 0) cannot fit with the EOS word.
        base = _read_block(BASIC11_RX02.read_bytes(), "rx02", 6) + _read_block(
            BASIC11_RX02.read_bytes(), "rx02", 7
        )
        parsed = parse_segment(base)  # exactly 72 entries: at capacity
        assert len(parsed.entries) == 72
        import dataclasses

        overfull = dataclasses.replace(
            parsed, entries=parsed.entries + (parsed.entries[0],)
        )
        with pytest.raises(ValueError):
            serialize_segment(overfull)

    def test_segment_max_entries(self):
        # (1024 - 10 header - 2 EOS) // entry size; 72 for plain entries,
        # matching the manual's S = (512-5)/(7+N) integer formula (sec 1.1.4).
        assert segment_max_entries(0) == 72
        assert segment_max_entries(2) == 63
        assert segment_max_entries(64) == 12

    def test_default_segment_counts(self):
        # RT-11 V5.4D DUP defaults (PUTR.ASM rtnseg table, cross-checked
        # against the RT-11 V04.00 SUG device table quoted there): RX01
        # (494 blocks) -> 1, RX02 (988) and RX50 (800) -> 4. The V&FF manual
        # (sec 1.1.2) defers per-device defaults to DUP, so the DUP table is
        # the authority.
        assert default_segment_count(494) == 1
        assert default_segment_count(512) == 1
        assert default_segment_count(513) == 4
        assert default_segment_count(800) == 4
        assert default_segment_count(988) == 4
        assert default_segment_count(2048) == 4
        assert default_segment_count(2049) == 16
        assert default_segment_count(12288) == 16
        assert default_segment_count(12289) == MAX_SEGMENTS
        assert default_segment_count(65535) == MAX_SEGMENTS


class TestHomeBlockEncoding:
    def test_round_trip_through_parser(self):
        block = encode_home_block(volume_id="MYVOLUME", owner="ME")
        home = parse_home_block(block)
        assert home.pack_cluster_size == 1
        assert home.dir_start == 6
        assert home.system_version == "V05"
        assert home.volume_id == "MYVOLUME    "
        assert home.owner == "ME          "
        assert home.system_id == "DECRT11A    "
        # Unlike real DEC factory disks, OUR home blocks carry a correct
        # checksum (manual sec 1.1.1: simple additive sum of the other 255
        # words, FILES-11 ODS style).
        assert home.checksum_stored == home.checksum_computed

    def test_checksum_algorithm_pinned_independently(self):
        # Independent transcription of the manual's algorithm:
        #   CLR R1; MOV #255.,R2; 10$: ADD (R0)+,R1; SOB R2,10$; MOV R1,@R0
        block = encode_home_block(volume_id="CHKSUM")
        words = struct.unpack("<256H", block)
        assert words[255] == sum(words[:255]) & 0xFFFF

    def test_defaults_and_field_truncation(self):
        block = encode_home_block()
        home = parse_home_block(block)
        assert home.volume_id == "RT11A       "  # manual table 1-1 default
        assert home.owner == " " * 12
        block = encode_home_block(volume_id="ABCDEFGHIJKLMNOP", dir_start=10)
        home = parse_home_block(block)
        assert home.volume_id == "ABCDEFGHIJKL"  # truncated to the 12-byte field
        assert home.dir_start == 10


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


# Materialized with eager ids: a callable `ids=` over an EMPTY parametrize
# list breaks collection of the whole file when local_images/ is absent.
_CORPUS = _corpus_entries()

# Local-only oracle manifests (rt11probe.py extractions): these corpus images
# are NOT committed as resources, so their per-file content oracles live next
# to the corpus inventory instead of as pinned literals (READ_ORACLES below
# pins the committed ones). The IMD entry exercises the container end-to-end.
_MANIFEST_DIR = CORPUS_INVENTORY.parent / "manifests"
_MANIFEST_CASES = [
    (
        "v40_disk1.manifest.json",
        "images/RT-11_v4.0_ORIGINAL_DISKS.d"
        "/RT-11 v4.0 BIN RX01 1-7 (ORIGINAL DISK)"
        "/RT-11 v4.0 BIN RX01 1-7 (ORIGINAL DISK).img",
    ),
    ("v54_rx01_imd.manifest.json", "images/RT11RX01.IMD"),
    (
        "v54b_auto_logical.manifest.json",
        "images/BA-P732I-BC_RT-11_V5.4B_AUTO_87.DSK",
    ),
]


@pytest.mark.skipif(
    not (LOCAL_RT11.is_dir() and CORPUS_INVENTORY.is_file()),
    reason="local RT-11 corpus not present",
)
class TestCorpusSweep:
    @pytest.mark.parametrize(
        "entry", _CORPUS, ids=[Path(e["path"]).name for e in _CORPUS]
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

    @pytest.mark.parametrize(
        "entry", _CORPUS, ids=[Path(e["path"]).name for e in _CORPUS]
    )
    def test_controller_detects_inventory_image(self, entry):
        # Full-stack detection over the same inventory: every image must
        # auto-open as RT-11 with the inventory's view, permanent-file
        # count, and volume id.
        path = CORPUS_INVENTORY.parent.parent / entry["path"]
        if not path.is_file():
            pytest.skip(f"missing corpus file {path}")
        controller = _open(path)
        try:
            fs = controller.filesystem
            assert isinstance(fs, RT11Filesystem)
            assert fs.filesystem_type == "RT11"
            cfg = fs.get_specific_config()
            assert isinstance(cfg, RT11Config)
            expected = "logical" if entry["view"] == "flat" else entry["view"]
            assert cfg.view == expected, f"{path.name}: {cfg.view} != {expected}"
            items = fs.list_directory("/")
            permanent = [i for i in items if "TENT" not in i.attributes]
            assert len(permanent) == entry["perm_files"], path.name
            assert fs.get_volume_label() == (entry["volume_id"].strip() or None)
        finally:
            controller.close_disk()

    @pytest.mark.parametrize(
        "manifest_name,rel_path",
        _MANIFEST_CASES,
        ids=[m.split(".")[0] for m, _p in _MANIFEST_CASES],
    )
    def test_extracted_content_matches_prototype_manifest(
        self, manifest_name, rel_path
    ):
        # Content spot-check against the independent rt11probe extractor:
        # the full listing (names, sizes, real dates) and every file's
        # sha256 must match its manifest.
        image = CORPUS_INVENTORY.parent.parent / rel_path
        manifest_path = _MANIFEST_DIR / manifest_name
        if not (image.is_file() and manifest_path.is_file()):
            pytest.skip(f"missing corpus image or manifest for {manifest_name}")
        with manifest_path.open() as fh:
            manifest = json.load(fh)
        assert manifest
        controller = _open(image)
        try:
            fs = controller.filesystem
            assert isinstance(fs, RT11Filesystem)
            items = fs.list_directory("/")
            assert [i.name for i in items] == [e["name"] for e in manifest]
            for item, entry in zip(items, manifest):
                assert item.size == entry["blocks"] * BLOCK, entry["name"]
                assert item.datetime == datetime.datetime.strptime(
                    entry["date"], "%d-%b-%Y"
                ), entry["name"]
                data = fs.read_file(entry["name"])
                digest = hashlib.sha256(data).hexdigest()
                assert digest == entry["sha256"], entry["name"]
        finally:
            controller.close_disk()


# ---------------------------------------------------------------------------
# Task 3: filesystem plugin -- profiles, detection, controller read path
# ---------------------------------------------------------------------------

NO_DATE = datetime.datetime(1972, 1, 1)  # epoch of the RT-11 date word

RT11_PROFILE_NAMES = (
    "rt11_rx01",
    "rt11_rx02",
    "rt11_rx50",
    "rt11_logical_494",
    "rt11_logical_500",
    "rt11_logical_800",
    "rt11_logical_988",
)

CPM_8IN_IMG = RES.parent / "CPM" / "disk1.img"
FAT_144M_IMG = RES.parent / "populated_read_test_144m.img"


def _open(path):
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto"), (
        f"failed to open {Path(path).name}"
    )
    return controller


class TestRT11Profiles:
    def test_each_profile_registered_exactly_once(self):
        controller = DiskController()
        names = [name for name, _desc in controller.list_formats()]
        for expected in RT11_PROFILE_NAMES:
            assert names.count(expected) == 1, expected

    def test_raw_profile_geometries_and_configs(self):
        cases = [
            ("rt11_rx01", 77, 26, 128, 256256, "rx01", 494),
            ("rt11_rx02", 77, 26, 256, 512512, "rx02", 988),
            ("rt11_rx50", 80, 10, 512, 409600, "rx50", 800),
        ]
        for name, cyls, spt, bps, size, view, blocks in cases:
            profile = RT11_FORMATS[name]
            pf = profile.physical_format
            assert (pf.cylinders, pf.heads) == (cyls, 1), name
            tf = pf.track_formats[0]
            assert (tf.sectors_per_track, tf.bytes_per_sector) == (spt, bps), name
            assert pf.total_bytes == size, name
            cfg = profile.filesystem_config
            assert isinstance(cfg, RT11Config), name
            assert (cfg.view, cfg.total_blocks) == (view, blocks), name
            assert profile.get_filesystem_type() == "RT11", name

    def test_logical_profiles_use_synthetic_uniform_geometry(self):
        # cylinders = total_blocks x heads=1 x spt=1 x 512 bytes: a pure
        # 1:1 byte-offset container for logical-block-order images.
        cases = [
            ("rt11_logical_494", 494, 252928),
            ("rt11_logical_500", 500, 256000),
            ("rt11_logical_800", 800, 409600),
            ("rt11_logical_988", 988, 505856),
        ]
        for name, blocks, size in cases:
            profile = RT11_FORMATS[name]
            pf = profile.physical_format
            assert (pf.cylinders, pf.heads) == (blocks, 1), name
            tf = pf.track_formats[0]
            assert (tf.sectors_per_track, tf.bytes_per_sector) == (1, 512), name
            assert pf.total_bytes == size, name
            cfg = profile.filesystem_config
            assert (cfg.view, cfg.total_blocks) == ("logical", blocks), name


CONTROLLER_CASES = [
    # (path, profile, resolved view, container blocks)
    (RX01_V03B, "rt11_rx01", "rx01", 494),
    (V0501_DSK, "rt11_rx02", "rx02", 988),
    # A logical-order image at exactly raw-RX02 size opens under the rx02
    # profile geometry; the view resolver picks "logical" from the contents.
    (V0501_IMG, "rt11_rx02", "logical", 1001),
    (BASIC11_RX02, "rt11_rx02", "rx02", 988),
]


class TestControllerEndToEnd:
    @pytest.mark.parametrize(
        "path,profile,view,total", CONTROLLER_CASES, ids=lambda c: getattr(c, "name", c)
    )
    def test_auto_detects_rt11_with_resolved_view(self, path, profile, view, total):
        controller = _open(path)
        try:
            assert isinstance(controller.filesystem, RT11Filesystem)
            assert controller.filesystem.filesystem_type == "RT11"
            name, _config, _pf = controller.detect_format()
            assert name == profile
            cfg = controller.filesystem.get_specific_config()
            assert isinstance(cfg, RT11Config)
            assert (cfg.view, cfg.total_blocks) == (view, total)
            assert controller.filesystem.list_directory("/")
        finally:
            controller.close_disk()

    def test_v03b_listing_matches_dir(self):
        # Per the disk's own DIR listing: 33 permanent files, SWAP.SYS first
        # (24 blocks, 27-Mar-79, starts at block 14), STARTF.COM last.
        controller = _open(RX01_V03B)
        try:
            items = controller.filesystem.list_directory("/")
            assert len(items) == 33
            first = items[0]
            assert first.name == "SWAP.SYS"
            assert first.size == 24 * BLOCK
            assert first.datetime == datetime.datetime(1979, 3, 27)
            assert first.starting_cluster == 14
            assert not first.is_dir
            assert items[-1].name == "STARTF.COM"
        finally:
            controller.close_disk()

    def test_v0501_protected_permanent_attribute(self):
        # Genuine DEC DIR listing pins "SWAP  .SYS    26P 01-Feb-84".
        controller = _open(V0501_DSK)
        try:
            items = controller.filesystem.list_directory("/")
            assert len(items) == 28
            swap = items[0]
            assert swap.name == "SWAP.SYS"
            assert swap.size == 26 * BLOCK
            assert swap.datetime == datetime.datetime(1984, 2, 1)
            assert "PROT" in swap.attributes
        finally:
            controller.close_disk()


class TestPhysicalLogicalPair:
    """The view-resolver acid test: the SAME V05.01 floppy archived in raw
    RX02 physical order (.DSK) and logical block order (.IMG), both 512,512
    bytes, must produce byte-identical files and identical listings."""

    def test_identical_listings_and_per_file_hashes(self):
        c_dsk = _open(V0501_DSK)
        c_img = _open(V0501_IMG)
        try:
            l_dsk = c_dsk.filesystem.list_directory("/")
            l_img = c_img.filesystem.list_directory("/")
            key = [(i.name, i.size, i.datetime, i.attributes) for i in l_dsk]
            assert key == [(i.name, i.size, i.datetime, i.attributes) for i in l_img]
            assert len(l_dsk) == 28
            for info in l_dsk:
                data_dsk = c_dsk.filesystem.read_file(info.name)
                data_img = c_img.filesystem.read_file(info.name)
                assert len(data_dsk) == info.size, info.name
                assert (
                    hashlib.sha256(data_dsk).hexdigest()
                    == hashlib.sha256(data_img).hexdigest()
                ), info.name
        finally:
            c_dsk.close_disk()
            c_img.close_disk()


# ---------------------------------------------------------------------------
# read_file content oracles.
#
# Derivation: the sha256 literals below are pinned from the output of
# docs/superpowers/research/rt11/oracles/rt11probe.py `extract` over the
# committed images (manifests m_v03b1.json, m_dsk.json / m_img.json and
# manifests/basic11_rx02_raw.manifest.json). rt11probe is an INDEPENDENT
# reader written from the DEC V&FF manual (AA-PD6PA-TC), not from the
# production module, so these are true content oracles. The corpus
# inventory (corpus_inventory.json) manifests whole-image data only.
# ---------------------------------------------------------------------------

READ_ORACLES = [
    (
        RX01_V03B,
        "SWAP.SYS",
        24,
        "038c4af99d4a5fd7014a30bff4aba41c1cf8647006dfe85d1a0844eb2a2b37ab",
    ),
    (
        RX01_V03B,
        "DXMNSJ.SYS",
        63,
        "8041c1a1e4a93fcc43bc0d9406d9e9da95132c975bf87e6ae338c24711013186",
    ),
    (
        RX01_V03B,
        "STARTF.COM",
        1,
        "8b3d0f838da7e799e50ca0ac035ac79d0ab2c8b451aa800e6d9afcea5708d9dd",
    ),
    (
        V0501_DSK,
        "SWAP.SYS",
        26,
        "e57887d05051817eff3b9e8ae1df2e0727c8d96fb2e4795dba313cf08581921a",
    ),
    (
        V0501_DSK,
        "QUEMAN.SAV",
        15,
        "dfe51784adf05805e6353eaf421658f86ea7b011f88c4a965ab3ffb73a4b3b23",
    ),
    (
        V0501_DSK,
        "V5NOTE.TXT",
        29,
        "1e0ff979205ebc181702e1a64dc401e87418faf1edcdde5cf057e0a8b9b10e1f",
    ),
    (
        V0501_IMG,
        "SWAP.SYS",
        26,
        "e57887d05051817eff3b9e8ae1df2e0727c8d96fb2e4795dba313cf08581921a",
    ),
    (
        V0501_IMG,
        "QUEMAN.SAV",
        15,
        "dfe51784adf05805e6353eaf421658f86ea7b011f88c4a965ab3ffb73a4b3b23",
    ),
    (
        V0501_IMG,
        "V5NOTE.TXT",
        29,
        "1e0ff979205ebc181702e1a64dc401e87418faf1edcdde5cf057e0a8b9b10e1f",
    ),
    (
        BASIC11_RX02,
        "BSOT0D.EAE",
        12,
        "f47fa8549073cb4b32354f3a35afd3909fafe24b03c8102d8b2ea1dfd43925af",
    ),
    # CNC.SAV lives in linked segment 2: exercises the segment-chain walk.
    (
        BASIC11_RX02,
        "CNC.SAV",
        58,
        "8ce0c092d040197ff9ffaa27d22f6c595159a1dd9a2e10d303438825396c14d3",
    ),
    (
        BASIC11_RX02,
        "NULLPU.XYZ",
        1,
        "501384736d2398dc67a9ad4e9c442c7b7bd2047d1a0e8c143354370dee95bad8",
    ),
]


class TestReadFileOracles:
    @pytest.mark.parametrize(
        "path,name,blocks,sha",
        READ_ORACLES,
        ids=[f"{p.name}:{n}" for p, n, _b, _s in READ_ORACLES],
    )
    def test_content_hash_matches_independent_extractor(self, path, name, blocks, sha):
        controller = _open(path)
        try:
            data = controller.filesystem.read_file(name)
            assert len(data) == blocks * BLOCK
            assert hashlib.sha256(data).hexdigest() == sha
        finally:
            controller.close_disk()

    def test_leading_slash_and_case_are_normalized(self):
        controller = _open(RX01_V03B)
        try:
            direct = controller.filesystem.read_file("SWAP.SYS")
            assert controller.filesystem.read_file("/SWAP.SYS") == direct
            assert controller.filesystem.read_file("swap.sys") == direct
        finally:
            controller.close_disk()

    def test_missing_file_raises(self):
        controller = _open(RX01_V03B)
        try:
            with pytest.raises(FileNotFoundError):
                controller.filesystem.read_file("NOSUCH.FIL")
        finally:
            controller.close_disk()


class TestTentativeFiles:
    def test_basic11_tentative_listed_with_tent_attribute(self):
        # The committed BASIC-11 disk carries one tentative file (a file
        # opened but never closed by the original system): TEST.DAT, 4
        # blocks, no date word, starting at block 734 (per rt11probe).
        controller = _open(BASIC11_RX02)
        try:
            items = controller.filesystem.list_directory("/")
            assert len(items) == 122  # 121 permanent + 1 tentative
            tent = [i for i in items if "TENT" in i.attributes]
            assert [t.name for t in tent] == ["TEST.DAT"]
            entry = tent[0]
            assert entry.size == 4 * BLOCK
            assert entry.datetime == NO_DATE  # date word 0 == no date
            assert entry.starting_cluster == 734
            data = controller.filesystem.read_file("TEST.DAT")
            assert len(data) == 4 * BLOCK
        finally:
            controller.close_disk()

    def test_permanent_file_without_date_gets_epoch(self):
        controller = _open(BASIC11_RX02)
        try:
            items = {i.name: i for i in controller.filesystem.list_directory("/")}
            assert items["T.BAS"].datetime == NO_DATE
        finally:
            controller.close_disk()


class TestDetectionMatrix:
    def test_cpm_8inch_disk_scores_zero_on_rt11(self):
        # Cross-pin with a real committed CP/M 8" image, both with the
        # rt11_rx01 profile config applied and with no config at all.
        profile = RT11_FORMATS["rt11_rx01"]
        for config in (profile.filesystem_config, None):
            driver = IMGImageDriver(str(CPM_8IN_IMG))
            disk = Disk(driver)
            disk.set_geometry(copy.deepcopy(profile.physical_format))
            fs = RT11Filesystem(disk, config=config)
            assert fs.get_validity_score() == 0

    def test_rt11_rx01_scores_zero_on_cpm(self):
        # Truthful pin (verified empirically): CPMFilesystem scores exactly
        # 0 on the committed RX01 under both 8" SSSD profile configs --
        # below CP/M's own threshold (50) and the IMG claim floor (30).
        for pname in ("cpm_8_sssd_250k_interleave6", "cpm_8_sssd_250k_sequential"):
            profile = CPM_FORMATS[pname]
            driver = IMGImageDriver(str(RX01_V03B))
            disk = Disk(driver)
            disk.set_geometry(copy.deepcopy(profile.physical_format))
            fs = CPMFilesystem(disk, config=profile.filesystem_config)
            assert fs.get_validity_score() == 0

    def test_cpm_8inch_disk_still_detects_as_cpm(self):
        # Adding RT-11 profiles at the same 256,256-byte size must not
        # disturb CP/M detection of a real CP/M 8" disk.
        controller = _open(CPM_8IN_IMG)
        try:
            assert isinstance(controller.filesystem, CPMFilesystem)
            name, _config, _pf = controller.detect_format()
            assert name == "cpm_8_sssd_250k_interleave6"
        finally:
            controller.close_disk()

    def test_fat12_disk_scores_zero_on_rt11(self):
        controller = _open(FAT_144M_IMG)
        try:
            assert not isinstance(controller.filesystem, RT11Filesystem)
            fs = RT11Filesystem(controller.disk)
            assert fs.get_validity_score() == 0
        finally:
            controller.close_disk()

    def test_validity_score_never_raises_without_geometry(self):
        driver = IMGImageDriver(str(RX01_V03B))
        fs = RT11Filesystem(Disk(driver))  # no geometry set at all
        assert fs.get_validity_score() == 0
        assert fs.get_specific_config() is None
        assert fs.get_allocated_units() == []
        assert fs.get_free_space() == (0, 0)
        assert fs.get_disk_map_layout() == {}


RT11_IMD = LOCAL_RT11 / "RT11RX01.IMD"


@pytest.mark.skipif(not RT11_IMD.is_file(), reason="local RT-11 corpus not present")
class TestIMDPath:
    def test_imd_rx01_detects_rt11(self):
        controller = _open(RT11_IMD)
        try:
            assert isinstance(controller.filesystem, RT11Filesystem)
            cfg = controller.filesystem.get_specific_config()
            assert (cfg.view, cfg.total_blocks) == ("rx01", 494)
            assert controller.filesystem.list_directory("/")
        finally:
            controller.close_disk()


# ---------------------------------------------------------------------------
# TD0 container path: the README claims RT-11 works inside Teledisk archives
# ("RT-11 ... IMG, IMD, TD0").  No real RT-11 TD0 is committed, so one is
# SYNTHESIZED here -- a normal-compression archive (layout per the TD0
# driver's parser, td0notes.txt sections 3-7) wrapped around the committed
# raw RX01 -- and must detect and read identically to the raw image.
# ---------------------------------------------------------------------------

_TD0_CRC16 = crcmod.predefined.mkCrcFun("crc-16-teledisk")


def _synthesize_rt11_td0(raw_rx01: bytes, out_path: Path) -> None:
    """Wraps a raw RX01 image (77x1x26x128, physical order) in a normal TD0.

    Header: 'TD' signature (uncompressed body), sequence 0, version 0x15,
    data-rate byte 0x80 (250 kbps | FM bit -- 8" single density), drive
    type 5 (8"), stepping 0 (bit 7 clear: no comment block), single-sided;
    CRC-16-teledisk over bytes 0..9 stored little-endian at bytes 10-11.
    Per track: sector count / cylinder / head plus the low CRC byte over
    those three bytes.  Per sector: id-cyl / id-head / 1-based sector ID /
    size code 0 (128 bytes) / flags 0 / low CRC byte over the decoded
    data, then the data block (LE16 length = data + method byte, method 0
    = raw).  The track list ends with the 0xFF terminator.
    """
    header = struct.pack("<2sBBBBBBBB", b"TD", 0, 0, 0x15, 0x80, 5, 0, 0, 1)
    header += struct.pack("<H", _TD0_CRC16(header))

    body = bytearray()
    for cyl in range(77):
        track = struct.pack("<BBB", 26, cyl, 0)
        body += track + bytes([_TD0_CRC16(track) & 0xFF])
        for sector_id in range(1, 27):
            offset = (cyl * 26 + sector_id - 1) * 128
            data = raw_rx01[offset : offset + 128]
            scrc = _TD0_CRC16(data) & 0xFF
            body += struct.pack("<BBBBBB", cyl, 0, sector_id, 0, 0, scrc)
            body += struct.pack("<HB", len(data) + 1, 0) + data
    body += b"\xff"  # track-list terminator
    out_path.write_bytes(header + bytes(body))


class TestTD0Path:
    def test_synthesized_td0_rx01_detects_rt11(self, tmp_path):
        td0 = tmp_path / "rt11_v03b.td0"
        _synthesize_rt11_td0(RX01_V03B.read_bytes(), td0)
        controller = _open(td0)
        try:
            assert controller.driver.driver_type == "TD0"
            assert isinstance(controller.filesystem, RT11Filesystem)
            assert controller.filesystem.filesystem_type == "RT11"
            cfg = controller.filesystem.get_specific_config()
            assert (cfg.view, cfg.total_blocks) == ("rx01", 494)
            items = controller.filesystem.list_directory("/")
            assert len(items) == 33  # all permanent, matching the raw RX01
            swap_sha = next(
                sha
                for path, name, _blocks, sha in READ_ORACLES
                if path == RX01_V03B and name == "SWAP.SYS"
            )
            data = controller.filesystem.read_file("SWAP.SYS")
            assert hashlib.sha256(data).hexdigest() == swap_sha
        finally:
            controller.close_disk()


# ---------------------------------------------------------------------------
# DEC DIR oracle: genuine DIR listings archived alongside the disks.
# ---------------------------------------------------------------------------

_DIR_ENTRY_RE = re.compile(
    r"([A-Z0-9$%]{1,6}) *\.([A-Z0-9$%]{1,3}) +(\d+)(P?) +(\d{2})-([A-Za-z]{3})-(\d{2})"
)
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip


def _parse_dec_dir(text):
    """Parses a two-column RT-11 DIR listing into (entries, files, free)."""
    entries = []
    for match in _DIR_ENTRY_RE.finditer(text):
        base, ext, blocks, prot, day, mon, yy = match.groups()
        year = 1900 + int(yy) if int(yy) >= 72 else 2000 + int(yy)
        entries.append(
            {
                "name": f"{base}.{ext}",
                "blocks": int(blocks),
                "protected": prot == "P",
                "date": datetime.datetime(year, _MONTHS[mon.upper()], int(day)),
            }
        )
    counts = re.search(r"(\d+) Files, (\d+) Blocks", text)
    free = re.search(r"(\d+) Free blocks", text)
    assert counts and free, "trailer lines missing from DIR listing"
    return entries, int(counts.group(1)), int(free.group(1))


@pytest.mark.skipif(not LOCAL_RT11.is_dir(), reason="local RT-11 corpus not present")
class TestDECDirOracle:
    @pytest.mark.parametrize(
        "image,listing",
        [
            ("RT11-V05.01.d/BA-P732B-BC.DSK", "RT11-V05.01.d/BA-P732B-BC.TXT"),
            ("BA-P732I-BC_RT-11_V5.4B_AUTO_87.DSK", "p732i.dir.txt"),
        ],
        ids=["BA-P732B-BC", "BA-P732I-BC"],
    )
    def test_matches_genuine_dec_dir_listing(self, image, listing):
        img_path = LOCAL_RT11 / image
        txt_path = LOCAL_RT11 / listing
        if not (img_path.is_file() and txt_path.is_file()):
            pytest.skip(f"missing corpus file {img_path.name} / {txt_path.name}")
        expected, n_files, free_blocks = _parse_dec_dir(
            txt_path.read_text(errors="replace")
        )
        assert len(expected) == n_files  # listing parser self-check
        controller = _open(img_path)
        try:
            # DIR omits tentative files by default; ours flags them TENT.
            ours = [
                i
                for i in controller.filesystem.list_directory("/")
                if "TENT" not in i.attributes
            ]
            assert [i.name for i in ours] == [e["name"] for e in expected]
            for mine, ref in zip(ours, expected):
                assert mine.size == ref["blocks"] * BLOCK, mine.name
                assert mine.datetime == ref["date"], mine.name
                assert ("PROT" in mine.attributes) == ref["protected"], mine.name
            free_bytes, _total = controller.filesystem.get_free_space()
            assert free_bytes == free_blocks * BLOCK
        finally:
            controller.close_disk()


class TestFilesystemSurface:
    def test_allocation_unit_size_is_512(self):
        controller = _open(RX01_V03B)
        try:
            assert controller.filesystem.allocation_unit_size == BLOCK
        finally:
            controller.close_disk()

    def test_free_space_v03b(self):
        controller = _open(RX01_V03B)
        try:
            free, total = controller.filesystem.get_free_space()
            assert free == 43 * BLOCK  # single trailing empty run
            # Total: the directory-covered data area (sum of all entry
            # lengths) -- 494 device blocks minus boot/home/reserved/dir.
            assert total == 480 * BLOCK
        finally:
            controller.close_disk()

    def test_file_allocation_units_contiguous(self):
        controller = _open(RX01_V03B)
        try:
            units = controller.filesystem.get_file_allocation_units("SWAP.SYS")
            assert units == list(range(14, 38))  # 24 contiguous blocks
        finally:
            controller.close_disk()

    def test_allocated_units(self):
        controller = _open(RX01_V03B)
        try:
            units = set(controller.filesystem.get_allocated_units())
            # Boot, home, reserved and directory blocks are always allocated.
            assert set(range(0, 14)).issubset(units)
            # The trailing 43-block empty run (451-493) is free.
            assert units.isdisjoint(range(451, 494))
            assert max(units) == 450
        finally:
            controller.close_disk()

    def test_disk_map_layout_four_types(self):
        controller = _open(RX01_V03B)
        try:
            layout = controller.filesystem.get_disk_map_layout()
            assert len(layout["legend"]) == 4
            assert layout["allocation_unit_size_sectors"] == 4  # 512 // 128
            get_type = layout["get_sector_type"]
            spt = 26

            def lba_of(block):
                cyl, _head, sec = logical_block_to_chs("rx01", block)[0]
                return cyl * spt + sec

            # Physical track 0 is outside the RT-11 block space entirely.
            assert get_type(0) == "system"
            assert get_type(lba_of(0)) == "system"  # boot block
            assert get_type(lba_of(1)) == "system"  # home block
            assert get_type(lba_of(6)) == "directory"
            assert get_type(lba_of(14)) == "file"  # SWAP.SYS first block
            assert get_type(lba_of(451)) == "free"  # empty run
            legend_types = set(layout["type_color_map"])
            assert {"system", "directory", "file", "free"} <= legend_types
        finally:
            controller.close_disk()

    def test_display_info_and_volume_label(self):
        controller = _open(RX01_V03B)
        try:
            info = controller.filesystem.get_display_info()
            assert info["Filesystem"] == "RT-11"
            assert info["View"] == "rx01 (physical sector order)"
            assert info["Total Blocks"] == "494"
            assert info["Volume ID"] == "AS-5777C-BC"
            assert info["Owner"] == "DX1 DISTRIB"
            assert info["System ID"] == "DECRT11A"
            assert info["System Version"] == "V3A"
            assert info["Directory Segments"] == "1/4"
            assert info["Files"] == "33"
            assert info["Tentative Files"] == "0"
            assert info["Free Blocks"] == "43"
            # Single trailing empty run: free space, not fragmentation.
            assert info["Fragmentation"] == "0 free run(s) between files"
            assert controller.filesystem.get_volume_label() == "AS-5777C-BC"
        finally:
            controller.close_disk()

    def test_create_directory_not_supported(self):
        controller = _open(RX01_V03B)
        try:
            with pytest.raises(NotImplementedError):
                controller.filesystem.create_directory("/SUB")
        finally:
            controller.close_disk()


# ---------------------------------------------------------------------------
# Synthetic corruption: defensive read-path branches pinned against patched
# copies of the committed V5.01 logical image (block N == byte offset N*512).
# A header-valid volume with broken entries/links scores 65 (50 header + 15
# DECRT11A) which is >= the 40 threshold, so these branches are all live.
# ---------------------------------------------------------------------------

SEG1_OFF = 6 * BLOCK  # segment 1 byte offset under the logical view
ENTRY0_OFF = SEG1_OFF + 10  # first entry after the five-word header


def _patched_v0501(tmp_path, mutate):
    """Copy the committed logical IMG to tmp and apply ``mutate`` to it."""
    data = bytearray(V0501_IMG.read_bytes())
    mutate(data)
    path = tmp_path / "patched.img"
    path.write_bytes(bytes(data))
    return path


class TestSyntheticCorruption:
    def test_insane_length_word_lists_but_read_is_bounded(self, tmp_path):
        # Entry length word (5th word) -> 0xFFFF: the listing must survive
        # (no bounds check needed there) while read_file hits the overrun
        # guard with a bounded ValueError instead of reading wild blocks.
        def mutate(data):
            struct.pack_into("<H", data, ENTRY0_OFF + 8, 0xFFFF)

        controller = _open(_patched_v0501(tmp_path, mutate))
        try:
            items = controller.filesystem.list_directory("/")
            assert items[0].name == "SWAP.SYS"
            assert items[0].size == 0xFFFF * BLOCK
            assert len(items) == 28  # nothing else dropped
            with pytest.raises(ValueError, match="overrun"):
                controller.filesystem.read_file("SWAP.SYS")
        finally:
            controller.close_disk()

    def test_insane_length_word_blocks_writes_too(self, tmp_path):
        # The shifted start blocks push every free run past the device end;
        # the allocator must refuse (OSError) rather than scribble blocks,
        # leaving the image byte-identical.
        def mutate(data):
            struct.pack_into("<H", data, ENTRY0_OFF + 8, 0xFFFF)

        path = _patched_v0501(tmp_path, mutate)
        before = path.read_bytes()
        controller = _open(path)
        try:
            with pytest.raises(OSError):
                controller.filesystem.write_file("NEW.DAT", b"x" * BLOCK)
            controller.flush()
        finally:
            controller.close_disk()
        assert path.read_bytes() == before

    def test_broken_segment_link_truncates_chain(self, tmp_path):
        # Segment 1's link word -> 3 (header-valid: <= total_segments 4) but
        # segment 3 is 0x88 fill garbage: its first status word has the EOS
        # bit, and its own link (0x8888) is out of range. The chain walk must
        # truncate with no hang and no raise.
        def mutate(data):
            assert struct.unpack_from("<H", data, SEG1_OFF)[0] == 4
            struct.pack_into("<H", data, SEG1_OFF + 2, 3)

        controller = _open(_patched_v0501(tmp_path, mutate))
        try:
            items = controller.filesystem.list_directory("/")
            assert len(items) == 28  # segment 1 intact, garbage chain dropped
            assert items[0].name == "SWAP.SYS"
        finally:
            controller.close_disk()

    def test_self_linked_segment_chain_terminates(self, tmp_path):
        # Link word -> 1 (self-cycle): the visited-set guard must stop the
        # walk after one pass instead of looping forever.
        def mutate(data):
            struct.pack_into("<H", data, SEG1_OFF + 2, 1)

        controller = _open(_patched_v0501(tmp_path, mutate))
        try:
            items = controller.filesystem.list_directory("/")
            assert len(items) == 28
        finally:
            controller.close_disk()

    def test_calendar_invalid_date_falls_back_to_epoch(self, tmp_path):
        # Date word -> Feb 30 1984: field-wise in range, calendar-invalid.
        # The file must stay listed with the epoch fallback datetime.
        def mutate(data):
            struct.pack_into(
                "<H", data, ENTRY0_OFF + 12, (2 << 10) | (30 << 5) | (1984 - 1972)
            )

        controller = _open(_patched_v0501(tmp_path, mutate))
        try:
            items = controller.filesystem.list_directory("/")
            assert items[0].name == "SWAP.SYS"
            assert items[0].datetime == NO_DATE
        finally:
            controller.close_disk()


class TestConfigPlumbing:
    def test_configs_match_delegates_to_rt11config(self):
        a = RT11Config(view="rx01", total_blocks=494)
        b = RT11Config(view="rx01", total_blocks=494, volume_id="X", owner="Y")
        assert RT11Filesystem.configs_match(a, b)
        assert not RT11Filesystem.configs_match(
            a, RT11Config(view="logical", total_blocks=494)
        )
        assert not RT11Filesystem.configs_match(a, None)
        assert not RT11Filesystem.configs_match(a, object())

    def test_create_config_from_params_infers_view_from_geometry(self):
        cases = [
            ("rt11_rx01", "rx01", 494),
            ("rt11_rx02", "rx02", 988),
            ("rt11_rx50", "rx50", 800),
            ("rt11_logical_800", "logical", 800),
        ]
        for profile_name, view, blocks in cases:
            pf = RT11_FORMATS[profile_name].physical_format
            cfg = RT11Filesystem.create_config_from_params({}, pf)
            assert isinstance(cfg, RT11Config), profile_name
            assert (cfg.view, cfg.total_blocks) == (view, blocks), profile_name

    def test_create_config_rejects_unusable_sector_size(self):
        from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

        pf = PhysicalFormat(
            cylinders=77,
            heads=1,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=1024,
            track_formats=[TrackFormat(0, 76, 0, 0, 8, "MFM", 500, 1)],
        )
        assert RT11Filesystem.create_config_from_params({}, pf) is None


def _rx02_geometry_fs(path_or_bytes, tmp_path=None, config=None):
    """Open bytes/path under the rx02 profile geometry with an optional
    explicit config (the constructor-injection path the controller uses)."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        path = tmp_path / "volume.img"
        path.write_bytes(bytes(path_or_bytes))
    else:
        path = path_or_bytes
    driver = IMGImageDriver(str(path))
    disk = Disk(driver)
    disk.set_geometry(copy.deepcopy(RT11_FORMATS["rt11_rx02"].physical_format))
    return RT11Filesystem(disk, config=config)


class TestExplicitViewContract:
    """An explicit config view is honored when it scores >= threshold and
    falls back to full content resolution otherwise (the override used to be
    silently discarded -- dead plumbing)."""

    def _dual_view_volume(self):
        """One 512,512-byte container holding a valid RT-11 directory under
        BOTH the rx02 and the logical view: physical track 0 (bytes 0-6655,
        where the logical view's home block and segment live) is invisible
        to the rx02 view, so the two structures never collide. Both views
        score 90; only an explicit view can disambiguate deterministically.
        """
        data = bytearray(VIEW_GEOMETRY["rx02"].image_size)

        def write_block(view, block, payload):
            if view == "logical":
                data[block * BLOCK : (block + 1) * BLOCK] = payload
                return
            geom = VIEW_GEOMETRY[view]
            for index, (cyl, _head, sec) in enumerate(
                logical_block_to_chs(view, block)
            ):
                pos = (cyl * geom.sectors_per_track + sec) * geom.bytes_per_sector
                chunk = payload[
                    index * geom.bytes_per_sector : (index + 1) * geom.bytes_per_sector
                ]
                data[pos : pos + geom.bytes_per_sector] = chunk

        entry = struct.pack(
            "<7H", E_PERM, rad50_encode("DUA"), 0, rad50_encode("TST"), 5, 0, 0
        )
        segment = struct.pack("<5H", 1, 0, 1, 0, 8) + entry
        segment += struct.pack("<H", E_EOS)
        segment += b"\x00" * (SEGMENT_SIZE - len(segment))
        for view in ("rx02", "logical"):
            write_block(view, 1, encode_home_block(volume_id=view.upper()))
            write_block(view, 6, segment[:BLOCK])
            write_block(view, 7, segment[BLOCK:])
        return bytes(data)

    def test_explicit_view_wins_on_ambiguous_volume(self, tmp_path):
        volume = self._dual_view_volume()
        for view, blocks in (("rx02", 988), ("logical", 1001)):
            fs = _rx02_geometry_fs(
                volume, tmp_path, config=RT11Config(view=view, total_blocks=blocks)
            )
            cfg = fs.get_specific_config()
            assert (cfg.view, cfg.total_blocks) == (view, blocks), view
            assert fs.get_volume_label() == view.upper()

    def test_without_explicit_view_physical_candidate_wins_tie(self, tmp_path):
        # Documented tie-break: physical candidate first.
        fs = _rx02_geometry_fs(self._dual_view_volume(), tmp_path)
        assert fs.get_specific_config().view == "rx02"

    def test_explicit_view_below_threshold_falls_back(self):
        # The raw .DSK twin scores ~0 under "logical"; an explicit logical
        # view must NOT be trusted blindly -- resolution falls back and
        # finds rx02 from the contents.
        fs = _rx02_geometry_fs(
            V0501_DSK, config=RT11Config(view="logical", total_blocks=1001)
        )
        cfg = fs.get_specific_config()
        assert (cfg.view, cfg.total_blocks) == ("rx02", 988)

    def test_explicit_matching_view_adopted_on_real_pair(self):
        fs = _rx02_geometry_fs(
            V0501_IMG, config=RT11Config(view="logical", total_blocks=1001)
        )
        assert fs.get_specific_config().view == "logical"
        fs = _rx02_geometry_fs(
            V0501_DSK, config=RT11Config(view="rx02", total_blocks=988)
        )
        assert fs.get_specific_config().view == "rx02"


# ---------------------------------------------------------------------------
# Task 4: write path -- format, first-fit allocation, segment splitting,
# delete. All on fresh tmp images or tmp COPIES of the committed resources
# (the committed images are read-only oracles).
# ---------------------------------------------------------------------------

# profile -> (resolved view, container blocks, directory segments)
FORMAT_CASES = {
    "rt11_rx01": ("rx01", 494, 1),
    "rt11_rx02": ("rx02", 988, 4),
    "rt11_rx50": ("rx50", 800, 4),
    "rt11_logical_494": ("logical", 494, 1),
    "rt11_logical_500": ("logical", 500, 1),
    "rt11_logical_800": ("logical", 800, 4),
    "rt11_logical_988": ("logical", 988, 4),
}


def _format_new(tmp_path, profile_name, label="TEST", name="vol"):
    """Create + format a new image; returns (controller, image path)."""
    img = tmp_path / f"{name}.img"
    controller = DiskController()
    assert controller.format_disk_media(
        format_name=profile_name,
        volume_label=label,
        file_path=str(img),
        disk_type="IMG",
    ), f"create/format failed for {profile_name}"
    return controller, img


def _copy_resource(tmp_path, src):
    dst = tmp_path / src.name
    dst.write_bytes(src.read_bytes())
    return dst


def _parse_linked_segments(data, view, dir_start=6):
    """Raw [(segment_number, ParsedSegment), ...] walk in linked order."""
    out = []
    number = 1
    seen = set()
    while number and number not in seen:
        seen.add(number)
        first = dir_start + (number - 1) * 2
        seg = parse_segment(
            _read_block(data, view, first) + _read_block(data, view, first + 1)
        )
        out.append((number, seg))
        number = seg.header.next_segment
    return out


class TestFormatFS:
    @pytest.mark.parametrize(
        "profile", sorted(FORMAT_CASES), ids=lambda p: p.removeprefix("rt11_")
    )
    def test_fresh_format_layout_and_reopen(self, tmp_path, profile):
        view, total, segments = FORMAT_CASES[profile]
        data_start = 6 + 2 * segments
        controller, img = _format_new(tmp_path, profile, label="FRESH")
        try:
            assert isinstance(controller.filesystem, RT11Filesystem)
            assert controller.filesystem.list_directory("/") == []
            free, fs_total = controller.filesystem.get_free_space()
            # The whole data area is free: device - dir_start - 2*segments.
            assert free == (total - data_start) * BLOCK
            assert fs_total == (total - data_start) * BLOCK
            cfg = controller.filesystem.get_specific_config()
            assert (cfg.view, cfg.total_blocks) == (view, total)
        finally:
            controller.close_disk()

        raw = img.read_bytes()
        assert _read_block(raw, view, 0) == b"\x00" * BLOCK  # boot block
        home = parse_home_block(_read_block(raw, view, 1))
        assert home.volume_id == "FRESH       "
        assert home.system_id == "DECRT11A    "
        assert home.dir_start == 6
        assert home.pack_cluster_size == 1
        assert home.checksum_stored == home.checksum_computed
        chain = _parse_linked_segments(raw, view)
        assert [number for number, _seg in chain] == [1]
        seg = chain[0][1]
        assert seg.header.total_segments == segments  # INIT default for size
        assert seg.header.highest_in_use == 1
        assert seg.header.extra_bytes == 0
        assert seg.header.data_start_block == data_start
        assert seg.eos_found
        assert len(seg.entries) == 1
        empty = seg.entries[0]
        assert empty.status == E_MPTY
        assert (empty.start_block, empty.length) == (data_start, total - data_start)

        # Reopen by auto-detection: the formatted volume must detect as
        # RT-11 with the view that was written.
        verifier = _open(img)
        try:
            assert isinstance(verifier.filesystem, RT11Filesystem)
            cfg = verifier.filesystem.get_specific_config()
            assert (cfg.view, cfg.total_blocks) == (view, total)
            assert verifier.filesystem.get_volume_label() == "FRESH"
            assert verifier.filesystem.list_directory("/") == []
        finally:
            verifier.close_disk()


class TestWriteReadRoundTrip:
    # Zero-length entries are valid RT-11: V&FF manual sec 1.1.3 -- after
    # .CLOSE "the length of the file is the actual size of the data that
    # was written" (zero when nothing was), Figure 1-11 shows a legitimate
    # 0-block empty area, and the length word (5th entry word, sec 1.1.2.2)
    # has no minimum. So an empty write creates a 0-block permanent entry.
    SIZES = [0, 1, BLOCK, BLOCK + 700, 4 * BLOCK]

    @pytest.mark.parametrize("profile", ["rt11_rx02", "rt11_logical_494"])
    def test_sizes_round_trip_and_persist(self, tmp_path, profile):
        controller, img = _format_new(tmp_path, profile)
        names = []
        try:
            fs = controller.filesystem
            for index, size in enumerate(self.SIZES):
                name = f"RT{index}.DAT"
                payload = bytes((index + j) % 251 for j in range(size))
                fs.write_file(name, payload)
                names.append((name, payload))
            for name, payload in names:
                blocks = (len(payload) + BLOCK - 1) // BLOCK
                data = fs.read_file(name)
                assert len(data) == blocks * BLOCK, name
                assert data[: len(payload)] == payload, name
                assert data[len(payload) :] == b"\x00" * (len(data) - len(payload))
            listing = {i.name: i for i in fs.list_directory("/")}
            assert set(listing) == {name for name, _p in names}
            assert listing["RT0.DAT"].size == 0
            assert fs.read_file("RT0.DAT") == b""
            controller.flush()
        finally:
            controller.close_disk()

        verifier = _open(img)
        try:
            for name, payload in names:
                data = verifier.filesystem.read_file(name)
                assert data[: len(payload)] == payload, name
        finally:
            verifier.close_disk()

    def test_date_stamped_today(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            fs.write_file("TODAY.DAT", b"dated")
            (item,) = fs.list_directory("/")
            today = datetime.date.today()
            assert item.datetime.date() == today
            # And the on-disk word really is the RT-11 encoding of today.
            raw_word = struct.unpack_from(
                "<H",
                fs._read_block(6),
                10 + 12,  # first entry's date word
            )[0]
            assert raw_word == encode_date_word(today.year, today.month, today.day)
        finally:
            controller.close_disk()

    def test_write_rejects_non_rt11_volume(self):
        profile = RT11_FORMATS["rt11_rx01"]
        driver = IMGImageDriver(str(CPM_8IN_IMG))
        disk = Disk(driver)
        disk.set_geometry(copy.deepcopy(profile.physical_format))
        fs = RT11Filesystem(disk)
        with pytest.raises(ValueError):
            fs.write_file("X.DAT", b"x")


class TestWriteNameValidation:
    def test_lowercase_is_uppercased(self, tmp_path):
        # CP/M precedent: case is normalized, structure is validated
        # strictly (no silent truncation).
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            fs.write_file("hello.dat", b"hi")
            assert [i.name for i in fs.list_directory("/")] == ["HELLO.DAT"]
            assert fs.read_file("hello.dat")[:2] == b"hi"
        finally:
            controller.close_disk()

    def test_rad50_specials_accepted(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            for name in ("A$B%C0.99$", "X", "NOEXT", "/SLASH.DAT"):
                fs.write_file(name, b"ok")
            names = {i.name for i in fs.list_directory("/")}
            assert names == {"A$B%C0.99$", "X", "NOEXT", "SLASH.DAT"}
        finally:
            controller.close_disk()

    @pytest.mark.parametrize(
        "bad",
        [
            "TOOLONG7.DAT",  # base > 6
            "GOOD.LONG",  # type > 3
            "TWO.DO.TS",  # more than one dot
            ".DAT",  # empty base
            "",  # empty
            "BAD-1.DAT",  # '-' not in RAD50
            "SP CE.DAT",  # embedded space
            "UNIé.DAT",  # non-ASCII
        ],
    )
    def test_invalid_names_rejected_without_mutation(self, tmp_path, bad):
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            controller.flush()
            before = img.read_bytes()
            with pytest.raises(ValueError):
                controller.filesystem.write_file(bad, b"x")
            controller.flush()
            assert img.read_bytes() == before
        finally:
            controller.close_disk()


class TestFirstFitAllocation:
    def _carve_two_runs(self, fs):
        """A(4) B(1) C(4) D(1) then delete A and C -> two free runs of 4
        at blocks 8 and 13 ahead of the big trailing run."""
        fs.write_file("A.DAT", b"A" * (4 * BLOCK))
        fs.write_file("B.DAT", b"B" * BLOCK)
        fs.write_file("C.DAT", b"C" * (4 * BLOCK))
        fs.write_file("D.DAT", b"D" * BLOCK)
        assert fs.get_file_allocation_units("A.DAT") == [8, 9, 10, 11]
        assert fs.get_file_allocation_units("C.DAT") == [13, 14, 15, 16]
        fs.delete("A.DAT")
        fs.delete("C.DAT")

    def test_first_fit_takes_the_first_run(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            self._carve_two_runs(fs)
            # E fits BOTH free runs -> must take the FIRST (block 8).
            fs.write_file("E.DAT", b"E" * (2 * BLOCK))
            assert fs.get_file_allocation_units("E.DAT") == [8, 9]
        finally:
            controller.close_disk()

    def test_exact_fit_consumes_empty_entirely(self, tmp_path):
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            self._carve_two_runs(fs)
            fs.write_file("E.DAT", b"E" * (2 * BLOCK))
            # F fits the 2-block residual at block 10 exactly.
            fs.write_file("F.DAT", b"F" * (2 * BLOCK))
            assert fs.get_file_allocation_units("F.DAT") == [10, 11]
            # G no longer fits ahead of C's old run.
            fs.write_file("G.DAT", b"G" * (4 * BLOCK))
            assert fs.get_file_allocation_units("G.DAT") == [13, 14, 15, 16]
            controller.flush()
        finally:
            controller.close_disk()

        # Exact fits replace the empty entry instead of leaving 0-block
        # empties behind.
        ((_n, seg),) = _parse_linked_segments(img.read_bytes(), "logical")
        assert [(e.status, e.length) for e in seg.entries] == [
            (E_PERM, 2),  # E at 8
            (E_PERM, 2),  # F at 10
            (E_PERM, 1),  # B at 12
            (E_PERM, 4),  # G at 13
            (E_PERM, 1),  # D at 17
            (E_MPTY, 494 - 18),
        ]

    def test_no_single_run_large_enough_raises_oserror(self, tmp_path):
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            self._carve_two_runs(fs)
            fs.write_file("HUGE.DAT", b"H" * ((494 - 18) * BLOCK))  # big run
            controller.flush()
            before = img.read_bytes()
            # 8 free blocks total but no contiguous run >= 5 (authentic
            # RT-11: contiguous files, no coalescing without SQUEEZE).
            with pytest.raises(OSError):
                fs.write_file("NOFIT.DAT", b"x" * (5 * BLOCK))
            controller.flush()
            assert img.read_bytes() == before  # validate-before-mutate
        finally:
            controller.close_disk()


class TestReplaceOnSameName:
    def test_replace_deletes_old_and_creates_new(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            fs.write_file("FOO.DAT", b"v1" * 600)  # 3 blocks at 8
            free_after_v1 = fs.get_free_space()[0]
            fs.write_file("FOO.DAT", b"v2")  # 1 block
            items = fs.list_directory("/")
            assert [i.name for i in items] == ["FOO.DAT"]
            assert items[0].size == BLOCK
            assert fs.read_file("FOO.DAT")[:2] == b"v2"
            # Old 3-block run freed, new 1-block run used.
            assert fs.get_free_space()[0] == free_after_v1 + 2 * BLOCK
            # Allocate-before-free (.ENTER ordering): the replacement is
            # planned while the old entry at 8-10 is still live, so
            # first-fit lands AFTER the old run, never inside it.
            assert fs.get_file_allocation_units("FOO.DAT") == [11]
        finally:
            controller.close_disk()

    def test_failed_replace_leaves_old_file_intact(self, tmp_path):
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            fs.write_file("KEEP.DAT", b"precious " * 100)  # 2 blocks
            fs.write_file("BIG.DAT", b"B" * ((494 - 10) * BLOCK))  # fill rest
            controller.flush()
            before = img.read_bytes()
            # KEEP's old blocks are not even a candidate (allocate-before-
            # free), and no run can hold 5 blocks anyway; the plan must
            # fail BEFORE any byte is written.
            with pytest.raises(OSError):
                fs.write_file("KEEP.DAT", b"x" * (5 * BLOCK))
            controller.flush()
            assert img.read_bytes() == before
            assert fs.read_file("KEEP.DAT")[:9] == b"precious "
        finally:
            controller.close_disk()

    def test_replacing_protected_file_is_refused(self, tmp_path):
        path = _copy_resource(tmp_path, V0501_IMG)
        controller = _open(path)
        try:
            before = controller.filesystem.read_file("SWAP.SYS")
            with pytest.raises(PermissionError):
                controller.filesystem.write_file("SWAP.SYS", b"clobber")
            assert controller.filesystem.read_file("SWAP.SYS") == before
        finally:
            controller.close_disk()

    def test_replace_allocates_outside_old_run(self, tmp_path):
        # Authentic .ENTER ordering: the new copy is allocated while the
        # old entry is still live, so the old run's blocks are never
        # allocation candidates -- a mid-write I/O failure can no longer
        # corrupt the old file (real RT-11 .ENTERs a tentative file in NEW
        # space and deletes the old at close).
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            fs.write_file("FILE.DAT", b"v1" * BLOCK)  # 2 blocks
            (item,) = fs.list_directory("/")
            old_start = item.extra_data["start_block"]
            old_run = set(range(old_start, old_start + 2))
            # Ample space elsewhere: the replacement must land OUTSIDE the
            # old run, not first-fit back into it.
            fs.write_file("FILE.DAT", b"v2" * BLOCK)  # 2 blocks
            (item,) = fs.list_directory("/")
            assert item.extra_data["start_block"] != old_start
            new_units = fs.get_file_allocation_units("FILE.DAT")
            assert not set(new_units) & old_run
            assert fs.read_file("FILE.DAT") == b"v2" * BLOCK
        finally:
            controller.close_disk()

    def test_replace_when_freed_run_precedes_old_entry(self, tmp_path):
        # Pins identity-based old-entry removal (_model_mark_empty_entry)
        # on replace. When a freed run sits EARLIER in the directory than
        # the file being replaced, first-fit splices the NEW entry BEFORE
        # the old one -- after allocation TWO live entries share the name.
        # A name-based relocation ("find first live B.DAT, mark it empty")
        # would mark the NEW entry, leave the OLD one live, and the write
        # would "succeed" while B.DAT silently reads back the old content.
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        v1 = bytes((i * 3) % 256 for i in range(2 * BLOCK))
        v2 = bytes((i * 5 + 1) % 256 for i in range(2 * BLOCK))
        try:
            fs = controller.filesystem
            fs.write_file("A.DAT", b"A" * (2 * BLOCK))  # blocks 8-9
            fs.write_file("B.DAT", v1)  # blocks 10-11
            assert fs.get_file_allocation_units("B.DAT") == [10, 11]
            fs.delete("A.DAT")  # frees 8-9, EARLIER than B's entry
            fs.write_file("B.DAT", v2)  # first-fit -> A's freed run
            # The data-loss signal: B must read back the NEW content.
            assert fs.read_file("B.DAT") == v2
            # The new copy landed in A's old run, ahead of the old entry.
            assert fs.get_file_allocation_units("B.DAT") == [8, 9]
            # Old B run freed: only the new 2-block copy is allocated.
            assert fs.get_free_space()[0] == (494 - 8 - 2) * BLOCK
            controller.flush()
        finally:
            controller.close_disk()

        # Raw proof: exactly one live B.DAT remains, it is the EARLIER
        # entry (in A's freed run), and the old run at 10-11 is E.MPTY.
        ((_n, seg),) = _parse_linked_segments(img.read_bytes(), "logical")
        live_b = [
            e
            for e in seg.entries
            if not e.is_empty
            and (e.name or "").strip() == "B"
            and (e.file_type or "").strip() == "DAT"
        ]
        assert len(live_b) == 1
        assert [(e.status, e.length, e.start_block) for e in seg.entries] == [
            (E_PERM, 2, 8),  # NEW B.DAT in A's freed run
            (E_MPTY, 2, 10),  # OLD B run, freed by the replace
            (E_MPTY, 494 - 12, 12),
        ]

    def test_replace_needs_room_for_old_and_new_together(self, tmp_path):
        # 486 data blocks; OLD takes 480, leaving a 6-block trailing run.
        # 100 new blocks would fit ONLY if OLD's run were freed first --
        # authentic .ENTER needs old and new to coexist, so the plan must
        # fail with the whole image byte-identical and OLD untouched.
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            payload = bytes(255 - (i % 251) for i in range(480 * BLOCK))
            fs.write_file("OLD.DAT", payload)
            controller.flush()
            before = img.read_bytes()
            free_before = fs.get_free_space()
            with pytest.raises(OSError, match="contiguous"):
                fs.write_file("OLD.DAT", b"n" * (100 * BLOCK))
            controller.flush()
            assert img.read_bytes() == before
            assert fs.get_free_space() == free_before
            assert fs.read_file("OLD.DAT") == payload
        finally:
            controller.close_disk()

    @pytest.mark.parametrize(
        "profile,view", [("rt11_rx02", "rx02"), ("rt11_logical_988", "logical")]
    )
    def test_replace_fits_single_entry_both_views(self, tmp_path, profile, view):
        # Replace-when-it-fits end to end, through a raw physical view and
        # the logical view: exactly one live entry remains (no duplicate
        # names) and the new content persists across a reopen.
        v1 = b"first version " * 100  # 3 blocks
        v2 = bytes((i * 7) % 256 for i in range(2 * BLOCK))  # 2 blocks
        controller, img = _format_new(tmp_path, profile)
        try:
            fs = controller.filesystem
            fs.write_file("FILE.DAT", v1)
            fs.write_file("FILE.DAT", v2)
            assert [i.name for i in fs.list_directory("/")] == ["FILE.DAT"]
            assert fs.read_file("FILE.DAT") == v2
            controller.flush()
        finally:
            controller.close_disk()

        # Prove it on the raw bytes through the view in question.
        chain = _parse_linked_segments(img.read_bytes(), view)
        live = [
            entry
            for _number, seg in chain
            for entry in seg.entries
            if entry.is_permanent and not entry.is_empty
        ]
        assert len(live) == 1

        verifier = _open(img)
        try:
            assert verifier.filesystem.read_file("FILE.DAT") == v2
        finally:
            verifier.close_disk()

    @pytest.mark.parametrize("victim_index", [0, 70])
    def test_replace_into_full_segment_splits_correctly(self, tmp_path, victim_index):
        # 71 one-block files plus the trailing empty fill segment 1 to its
        # 72-entry capacity. A replace then needs one extra entry (new
        # entry + shrunken empty) and must split; with allocate-before-free
        # the old entry may have MOVED to the new segment (victim 70)
        # before it is marked E.MPTY.
        controller, _img = _format_new(tmp_path, "rt11_rx02")
        files = {}
        try:
            fs = controller.filesystem
            for index in range(71):
                name = f"F{index:03d}.DAT"
                payload = f"file {index} ".encode() * 30
                fs.write_file(name, payload)
                files[name] = payload
            victim = f"F{victim_index:03d}.DAT"
            new_payload = b"REPLACED" * (2 * BLOCK // 8)  # 2 blocks
            fs.write_file(victim, new_payload)
            files[victim] = new_payload

            listing = {i.name for i in fs.list_directory("/")}
            assert listing == set(files)  # still 71 names, no duplicates
            for name, payload in files.items():
                assert fs.read_file(name)[: len(payload)] == payload, name
            # Structural invariants after the split-during-replace.
            allocated = fs.get_allocated_units()
            assert len(allocated) == len(set(allocated))
            free_blocks = fs.get_free_space()[0] // BLOCK
            assert len(allocated) + free_blocks == 988
            flat = [
                unit for name in files for unit in fs.get_file_allocation_units(name)
            ]
            assert len(flat) == len(set(flat))
        finally:
            controller.close_disk()


class TestDelete:
    def test_delete_marks_empty_no_coalescing(self, tmp_path):
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            for name in ("A.DAT", "B.DAT", "C.DAT"):
                fs.write_file(name, name.encode())
            fs.delete("A.DAT")
            fs.delete("B.DAT")
            assert [i.name for i in fs.list_directory("/")] == ["C.DAT"]
            free, _total = fs.get_free_space()
            assert free == (494 - 8 - 1) * BLOCK
            controller.flush()
        finally:
            controller.close_disk()

        # Authentic RT-11: adjacent empties stay SEPARATE entries (only
        # SQUEEZE consolidates, and SQUEEZE is a non-goal).
        ((_n, seg),) = _parse_linked_segments(img.read_bytes(), "logical")
        assert [(e.status, e.length) for e in seg.entries] == [
            (E_MPTY, 1),
            (E_MPTY, 1),
            (E_PERM, 1),
            (E_MPTY, 494 - 11),
        ]

    def test_delete_missing_and_recursive(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem
            with pytest.raises(FileNotFoundError):
                fs.delete("GHOST.DAT")
            fs.write_file("REAL.DAT", b"x")
            assert fs.delete_recursive("REAL.DAT") is True
            assert fs.delete_recursive("REAL.DAT") is False
            assert fs.list_directory("/") == []
        finally:
            controller.close_disk()

    def test_delete_protected_refused(self, tmp_path):
        path = _copy_resource(tmp_path, V0501_IMG)
        controller = _open(path)
        try:
            with pytest.raises(PermissionError):
                controller.filesystem.delete("SWAP.SYS")
            names = {i.name for i in controller.filesystem.list_directory("/")}
            assert "SWAP.SYS" in names
        finally:
            controller.close_disk()

    def test_delete_on_real_volume_preserves_other_files(self, tmp_path):
        # Content-level no-regression on a REAL raw RX01: deleting one file
        # must leave every other file's bytes untouched.
        path = _copy_resource(tmp_path, RX01_V03B)
        controller = _open(path)
        try:
            fs = controller.filesystem
            before = {
                i.name: hashlib.sha256(fs.read_file(i.name)).hexdigest()
                for i in fs.list_directory("/")
            }
            free_before = fs.get_free_space()[0]
            fs.delete("SWAP.SYS")
            items = fs.list_directory("/")
            assert len(items) == 32
            assert "SWAP.SYS" not in {i.name for i in items}
            assert fs.get_free_space()[0] == free_before + 24 * BLOCK
            for item in items:
                data = fs.read_file(item.name)
                assert hashlib.sha256(data).hexdigest() == before[item.name]
        finally:
            controller.close_disk()

    def test_delete_real_tentative_file(self, tmp_path):
        path = _copy_resource(tmp_path, BASIC11_RX02)
        controller = _open(path)
        try:
            fs = controller.filesystem
            fs.delete("TEST.DAT")  # the genuine tentative entry
            names = {i.name for i in fs.list_directory("/")}
            assert "TEST.DAT" not in names
            assert len(names) == 121
        finally:
            controller.close_disk()


class TestTentativeRespectedByAllocator:
    def test_tentative_blocks_not_reallocated(self, tmp_path):
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            controller.filesystem.write_file("T.TMP", b"t" * (3 * BLOCK))
            controller.flush()
        finally:
            controller.close_disk()
        # Turn the permanent entry tentative (status word at block 6 + 10).
        data = bytearray(img.read_bytes())
        assert struct.unpack_from("<H", data, ENTRY0_OFF)[0] == E_PERM
        struct.pack_into("<H", data, ENTRY0_OFF, E_TENT)
        img.write_bytes(bytes(data))

        controller = _open(img)
        try:
            fs = controller.filesystem
            (tent,) = [i for i in fs.list_directory("/") if "TENT" in i.attributes]
            assert tent.name == "T.TMP"
            fs.write_file("NEW.DAT", b"n" * (2 * BLOCK))
            # Tentative runs are never allocation targets, but their blocks
            # are respected: NEW lands after T.TMP's 3 blocks at 8-10.
            assert fs.get_file_allocation_units("NEW.DAT") == [11, 12]
        finally:
            controller.close_disk()


class TestPrefixBlocks:
    def test_e_pre_attribute_and_raw_read(self, tmp_path):
        payload = b"P" * BLOCK + b"DATA" * 300
        controller, img = _format_new(tmp_path, "rt11_logical_494")
        try:
            controller.filesystem.write_file("PRE.DAT", payload)
            controller.flush()
        finally:
            controller.close_disk()
        data = bytearray(img.read_bytes())
        struct.pack_into("<H", data, ENTRY0_OFF, E_PERM | 0o20)  # set E.PRE
        img.write_bytes(bytes(data))

        controller = _open(img)
        try:
            (item,) = controller.filesystem.list_directory("/")
            assert "PRE" in item.attributes
            # Prefix blocks are part of the run and come back raw.
            blocks = (len(payload) + BLOCK - 1) // BLOCK
            data = controller.filesystem.read_file("PRE.DAT")
            assert len(data) == blocks * BLOCK
            assert data[: len(payload)] == payload
        finally:
            controller.close_disk()


class TestSegmentSplit:
    def _invariants(self, fs, total_blocks):
        allocated = fs.get_allocated_units()
        assert len(allocated) == len(set(allocated))
        free_blocks = fs.get_free_space()[0] // BLOCK
        # free + used + system covers the device exactly, no overlaps.
        assert len(allocated) + free_blocks == total_blocks
        unit_lists = [
            fs.get_file_allocation_units(i.name) for i in fs.list_directory("/")
        ]
        flat = [u for units in unit_lists for u in units]
        assert len(flat) == len(set(flat))  # no overlapping file runs

    def test_split_listing_order_and_full_recovery(self, tmp_path):
        controller, img = _format_new(tmp_path, "rt11_rx02")
        files = {}
        try:
            fs = controller.filesystem
            for index in range(75):  # > 72-entry segment capacity
                name = f"F{index:03d}.DAT"
                payload = f"file {index} ".encode() * 20
                fs.write_file(name, payload)
                files[name] = payload
            listing = fs.list_directory("/")
            assert len(listing) == 75
            for name, payload in files.items():
                assert fs.read_file(name)[: len(payload)] == payload, name
            self._invariants(fs, 988)
            controller.flush()
        finally:
            controller.close_disk()

        raw = img.read_bytes()
        chain = _parse_linked_segments(raw, "rx02")
        numbers = [number for number, _seg in chain]
        assert len(numbers) >= 2, "no split happened"
        assert numbers[0] == 1
        head = chain[0][1].header
        assert head.total_segments == 4
        assert head.highest_in_use == len(numbers)
        # DEC-style linked order: each segment's data area starts where the
        # previous one ends; entry capacity is never exceeded.
        expected_start = chain[0][1].header.data_start_block
        all_names = []
        for _number, seg in chain:
            assert seg.eos_found
            assert len(seg.entries) <= segment_max_entries(seg.header.extra_bytes)
            assert seg.header.data_start_block == expected_start
            expected_start += sum(e.length for e in seg.entries)
            all_names += [
                (e.name or "") + "." + (e.file_type or "")
                for e in seg.entries
                if e.is_permanent
            ]
        assert expected_start == 988  # runs tile the device exactly
        # Linked order preserves creation order across the split.
        assert all_names == [f"F{i:03d}".ljust(6) + ".DAT" for i in range(75)]

        # Delete everything: all space must be recoverable.
        controller = _open(img)
        try:
            fs = controller.filesystem
            for name in files:
                fs.delete(name)
            assert fs.list_directory("/") == []
            assert fs.get_free_space()[0] == (988 - 14) * BLOCK
            self._invariants(fs, 988)
            # And the volume is still writable after the churn.
            fs.write_file("AFTER.DAT", b"alive")
            assert fs.read_file("AFTER.DAT")[:5] == b"alive"
        finally:
            controller.close_disk()

    def test_directory_full_when_no_segment_available(self, tmp_path):
        # rx01 formats with ONE segment: filling it must raise a directory
        # full error (no segment to split into), leaving the disk valid.
        controller, img = _format_new(tmp_path, "rt11_rx01")
        try:
            fs = controller.filesystem
            written = 0
            with pytest.raises(OSError, match="[Dd]irectory"):
                for index in range(80):
                    fs.write_file(f"D{index:03d}.DAT", b"x")
                    written += 1
            assert written >= 69  # manual sec 1.1.4: 72 less reserved slots
            listing = fs.list_directory("/")
            assert len(listing) == written
            for item in listing:
                assert fs.read_file(item.name)[:1] == b"x"
        finally:
            controller.close_disk()


class TestChurn:
    def test_eighty_iterations_create_delete_with_invariants(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_rx50")
        rng = random.Random(56)
        shadow = {}
        try:
            fs = controller.filesystem
            for iteration in range(80):
                name = f"C{iteration:03d}.DAT"
                payload = rng.randbytes(rng.randint(0, 4 * BLOCK))
                fs.write_file(name, payload)
                shadow[name] = payload
                if len(shadow) > 12:
                    victim = rng.choice(sorted(shadow))
                    fs.delete(victim)
                    del shadow[victim]
                # Full content verification + structural invariants each
                # iteration.
                listing = {i.name: i for i in fs.list_directory("/")}
                assert set(listing) == set(shadow), f"iteration {iteration}"
                for fname, fdata in shadow.items():
                    blocks = (len(fdata) + BLOCK - 1) // BLOCK
                    back = fs.read_file(fname)
                    assert len(back) == blocks * BLOCK, fname
                    assert back[: len(fdata)] == fdata, fname
                allocated = fs.get_allocated_units()
                assert len(allocated) == len(set(allocated))
                free_blocks = fs.get_free_space()[0] // BLOCK
                assert len(allocated) + free_blocks == 800, f"iteration {iteration}"
                flat = [
                    unit
                    for fname in shadow
                    for unit in fs.get_file_allocation_units(fname)
                ]
                assert len(flat) == len(set(flat)), f"iteration {iteration}"
        finally:
            controller.close_disk()


class TestWritePairAcid:
    def test_same_content_via_rx02_and_logical_views(self, tmp_path):
        # The write-side twin of the view-resolver acid test: the same file
        # written through the raw rx02 mapping and the logical mapping must
        # read back identically from both volumes.
        payload = bytes((i * 7) % 256 for i in range(3 * BLOCK + 100))
        results = {}
        for profile in ("rt11_rx02", "rt11_logical_988"):
            controller, img = _format_new(tmp_path, profile, name=profile)
            try:
                controller.filesystem.write_file("ACID.DAT", payload)
                controller.flush()
                results[profile] = (
                    controller.filesystem.read_file("ACID.DAT"),
                    img.read_bytes(),
                )
            finally:
                controller.close_disk()
        read_rx02, raw_rx02 = results["rt11_rx02"]
        read_logical, raw_logical = results["rt11_logical_988"]
        assert read_rx02 == read_logical
        assert read_rx02[: len(payload)] == payload
        # Same logical content, different physical layouts.
        assert raw_rx02 != raw_logical

        for profile, view in (("rt11_rx02", "rx02"), ("rt11_logical_988", "logical")):
            verifier = _open(tmp_path / f"{profile}.img")
            try:
                cfg = verifier.filesystem.get_specific_config()
                assert cfg.view == view
                assert verifier.filesystem.read_file("ACID.DAT") == read_rx02
            finally:
                verifier.close_disk()


# ---------------------------------------------------------------------------
# Task 5: check(), display-info completeness, GUI smoke (offscreen).
# ---------------------------------------------------------------------------


def _two_segment_volume(tmp_path, shift=0):
    """Synthetic logical-order volume with a two-segment chain.

    Segment 1 holds one 5-block permanent file; segment 2 holds the
    trailing empty. Segment 2's data start is shifted by ``shift`` blocks
    from the true chain end: 0 = consistent, negative = overlapping runs
    (data-loss risk), positive = unreachable gap blocks (benign).
    """
    total = 494
    data_start = 6 + 2 * 2  # dir_start + 2 segments of 2 blocks
    file_length = 5
    seg1 = serialize_segment(
        ParsedSegment(
            header=SegmentHeader(
                total_segments=2,
                next_segment=2,
                highest_in_use=2,
                extra_bytes=0,
                data_start_block=data_start,
            ),
            entries=(
                DirEntry(
                    status=E_PERM,
                    name_words=(
                        rad50_encode("FIL"),
                        rad50_encode("E1"),
                        rad50_encode("DAT"),
                    ),
                    length=file_length,
                    job_channel=0,
                    date_word=0,
                    start_block=data_start,
                ),
            ),
            eos_found=True,
        )
    )
    start2 = data_start + file_length + shift
    seg2 = serialize_segment(
        ParsedSegment(
            header=SegmentHeader(
                total_segments=2,
                next_segment=0,
                highest_in_use=0,
                extra_bytes=0,
                data_start_block=start2,
            ),
            entries=(
                DirEntry(
                    status=E_MPTY,
                    name_words=(0, 0, 0),
                    length=total - start2,
                    job_channel=0,
                    date_word=0,
                    start_block=start2,
                ),
            ),
            eos_found=True,
        )
    )
    data = bytearray(total * BLOCK)
    data[1 * BLOCK : 2 * BLOCK] = encode_home_block(volume_id="CHKVOL")
    data[6 * BLOCK : 8 * BLOCK] = seg1
    data[8 * BLOCK : 10 * BLOCK] = seg2
    path = tmp_path / f"twoseg_{shift}.img"
    path.write_bytes(bytes(data))
    return path


class TestCheck:
    @pytest.mark.parametrize(
        "path",
        [RX01_V03B, V0501_DSK, V0501_IMG, BASIC11_RX02],
        ids=lambda p: p.name,
    )
    def test_committed_real_images_pass(self, path):
        controller = _open(path)
        try:
            assert controller.filesystem.check() is True
        finally:
            controller.close_disk()

    def test_fresh_format_and_churn_pass(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_rx02")
        try:
            fs = controller.filesystem
            assert fs.check() is True
            for index in range(6):
                fs.write_file(f"W{index}.DAT", bytes(7) * index)
            fs.delete("W2.DAT")
            fs.delete("W4.DAT")
            assert fs.check() is True
        finally:
            controller.close_disk()

    def test_consistent_two_segment_chain_passes(self, tmp_path):
        controller = _open(_two_segment_volume(tmp_path, shift=0))
        try:
            assert controller.filesystem.check() is True
        finally:
            controller.close_disk()

    def test_overlapping_runs_fail(self, tmp_path):
        # Segment 2's data start lands BEFORE segment 1's chain end: the
        # trailing empty overlaps FILE1.DAT's run -- data-loss risk.
        controller = _open(_two_segment_volume(tmp_path, shift=-2))
        try:
            assert controller.filesystem.check() is False
        finally:
            controller.close_disk()

    def test_gap_blocks_warn_only(self, tmp_path, caplog):
        # A higher-than-expected data start only strands blocks (lost
        # space, no overlap): finding logged, verdict still True.
        controller = _open(_two_segment_volume(tmp_path, shift=2))
        try:
            with caplog.at_level(logging.WARNING):
                assert controller.filesystem.check() is True
            assert "unreachable block" in caplog.text
        finally:
            controller.close_disk()

    def test_missing_eos_marker_fails(self, tmp_path):
        # Zero segment 2's end-of-segment word: the entry list can no
        # longer be trusted (parse runs to the structural end).
        path = _two_segment_volume(tmp_path, shift=0)
        data = bytearray(path.read_bytes())
        eos_off = 8 * BLOCK + 10 + 14  # segment 2: header + one entry
        assert struct.unpack_from("<H", data, eos_off)[0] == E_EOS
        struct.pack_into("<H", data, eos_off, 0)
        path.write_bytes(bytes(data))
        controller = _open(path)
        try:
            assert controller.filesystem.check() is False
        finally:
            controller.close_disk()

    @pytest.mark.parametrize(
        "segment_block",
        [8, 6],
        ids=["seg2_links_back_to_seg1", "seg1_links_to_itself"],
    )
    def test_cyclic_chain_fails(self, tmp_path, caplog, segment_block):
        # Re-point a segment's next-segment link back at segment 1: the
        # chain never terminates (a real RT-11 DIR would loop forever) --
        # severe structural corruption, not a clean truncation.
        path = _two_segment_volume(tmp_path, shift=0)
        data = bytearray(path.read_bytes())
        struct.pack_into("<H", data, segment_block * BLOCK + 2, 1)
        path.write_bytes(bytes(data))
        controller = _open(path)
        try:
            with caplog.at_level(logging.WARNING):
                assert controller.filesystem.check() is False
            assert "does not terminate" in caplog.text
        finally:
            controller.close_disk()

    def test_device_overrun_fails_without_raising(self, tmp_path):
        # The insane-length corruption from TestSyntheticCorruption: the
        # runs overrun the device; check() must report, not raise.
        def mutate(data):
            struct.pack_into("<H", data, ENTRY0_OFF + 8, 0xFFFF)

        controller = _open(_patched_v0501(tmp_path, mutate))
        try:
            assert controller.filesystem.check() is False
        finally:
            controller.close_disk()

    def test_non_rt11_volume_fails_without_raising(self):
        profile = RT11_FORMATS["rt11_rx01"]
        driver = IMGImageDriver(str(CPM_8IN_IMG))
        disk = Disk(driver)
        disk.set_geometry(copy.deepcopy(profile.physical_format))
        assert RT11Filesystem(disk).check() is False


class TestDisplayInfoCompleteness:
    def test_basic11_counts_tentative_separately(self):
        controller = _open(BASIC11_RX02)
        try:
            info = controller.filesystem.get_display_info()
            assert info["View"] == "rx02 (physical sector order)"
            assert info["Files"] == "121"  # permanent only, like DEC DIR
            assert info["Tentative Files"] == "1"  # TEST.DAT
        finally:
            controller.close_disk()

    def test_logical_view_label(self):
        controller = _open(V0501_IMG)
        try:
            info = controller.filesystem.get_display_info()
            assert info["View"] == "logical (block order)"
        finally:
            controller.close_disk()

    def test_fragmentation_counts_interior_empties_only(self, tmp_path):
        controller, _img = _format_new(tmp_path, "rt11_logical_494")
        try:
            fs = controller.filesystem

            def fragmentation():
                return fs.get_display_info()["Fragmentation"]

            for name in ("A.DAT", "B.DAT", "C.DAT"):
                fs.write_file(name, name.encode())
            assert fragmentation() == "0 free run(s) between files"
            fs.delete("B.DAT")
            assert fragmentation() == "1 free run(s) between files"
            # Adjacent empties stay separate entries (no coalescing): each
            # one is a fragment costing a directory slot.
            fs.delete("A.DAT")
            assert fragmentation() == "2 free run(s) between files"
            # With the last live entry gone everything is trailing free
            # space again.
            fs.delete("C.DAT")
            assert fragmentation() == "0 free run(s) between files"
        finally:
            controller.close_disk()


# ---------------------------------------------------------------------------
# GUI smoke (offscreen): the committed RX01 through the real GUI stack, and
# write+delete through the FileManager paths on a fresh formatted volume.
# Style follows test_55_td0's TestTD0GuiSmoke.
# ---------------------------------------------------------------------------


class TestRT11GuiSmoke:
    def _disk_manager(self, controller, **panel_labels):
        from fatfloppy.gui.managers.disk_manager import DiskManager

        manager = DiskManager.__new__(DiskManager)
        manager.logger = logging.getLogger("test_rt11_gui_smoke")
        manager.parent = SimpleNamespace(controller=controller, **panel_labels)
        return manager

    def test_detected_format_panel_names_rt11_profile(self):
        controller = _open(RX01_V03B)
        try:
            label = MagicMock()
            manager = self._disk_manager(controller, detected_format_info=label)
            manager.update_detected_format_info()
            assert label.setText.called
            text = label.setText.call_args[0][0]
            assert "rt11_rx01" in text
            assert "Container: IMG" in text
        finally:
            controller.close_disk()

    def test_listing_populates_with_real_dates(self):
        # RT-11's real per-file timestamps are a feature headline: the GUI
        # tree must carry them, not a placeholder epoch.
        from fatfloppy.gui.main_window import FileBrowserApp

        controller = _open(RX01_V03B)
        try:
            fake = MagicMock()
            fake.controller = controller
            root = FileBrowserApp._build_fs_tree(fake)
            assert root is not None
            assert len(root.children) == 33
            by_name = {child.name: child for child in root.children}
            assert by_name["SWAP.SYS"].modified == "1979-03-27 00:00:00"
            assert by_name["SWAP.SYS"].size == 24 * BLOCK
            assert all(
                child.modified.startswith("1979-03-27") for child in root.children
            )
        finally:
            controller.close_disk()

    def test_space_info_and_disk_map_render(self):
        from PyQt6.QtGui import QColor, QFont
        from PyQt6.QtWidgets import QApplication, QWidget

        from fatfloppy.gui.disk_map import DiskMapView

        app = QApplication.instance() or QApplication([])  # noqa: F841
        controller = _open(RX01_V03B)
        try:
            manager = self._disk_manager(controller)
            manager.busy_units = []
            manager.free_space = 0
            manager.total_space = 0
            manager.update_space_info()
            assert len(manager.busy_units) == 451  # blocks 0-450 allocated
            assert manager.free_space == 43
            assert manager.total_space == 480

            view = DiskMapView(QWidget())
            view.draw_disk_map(
                controller=controller,
                current_head=0,
                _busy_units=manager.busy_units,
                free_space=manager.free_space,
                total_space=manager.total_space,
                app_font=QFont(),
                text_color=QColor("black"),
            )
        finally:
            controller.close_disk()

    def test_write_and_delete_through_file_manager(self, tmp_path, monkeypatch):
        from PyQt6.QtCore import QObject
        from PyQt6.QtWidgets import QMessageBox

        from fatfloppy.gui.managers.file_manager import FileManager

        controller, _img = _format_new(tmp_path, "rt11_rx02", label="GUI")
        try:
            manager = FileManager.__new__(FileManager)
            QObject.__init__(manager)  # bind signals without a QMainWindow
            manager.logger = logging.getLogger("test_rt11_gui_smoke")
            parent = SimpleNamespace(
                controller=controller,
                current_node=object(),
                current_path="/",
                _build_full_path=lambda name: "/" + name,
            )
            manager.parent = parent

            warnings = []
            monkeypatch.setattr(
                QMessageBox,
                "warning",
                staticmethod(lambda *args, **_k: warnings.append(args)),
            )

            # Auto-named import: the host name needs the RT-11 name policy
            # ("hello world.txt" is not 6.3 and holds non-RAD50 chars).
            host = tmp_path / "hello world.txt"
            host.write_bytes(b"payload from host")
            manager.import_multiple_paths([str(host)], "/", auto_name=True)
            assert not warnings, f"import failed: {warnings}"
            listing = controller.list_directory("/")
            assert [item["name"] for item in listing] == ["HELLO$.TXT"]
            assert controller.read_file("/HELLO$.TXT")[:17] == b"payload from host"

            # Delete through the manager path (confirmation auto-accepted).
            monkeypatch.setattr(
                QMessageBox,
                "question",
                staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Yes),
            )
            item = SimpleNamespace(
                node=SimpleNamespace(name="HELLO$.TXT", is_dir=False)
            )
            parent.file_list = SimpleNamespace(selectedItems=lambda: [item])
            manager.delete_selected_items()
            assert not warnings, f"delete failed: {warnings}"
            assert controller.list_directory("/") == []
        finally:
            controller.close_disk()
