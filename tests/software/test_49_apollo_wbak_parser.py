# tests/software/test_49_apollo_wbak_parser.py
"""Apollo wbak parser tests: L1 framing, labels, records, catalog."""

import hashlib
import logging
import random
import struct
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fatfloppy.core.apollo_wbak import (
    AnsiLabel,
    ContinuationSpec,
    FrameParser,
    StreamError,
    WbakEntry,
    WbakTree,
    apollo_time_to_datetime,
    build_catalog,
    decode_wbak_name,
    expected_continuation,
    stitch_tree,
)

SEC = 1024
START = 0x800
IMAGE_SIZE = 1_261_568

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"


def seg(flags: int, payload: bytes) -> bytes:
    return (
        struct.pack(">HI", flags, len(payload))
        + payload
        + struct.pack(">IH", len(payload), flags)
    )


def tapemark() -> bytes:
    return seg(4, b"")


def filler_to_boundary(pos: int) -> bytes:
    pad = (-pos) % SEC
    # The real writer only ever pads from even positions; for synthetic odd
    # positions one zero byte completes the run so the builder terminates.
    return b"\x00\x06" * (pad // 2) + b"\x00" * (pad % 2)


def block_payload(seq: int, uid: bytes) -> bytes:
    """A canonical 8192-byte wbak data block payload.

    Layout: u32BE seq, u64BE backup uid, u16BE bh_size, then a SUB (9/1)
    record header so the default classifier accepts it as the first block
    of a section (chain establishment requires the structural header).
    """
    header = struct.pack(">I", seq) + uid + struct.pack(">HHHH", 8192, 9, 20, 1)
    body = bytes((seq + i) % 256 for i in range(8192 - len(header)))
    return header + body


def chunks_of(payload: bytes) -> list:
    """The writer's canonical segment chunking: 1012-byte sector fills."""
    return [payload[i : i + 1012] for i in range(0, len(payload), 1012)]


def accept_all(_parser, _payload, _head_len=None) -> str:
    """Permissive classifier for content-agnostic framing tests."""
    return "data"


class StreamBuilder:
    """Builds a synthetic floppy byte image with correct L1 framing."""

    def __init__(self):
        self.buf = bytearray(b"APOLLO\x00\x01")
        self.buf += bytes(START - len(self.buf))  # PV label + sector 1

    def pos(self) -> int:
        return len(self.buf)

    def add_record(self, payload: bytes) -> None:
        """Splits payload into segments that never cross sector boundaries."""
        remaining = payload
        first = True
        while True:
            room = SEC - (self.pos() % SEC)
            if room < 13:  # cannot fit a minimal segment
                self.buf += filler_to_boundary(self.pos())
                continue
            chunk_max = room - 12
            chunk, remaining = remaining[:chunk_max], remaining[chunk_max:]
            if first and not remaining:
                self.buf += seg(0, chunk)
                return
            if first:
                self.buf += seg(1, chunk)
                first = False
            elif remaining:
                self.buf += seg(2, chunk)
            else:
                self.buf += seg(3, chunk)
                return

    def add_tapemark(self) -> None:
        if SEC - (self.pos() % SEC) < 13:
            self.buf += filler_to_boundary(self.pos())
        self.buf += tapemark()

    def finish(self) -> bytes:
        self.add_tapemark()
        self.add_tapemark()  # EOT
        self.buf += b"\xee" * (IMAGE_SIZE - len(self.buf))  # untouched tail
        return bytes(self.buf)


class TestL1Framing:
    def test_whole_record_round_trip(self):
        b = StreamBuilder()
        b.add_record(b"VOL1" + b"A" * 76)
        img = b.finish()
        events = FrameParser(img).parse()
        records = [e for e in events if e.kind == "record"]
        assert len(records) == 1
        assert records[0].payload == b"VOL1" + b"A" * 76
        assert records[0].category == "label"

    def test_multi_segment_record_spans_sectors(self):
        b = StreamBuilder()
        big = block_payload(1, b"\x01\x23\x45\x67\x89\xab\xcd\xef")
        b.add_record(big)  # 8192 bytes -> first + 7 middles + last
        img = b.finish()
        records = [e for e in FrameParser(img).parse() if e.kind == "record"]
        assert len(records) == 1
        assert records[0].payload == big
        assert records[0].category == "block"
        assert not records[0].damage

    def test_tapemarks_and_eot(self):
        b = StreamBuilder()
        b.add_record(b"HDR1" + b"X" * 76)
        b.add_tapemark()
        b.add_record(b"HDR2" + b"Y" * 76)
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        kinds = [e.kind for e in events]
        assert kinds.count("tapemark") == 3  # 1 explicit + 2 EOT
        assert p.eot_offset is not None
        # nothing after EOT is parsed: the last event is the second EOT
        # tapemark and eot_offset points just past its 12 framing bytes
        assert events[-1].kind == "tapemark"
        assert p.eot_offset == events[-1].offset + 12
        assert all(e.offset < p.eot_offset for e in events)

    def test_eot_survives_filler_between_tapemarks(self):
        # The writer may pad to a sector boundary between the two closing
        # tapemarks; only data records reset the tapemark run.
        b = StreamBuilder()
        b.add_record(b"VOL1" + b"A" * 76)
        b.add_tapemark()  # at 0x85C
        b.buf += filler_to_boundary(b.pos())
        b.add_tapemark()  # at 0xC00
        b.buf += b"\xee" * (IMAGE_SIZE - len(b.buf))
        p = FrameParser(bytes(b.buf))
        p.parse()
        assert p.eot_offset == 0xC00 + 12

    def test_filler_skipped(self):
        b = StreamBuilder()
        b.add_record(b"Z" * (SEC - 12 - 5))  # leaves <13 bytes in the sector
        b.add_record(b"W" * 80)  # builder inserts filler first
        img = b.finish()
        events = FrameParser(img, classifier=accept_all).parse()
        records = [e for e in events if e.kind == "record"]
        assert [len(r.payload) for r in records] == [SEC - 17, 80]
        assert any(e.kind == "filler" for e in events)

    def test_orphan_middle_becomes_gap(self):
        # A bare middle segment with no open record must not crash or be
        # consumed as a record: its sector is skipped as stale/gap and
        # parsing resumes at the next boundary.
        b = StreamBuilder()
        b.buf += seg(2, b"M" * 100)
        b.buf += filler_to_boundary(b.pos())
        b.add_record(b"VOL1" + b"A" * 76)
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        records = [e for e in events if e.kind == "record"]
        assert [r.payload for r in records] == [b"VOL1" + b"A" * 76]
        assert any(e.kind in ("stale", "gap") for e in events)
        assert p.eot_offset is not None

    def test_segments_never_cross_boundary_enforced(self):
        # A hand-built segment crossing a boundary must be rejected/flagged,
        # not silently consumed.
        b = StreamBuilder()
        raw = seg(0, b"Q" * 1100)  # 1112 bytes > sector room from 0x800
        b.buf += raw
        img = b.finish()
        events = FrameParser(img).parse()
        assert not any(e.kind == "record" and e.payload == b"Q" * 1100 for e in events)

    def test_truncated_image_raises(self):
        with pytest.raises(StreamError):
            FrameParser(b"APOLLO\x00\x01" + bytes(100)).parse()


class TestL1Pathologies:
    def test_stale_sector_skipped(self):
        # One whole sector of old medium junk between two records: skipped,
        # flagged "stale", and both neighbours survive.  (Payloads are
        # ANSI-label shaped so the default classifier accepts them.)
        b = StreamBuilder()
        b.add_record(b"HDR1" + b"A" * 76)
        b.buf += filler_to_boundary(b.pos())
        b.buf += b"\xde\xad" * (SEC // 2)  # one stale sector of junk
        b.add_record(b"HDR2" + b"B" * 76)
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        records = [e for e in events if e.kind == "record"]
        assert [r.payload for r in records] == [
            b"HDR1" + b"A" * 76,
            b"HDR2" + b"B" * 76,
        ]
        stales = [e for e in events if e.kind == "stale"]
        assert [e.offset for e in stales] == [0xC00]
        assert p.eot_offset is not None

    def test_stale_sector_with_old_framing_rejected_by_chain(self):
        # A stale sector may contain a well-framed WHOLE record from an
        # older backup run; structural parsing alone would accept it.  The
        # record-level classifier rejects it (wrong uid/seq) -> stale.
        uid = b"\x11\x22\x33\x44\x55\x66\x77\x88"
        old_uid = b"\x99\x99\x99\x99\x99\x99\x99\x99"
        b = StreamBuilder()
        b.add_record(block_payload(7, uid))  # establishes chain: next seq 8
        # stale sector holding a well-framed whole record from an old run
        stale_payload = struct.pack(">I", 8) + old_uid + b"\x00" * 500
        assert b.pos() % SEC != 0
        b.buf += filler_to_boundary(b.pos())
        b.buf += seg(0, stale_payload)
        b.buf += filler_to_boundary(b.pos())
        b.add_record(block_payload(8, uid))  # the real next block
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        records = [e for e in events if e.kind == "record"]
        assert [r.payload[:4] for r in records] == [
            struct.pack(">I", 7),
            struct.pack(">I", 8),
        ]
        assert all(r.payload[4:12] == uid for r in records)
        assert any(e.kind == "stale" for e in events)

    def test_lost_middles_zero_filled(self):
        # The writer stalled after the head segment and never wrote the
        # first middle; the medium continues contiguously with the
        # remaining segments, so the record assembles 1012 bytes short.
        # Reference-verified damage model: the lost middles are the FIRST
        # k after the head -> head + k*1012 zeros + survivors + tail.
        uid = b"\x12\x34\x56\x78\x9a\xbc\xde\xf0"
        payload = block_payload(1, uid)
        chunks = chunks_of(payload)  # head + 7 middles + 96-byte tail
        b = StreamBuilder()
        assert b.pos() % SEC == 0
        b.buf += seg(1, chunks[0])  # head fills sector 2 exactly
        for chunk in chunks[2:-1]:  # chunks[1] never reached the medium
            b.buf += seg(2, chunk)
        b.buf += seg(3, chunks[-1])
        b.add_record(block_payload(2, uid))  # chained follower
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        records = [e for e in events if e.kind == "record"]
        assert len(records) == 2
        damaged = records[0]
        assert len(damaged.payload) == 8192
        assert damaged.payload[:1012] == chunks[0]
        assert damaged.payload[1012:2024] == bytes(1012)  # zero-filled
        assert damaged.payload[2024:] == payload[2024:]
        assert damaged.damaged
        assert damaged.damage == ((1012, 1012),)
        assert damaged.note == "zero-filled 1 middles"
        # the seq/uid chain survives: the follower still parses
        assert records[1].payload == block_payload(2, uid)
        assert not records[1].damage
        assert p.eot_offset is not None

    def test_absorbed_stale_dropped(self):
        # disk2 0x1d400 analogue: the head segment ends exactly at a sector
        # boundary, the next sector is stale but holds a well-framed full
        # 1012-byte middle, then the real continuation follows.  The record
        # assembles 1012 bytes too long (9204 for an 8192 block); recovery
        # drops the first sector-aligned middle after the record start.
        uid = b"\x0f\x1e\x2d\x3c\x4b\x5a\x69\x78"
        payload = block_payload(1, uid)
        chunks = chunks_of(payload)
        b = StreamBuilder()
        b.buf += seg(1, chunks[0])  # head fills sector 2 exactly
        b.buf += seg(2, b"\xab" * 1012)  # stale sector, valid middle framing
        for chunk in chunks[1:-1]:  # the real middles
            b.buf += seg(2, chunk)
        b.buf += seg(3, chunks[-1])
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        records = [e for e in events if e.kind == "record"]
        assert len(records) == 1
        assert records[0].payload == payload  # exactly the real 8192 bytes
        assert not records[0].damage
        stales = [e for e in events if e.kind == "stale"]
        assert [e.offset for e in stales] == [0xC00]
        assert p.eot_offset is not None

    def test_drop_on_miss_zero_fills_in_order(self):
        # Near EOV trailers garbage sectors interleave with the record's
        # middles; the writer dropped one pending middle per missed sector.
        # The unparseable sector is skipped ("gapsec") and the hole is
        # zero-filled at its in-order position (disk8 sectors 1217/1219).
        uid = b"\xa1\xb2\xc3\xd4\xe5\xf6\x07\x18"
        payload = block_payload(1, uid)
        chunks = chunks_of(payload)
        b = StreamBuilder()
        b.buf += seg(1, chunks[0])
        b.buf += seg(2, chunks[1])
        b.buf += seg(2, chunks[2])
        b.buf += b"\xd5" * SEC  # garbage sector; middle chunks[3] dropped
        for chunk in chunks[4:-1]:
            b.buf += seg(2, chunk)
        b.buf += seg(3, chunks[-1])
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        records = [e for e in events if e.kind == "record"]
        assert len(records) == 1
        rec = records[0]
        hole = 3 * 1012
        assert len(rec.payload) == 8192
        assert rec.payload[:hole] == payload[:hole]
        assert rec.payload[hole : hole + 1012] == bytes(1012)
        assert rec.payload[hole + 1012 :] == payload[hole + 1012 :]
        assert rec.damage == ((hole, 1012),)
        gapsecs = [e for e in events if e.kind == "gapsec"]
        assert [e.offset for e in gapsecs] == [0x800 + 3 * SEC]
        assert p.eot_offset is not None

    def test_aegis_like_garbage_terminates_cleanly(self):
        # disk5 analogue: APOLLO magic but the stream area holds AEGIS
        # filesystem data, not a tape stream.  The parser must terminate
        # without exception or hang, find no EOT and emit no records.
        rng = random.Random(0x5EED)
        img = bytearray(b"APOLLO\x00\x01")
        img += bytes(START - len(img))
        img += bytes(rng.randrange(256) for _ in range(IMAGE_SIZE - START))
        p = FrameParser(bytes(img))
        events = p.parse()
        assert p.eot_offset is None
        assert not [e for e in events if e.kind == "record"]
        assert {e.kind for e in events} <= {
            "stale",
            "gap",
            "filler",
            "tapemark",
            "gapsec",
            "badrec",
        }
        # almost every one of the 1230 stream sectors is stale junk
        assert sum(1 for e in events if e.kind == "stale") > 1000


class TestRealDiskTiling:
    @pytest.mark.parametrize("image", ["disk2.img", "disk8.img"])
    def test_tiles_to_eot_without_unknowns(self, image):
        data = (RESOURCES / image).read_bytes()
        p = FrameParser(data)
        events = p.parse()
        assert p.eot_offset is not None
        assert not [e for e in events if e.kind == "gap"], "unexplained bytes"
        assert not [e for e in events if e.kind == "badrec"]

    def test_disk2_eot_offset(self):
        data = (RESOURCES / "disk2.img").read_bytes()
        p = FrameParser(data)
        p.parse()
        assert p.eot_offset == 0x32564  # frame_report.txt: EOT=0x32564

    def test_disk2_frame_stats(self):
        # Pinned against the reference (wbak_dump.py frame, re-run
        # 2026-06-11, identical to frame_report.txt):
        #   EOT=0x32564 blocks=25 (short:1) tm=4 labels=7 (VOL1/UVL1/HDR1/
        #   HDR2/UHL1/EOF1/EOF2 x1) fillers=1 stale-sectors=2 at [43, 117]
        #   damaged block: seq 24, six 1012-byte holes from offset 80
        data = (RESOURCES / "disk2.img").read_bytes()
        p = FrameParser(data)
        events = p.parse()
        recs = [e for e in events if e.kind == "record"]
        assert sum(1 for r in recs if len(r.payload) == 80) == 7
        assert sum(1 for r in recs if len(r.payload) > 80) == 25
        assert sum(1 for e in events if e.kind == "tapemark") == 4
        assert p.eot_offset == 0x32564
        assert [e.offset // SEC for e in events if e.kind == "stale"] == [43, 117]
        assert sum(1 for e in events if e.kind == "filler") == 1
        damaged = [r for r in recs if r.damage]
        assert len(damaged) == 1
        assert struct.unpack_from(">I", damaged[0].payload)[0] == 24
        assert damaged[0].damage == (
            (80, 1012),
            (1092, 1012),
            (2104, 1012),
            (3116, 1012),
            (4128, 1012),
            (5140, 1012),
        )

    def test_disk8_frame_stats(self):
        # Pinned against the reference (wbak_dump.py frame, re-run
        # 2026-06-11, identical to frame_report.txt):
        #   EOT=0x1310b4 blocks=150 (short:3) tm=7 labels=12 (VOL1 1, UVL1 1,
        #   HDR1 2, HDR2 2, UHL1 2, EOF1 1, EOF2 1, EOV1 1, EOV2 1)
        #   fillers=3 stale-sectors=12 at [43, 117, 266, 340, 414, 537, 611,
        #   685, 834, 908, 990, 1145]; lost-write gap sectors [1217, 1219]
        #   damaged blocks: seq 24 (6 holes from 80), seq 30 (6 holes from
        #   12; its head segment lost the block header), seq 85 (1 hole)
        data = (RESOURCES / "disk8.img").read_bytes()
        p = FrameParser(data)
        events = p.parse()
        recs = [e for e in events if e.kind == "record"]
        labels = [r for r in recs if len(r.payload) == 80]
        assert len(labels) == 12
        assert Counter(r.payload[:4] for r in labels) == Counter(
            {
                b"VOL1": 1,
                b"UVL1": 1,
                b"HDR1": 2,
                b"HDR2": 2,
                b"UHL1": 2,
                b"EOF1": 1,
                b"EOF2": 1,
                b"EOV1": 1,
                b"EOV2": 1,
            }
        )
        assert sum(1 for r in recs if len(r.payload) > 80) == 150
        assert sum(1 for e in events if e.kind == "tapemark") == 7
        assert p.eot_offset == 0x1310B4
        assert [e.offset // SEC for e in events if e.kind == "stale"] == [
            43,
            117,
            266,
            340,
            414,
            537,
            611,
            685,
            834,
            908,
            990,
            1145,
        ]
        assert [e.offset // SEC for e in events if e.kind == "gapsec"] == [1217, 1219]
        assert sum(1 for e in events if e.kind == "filler") == 3
        damaged = [
            (struct.unpack_from(">I", r.payload)[0], r.damage) for r in recs if r.damage
        ]
        assert damaged == [
            (
                24,
                (
                    (80, 1012),
                    (1092, 1012),
                    (2104, 1012),
                    (3116, 1012),
                    (4128, 1012),
                    (5140, 1012),
                ),
            ),
            (
                30,
                (
                    (12, 1012),
                    (1024, 1012),
                    (2036, 1012),
                    (3048, 1012),
                    (4060, 1012),
                    (5072, 1012),
                ),
            ),
            (85, ((6220, 1012),)),
        ]


# ---------------------------------------------------------------- L2 helpers


def label80(kind: str, **fields) -> bytes:
    """Build an 80-byte ANSI X3.27 label (ECMA-13 1-based byte positions).

    Numeric fields accept a ``*_text`` override so junk can be injected
    (real disk10 has a destroyed EOF1 with non-digit numeric fields).
    """
    buf = bytearray(b" " * 80)
    buf[0:4] = kind.encode("ascii")

    def put(bp: int, text: str) -> None:
        buf[bp - 1 : bp - 1 + len(text)] = text.encode("ascii")

    if kind == "VOL1":
        put(5, fields.get("volume_id", "").ljust(6))
        put(38, fields.get("owner", "").ljust(14))
        put(80, "3")
    elif kind in ("UVL1", "UHL1", "UTL1"):
        put(5, fields.get("text", ""))
    elif kind in ("HDR1", "EOF1", "EOV1"):
        put(5, fields.get("file_id", "").ljust(17))
        put(22, fields.get("set_id", "BACKUP"))
        put(28, fields.get("section_text", f"{fields.get('section', 1):04d}"))
        put(32, fields.get("sequence_text", f"{fields.get('sequence', 1):04d}"))
        put(36, "0001")
        put(40, "00")
        put(42, fields.get("created_text", " 86351"))
        put(48, " 86351")
        put(55, fields.get("block_count_text", f"{fields.get('block_count', 0):06d}"))
    elif kind in ("HDR2", "EOF2", "EOV2"):
        put(5, fields.get("record_format", "F"))
        put(6, f"{fields.get('block_len', 8192):05d}")
        put(11, f"{fields.get('record_len', 8192):05d}")
    return bytes(buf)


# ------------------------------------------------------------ L3/L4 helpers

SYN_UID = b"\x31\xf4\x9b\xd4\x20\x00\x71\xfa"  # == UVL1 "31F49BD4.200071FA"
SYN_TIME = 0x31F49BD4  # 1986-12-17 21:37:04 UTC


def obj_rec(type1: int, type2: int, body: bytes) -> bytes:
    """One L4 object record: 6-byte magic + payload, +1 pad if odd."""
    out = struct.pack(">HHH", type1, len(body), type2) + body
    if len(body) % 2:
        out += b"\x00"
    return out


def sub_rec(root: bytes = b"//NODE/TREE") -> bytes:
    body = struct.pack(">H", 1) + bytes(8) + struct.pack(">H", len(root)) + root
    return obj_rec(9, 1, body)


def mark_rec() -> bytes:
    return obj_rec(8, 1, bytes(4))


def name_rec(path: bytes) -> bytes:
    return obj_rec(2, 1, b"\x01" * 8 + bytes(4) + path)


def file_rec(size: int, blocks: int = 1, mtime: int = SYN_TIME) -> bytes:
    """SR9.5 FILE record: layout per the reference's decode_file_header."""
    body = (
        b"\x00\x00\x90\x00"  # magic 0x00009000
        + b"\x02" * 8  # object uid
        + struct.pack(">8I", 0x311, 0, 0x1800F, 0, size, blocks, mtime, mtime)
        + b"\x03" * 8  # parent uid
        + bytes(12)  # 3 x u32 spare
    )
    assert len(body) == 64
    return obj_rec(0, 1, body)


def dir_rec(name: bytes, mtime: int = SYN_TIME) -> bytes:
    attrs = (
        b"\x00\x01\x90\x00"  # magic 0x00019000
        + b"\x04" * 8
        + struct.pack(">8I", 0, 0, 0, 0, 0, 0, mtime, mtime)
        + bytes(8)
        + bytes(28)
    )
    assert len(attrs) == 80
    return obj_rec(3, 2, attrs + name)


def link_rec(path: bytes, target: bytes) -> bytes:
    body = bytes(4) + struct.pack(">H", len(path)) + path + target
    return obj_rec(5, 1, body)


def data_rec(content: bytes) -> bytes:
    return obj_rec(1, 1, content)


def storage_header(total_size: int) -> bytes:
    """The 32-byte object storage header (0020 0001 + dtm + total size)."""
    return b"\x00\x20\x00\x01" + bytes(8) + struct.pack(">I", total_size) + bytes(16)


def build_block(seq: int, uid: bytes, records: bytes) -> bytes:
    """A canonical 8192-byte block: 14-byte header + records + zero pad."""
    payload = struct.pack(">I", seq) + uid + struct.pack(">H", 14 + len(records))
    payload += records
    assert len(payload) <= 8192
    return payload + bytes(8192 - len(payload))


def build_synthetic_image() -> bytes:
    """Full two-tree wbak volume: tree A complete (EOF), tree B cut (EOV).

    Tree A holds a DIR "SUB", a FILE "SUB/HELLO" (11 content bytes behind
    the 32-byte storage header) and a LINK; tree B holds one FILE whose
    DATA was cut by the end of volume.
    """
    b = StreamBuilder()
    b.add_record(label80("VOL1", volume_id="SYN001", owner="APOLLO"))
    b.add_record(label80("UVL1", text="31F49BD4.200071FA"))
    # tree A
    b.add_record(label80("HDR1", file_id="TREEA", section=1, sequence=1))
    b.add_record(label80("HDR2"))
    b.add_record(label80("UHL1", text="31F49BD4.200071FA 1986/12/17 21:37:04"))
    b.add_tapemark()
    records_a = (
        sub_rec()
        + mark_rec()
        + dir_rec(b"SUB")
        + mark_rec()
        + name_rec(b"SUB/HELLO")
        + file_rec(11 + 32)
        + data_rec(storage_header(11 + 32) + b"hello world")
        + mark_rec()
        + link_rec(b"SUB/LNK", b"hello_target")
    )
    b.add_record(build_block(1, SYN_UID, records_a))
    b.add_tapemark()
    b.add_record(label80("EOF1", file_id="TREEA", section=1, sequence=1, block_count=1))
    b.add_record(label80("EOF2"))
    b.add_tapemark()
    # tree B: FILE claims 1000 content bytes; only 68 arrive before EOV
    b.add_record(label80("HDR1", file_id="TREEB", section=1, sequence=2))
    b.add_record(label80("HDR2"))
    b.add_record(label80("UHL1", text="31F49BD5.200071FA"))
    b.add_tapemark()
    records_b = (
        sub_rec()
        + mark_rec()
        + name_rec(b"WORLD")
        + file_rec(1000 + 32, blocks=2)
        + data_rec(storage_header(1000 + 32) + b"x" * 68)
    )
    b.add_record(build_block(1, SYN_UID, records_b))
    b.add_tapemark()
    b.add_record(label80("EOV1", file_id="TREEB", section=1, sequence=2, block_count=1))
    b.add_record(label80("EOV2"))
    b.add_tapemark()
    return b.finish()


class TestL2Labels:
    def test_vol1_fields(self):
        lab = AnsiLabel.parse(label80("VOL1", volume_id="5ETC", owner="APOLLO"))
        assert lab.kind == "VOL1"
        assert lab.volume_id == "5ETC"
        assert lab.owner == "APOLLO"

    def test_hdr1_fields_with_slash_file_id(self):
        raw = label80(
            "HDR1", file_id="SYS5/ETC", set_id="BACKUP", section=1, sequence=1
        )
        lab = AnsiLabel.parse(raw)
        assert lab.kind == "HDR1"
        assert lab.file_id == "SYS5/ETC"
        assert lab.set_id == "BACKUP"
        assert lab.section == 1
        assert lab.sequence == 1
        assert lab.created == date(1986, 12, 17)  # " 86351" = day 351 of 1986

    def test_eov_vs_eof_and_block_count(self):
        eof = AnsiLabel.parse(label80("EOF1", file_id="X", block_count=25))
        eov = AnsiLabel.parse(label80("EOV1", file_id="X", block_count=85))
        assert (eof.kind, eof.block_count) == ("EOF1", 25)
        assert (eov.kind, eov.block_count) == ("EOV1", 85)

    def test_junk_numeric_fields_decode_to_none(self):
        # real disk10 has a destroyed EOF1: numeric fields must not raise
        raw = label80(
            "EOF1",
            file_id="LIB",
            section_text="??\xff?".replace("\xff", "Q"),
            sequence_text="    ",
            created_text="\x07unk!?"[:6].ljust(6),
            block_count_text="?int??",
        )
        lab = AnsiLabel.parse(raw)
        assert lab.section is None
        assert lab.sequence is None
        assert lab.created is None
        assert lab.block_count is None

    def test_hdr2_fields(self):
        lab = AnsiLabel.parse(label80("HDR2"))
        assert lab.kind == "HDR2"
        assert lab.record_format == "F"
        assert lab.block_len == 8192
        assert lab.record_len == 8192

    def test_uhl1_uid_and_datetime_text(self):
        raw = label80("UHL1", text="31F49BD4.200071FA 1986/12/17 21:37:04")
        lab = AnsiLabel.parse(raw)
        assert lab.uid_text == "31F49BD4.200071FA"
        assert lab.date_text == "1986/12/17"
        assert lab.time_text == "21:37:04"

    def test_non_label_rejected(self):
        with pytest.raises(ValueError):
            AnsiLabel.parse(b"JUNK" + b" " * 76)


class TestNameDecoding:
    def test_uppercase_becomes_lowercase(self):
        assert decode_wbak_name(b"SUB/HELLO") == "sub/hello"
        assert decode_wbak_name(b"CLEANUP_V1.0") == "cleanup_v1.0"

    def test_colon_escape_keeps_literal_uppercase(self):
        assert decode_wbak_name(b"FOO:BAR") == "fooBar"
        assert decode_wbak_name(b":M:A:K:EFILE") == "MAKEfile"

    def test_passthrough_of_non_letters(self):
        assert decode_wbak_name(b"?") == "?"
        assert decode_wbak_name(b"A_1.2-3") == "a_1.2-3"


class TestApolloTime:
    def test_epoch(self):
        assert apollo_time_to_datetime(0) == datetime(1980, 1, 1, tzinfo=timezone.utc)

    def test_disk8_uhl1_uids_match_label_datetimes(self):
        # The committed disk8 carries two UHL1 labels (read from the image):
        #   UHL1 31F49A80.A00071FA 1986/12/17 21:35:35
        #   UHL1 31F49BD4.200071FA 1986/12/17 21:37:04
        # The UID high word is the Apollo creation timestamp; converting it
        # must land within a minute of the label's own date/time text.
        data = (RESOURCES / "disk8.img").read_bytes()
        events = FrameParser(data).parse()
        uhl1 = [
            AnsiLabel.parse(e.payload)
            for e in events
            if e.kind == "record" and e.payload[:4] == b"UHL1"
        ]
        assert len(uhl1) == 2
        for label in uhl1:
            stated = datetime.strptime(
                f"{label.date_text} {label.time_text}", "%Y/%m/%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            converted = apollo_time_to_datetime(int(label.uid_text.split(".")[0], 16))
            assert abs((converted - stated).total_seconds()) <= 60
        # pinned literal: the COM tree's UHL1 stamp
        assert uhl1[1].uid_text == "31F49BD4.200071FA"
        pinned = datetime(1986, 12, 17, 21, 37, 4, tzinfo=timezone.utc)
        assert abs((apollo_time_to_datetime(0x31F49BD4) - pinned).total_seconds()) <= 60


class TestCatalogSynthetic:
    def test_two_tree_catalog_with_file_dir_link(self):
        img = build_synthetic_image()
        cat = build_catalog(img)
        assert cat.volume_id == "SYN001"
        assert cat.owner == "APOLLO"
        assert cat.backup_uid == "31F49BD4.200071FA"
        assert cat.created == apollo_time_to_datetime(SYN_TIME)
        assert [(t.file_id, t.section, t.sequence, t.complete) for t in cat.trees] == [
            ("TREEA", 1, 1, True),
            ("TREEB", 1, 2, False),
        ]

        tree_a = cat.trees[0]
        assert tree_a.uid_text == "31F49BD4.200071FA"
        assert tree_a.created == date(1986, 12, 17)
        assert [e.path for e in tree_a.entries] == ["sub", "sub/hello", "sub/lnk"]
        sub, hello, lnk = tree_a.entries
        assert sub.is_dir and not sub.damaged
        assert hello.size == 11
        assert hello.raw_name == b"SUB/HELLO"
        assert hello.mtime == apollo_time_to_datetime(SYN_TIME)
        assert hello.atime == apollo_time_to_datetime(SYN_TIME)
        assert not hello.damaged and not hello.partial
        assert cat.read(hello) == b"hello world"
        assert lnk.link_target == "hello_target"
        # extents cover the raw DATA chunk (incl. the 32-byte storage
        # header) and point at real medium bytes
        assert sum(length for _, length in hello.extents) == 11 + 32
        raw = b"".join(img[o : o + n] for o, n in hello.extents)
        assert raw == storage_header(11 + 32) + b"hello world"

        tree_b = cat.trees[1]
        assert tree_b.complete is False
        (world,) = tree_b.entries
        assert world.path == "world"
        assert world.size == 1000
        assert world.partial is True  # DATA cut by end of volume
        assert world.damaged is False  # cut, not corrupted
        assert cat.read(world) == b"x" * 68  # available prefix only

    def test_continued_section_orphan_head_retained(self):
        # A tree section >= 2 continues a file opened on a previous volume:
        # DATA arriving before any NAME/FILE has no owner on this disk and
        # is RETAINED as the tree's orphan tail for cross-volume stitching
        # (it used to be skipped with a byte-count note).
        b = StreamBuilder()
        b.add_record(label80("VOL1", volume_id="SYN002", owner="APOLLO"))
        b.add_record(label80("UVL1", text="31F49BD4.200071FA"))
        b.add_record(label80("HDR1", file_id="TREEC", section=2, sequence=1))
        b.add_record(label80("HDR2"))
        b.add_record(label80("UHL1", text="31F49BD4.200071FA"))
        b.add_tapemark()
        records = (
            sub_rec()
            + data_rec(b"y" * 200)  # tail of the previous volume's file
            + mark_rec()
            + name_rec(b"NEWFILE")
            + file_rec(5 + 32)
            + data_rec(storage_header(5 + 32) + b"abcde")
        )
        b.add_record(build_block(1, SYN_UID, records))
        b.add_tapemark()
        b.add_record(label80("EOF1", file_id="TREEC", section=2, sequence=1))
        b.add_record(label80("EOF2"))
        b.add_tapemark()
        cat = build_catalog(b.finish())
        (tree,) = cat.trees
        assert tree.continued_from_previous is True
        assert tree.orphan_tail == b"y" * 200
        assert tree.orphan_damage == []
        assert [e.path for e in tree.entries] == ["newfile"]
        assert cat.read(tree.entries[0]) == b"abcde"

    def test_aegis_like_image_yields_empty_catalog(self):
        # Documented choice: an APOLLO container without a wbak tape stream
        # (disk5 analogue) returns an EMPTY catalog (volume_id None, no
        # trees) instead of raising -- detection (Task 5) scores on that.
        rng = random.Random(0x5EED)
        img = bytearray(b"APOLLO\x00\x01")
        img += bytes(START - len(img))
        img += bytes(rng.randrange(256) for _ in range(IMAGE_SIZE - START))
        cat = build_catalog(bytes(img))
        assert cat.volume_id is None
        assert cat.trees == []

    def test_truncated_image_still_raises(self):
        with pytest.raises(StreamError):
            build_catalog(b"APOLLO\x00\x01" + bytes(100))


class TestReviewBatch:
    def test_chain_only_validation_note_on_damaged_head(self):
        # disk8 block seq 30 lost its head segment (block header gone): it
        # validates on the seq/uid chain alone and must say so.
        data = (RESOURCES / "disk8.img").read_bytes()
        events = FrameParser(data).parse()
        flagged = [
            e
            for e in events
            if e.kind == "record" and "chain-only validation" in e.note
        ]
        assert len(flagged) == 1
        assert struct.unpack_from(">I", flagged[0].payload)[0] == 30

    def test_filler_event_length(self):
        b = StreamBuilder()
        b.add_record(b"Z" * (SEC - 12 - 5))  # leaves 5 bytes -> filler run
        b.add_record(b"W" * 80)
        events = FrameParser(b.finish(), classifier=accept_all).parse()
        fillers = [e for e in events if e.kind == "filler"]
        assert fillers[0].length == 5

    def test_irregular_short_record_sentinel_and_catalog_damage(self):
        # A short record whose segments do not fit the canonical block
        # tiling carries the ((0, 0)) sentinel; the catalog treats the
        # whole block as damaged without inventing hole extents.
        records = (
            sub_rec()
            + mark_rec()
            + name_rec(b"VICTIM")
            + file_rec(500 + 32)
            + data_rec(storage_header(500 + 32) + b"q" * 500)
        )
        payload = build_block(1, SYN_UID, records)
        b = StreamBuilder()
        b.add_record(label80("VOL1", volume_id="SYN003", owner="APOLLO"))
        b.add_record(label80("UVL1", text="31F49BD4.200071FA"))
        b.add_record(label80("HDR1", file_id="TREED", section=1, sequence=1))
        b.add_record(label80("HDR2"))
        b.add_record(label80("UHL1", text="31F49BD4.200071FA"))
        b.add_tapemark()
        b.buf += filler_to_boundary(b.pos())
        b.buf += seg(1, payload[:800])  # head + tail only: 900 bytes,
        b.buf += seg(3, payload[800:900])  # 8192-900 not a multiple of 1012
        b.add_tapemark()
        b.add_record(label80("EOF1", file_id="TREED", section=1, sequence=1))
        b.add_record(label80("EOF2"))
        b.add_tapemark()
        img = b.finish()

        events = FrameParser(img).parse()
        irregular = [e for e in events if e.kind == "record" and e.damage == ((0, 0),)]
        assert len(irregular) == 1
        assert irregular[0].note == "irregular short record"

        cat = build_catalog(img)
        (tree,) = cat.trees
        (victim,) = tree.entries
        assert victim.path == "victim"
        assert victim.damaged is True
        assert ("irregular", 1) in victim.damage_notes
        assert not any(note[0] == "hole" for note in victim.damage_notes)


class TestCatalogReal:
    def test_disk2_inventory(self):
        # Pinned against the reference implementation, re-run 2026-06-11:
        #   python3 docs/superpowers/research/apollo/empirical/wbak_dump.py \
        #       list tests/resources/APOLLO/disk2.img
        # -> SYS5/ETC (seq 1, uid 32A339B6.F00071FA) EOF, 25 blocks,
        #    36 files, 2 dirs, 3 links; link SYS5/ETC/RC -> '`node_data/etc.rc';
        #    9 files carry DAMAGE annotations; MOTD's is
        #    [('hole', 24, 1040, 186)] (matches full_listing.txt).
        data = (RESOURCES / "disk2.img").read_bytes()
        cat = build_catalog(data)
        assert cat.volume_id == "5ETC"
        assert cat.owner == "APOLLO"
        assert cat.backup_uid == "32A339B7.000071FA"
        assert cat.created is not None
        assert cat.created.date() == date(1987, 1, 21)

        (tree,) = cat.trees
        assert tree.file_id == "SYS5/ETC"
        assert tree.section == 1
        assert tree.sequence == 1
        assert tree.complete is True  # EOF
        assert tree.uid_text == "32A339B6.F00071FA"
        assert tree.created == date(1987, 1, 21)  # HDR1 " 87021"
        assert tree.continued_from_previous is False
        assert tree.block_count == 25

        files = [e for e in tree.entries if not e.is_dir and e.link_target is None]
        dirs = [e for e in tree.entries if e.is_dir]
        links = [e for e in tree.entries if e.link_target is not None]
        assert len(files) == 36
        assert len(dirs) == 2
        assert len(links) == 3
        assert {d.path for d in dirs} == {"", "net"}  # "" = the tree root

        rc = next(e for e in links if e.path == "rc")
        assert rc.link_target == "`node_data/etc.rc"
        assert {e.path for e in links} == {"rc", "mnttab", "utmp"}

        # damage census: 9 files carry parser damage annotations
        assert sum(1 for e in files if e.damage_notes) == 9
        motd = next(e for e in files if e.path == "motd")
        assert motd.damaged is True
        assert motd.damage_notes == [("hole", 24, 1040, 186)]
        # the file whose NAME record was destroyed keeps the placeholder
        assert sum(1 for e in files if e.path == "?") == 1

        # an intact file reads fully: RELEASELOG, 74 - 32 = 42 bytes
        releaselog = next(e for e in files if e.path == "releaselog")
        assert releaselog.size == 42
        assert not releaselog.damaged
        assert len(cat.read(releaselog)) == 42

    def test_disk8_inventory_and_extraction(self):
        # Pinned against the reference implementation, re-run 2026-06-11:
        #   python3 docs/superpowers/research/apollo/empirical/wbak_dump.py \
        #       list tests/resources/APOLLO/disk8.img
        # -> INSTALL (seq 1) EOF, 65 blocks, 72 files + 5 dirs;
        #    COM (seq 2, section 1) EOV, 85 blocks, 2 files, the open one
        #    cut at [SHORT 130218/439276] with [('hole', 85, 128246, 1012)].
        # The CLEANUP_V1.0 sha256 is of the reference-extracted file
        # (empirical/extracted/INSTALL.seq1.31F49A80/INSTALL/COM/
        # CLEANUP_V1.0, 1226 bytes) with its 32-byte storage header
        # stripped: sha256(file[32:]), 1194 bytes.
        data = (RESOURCES / "disk8.img").read_bytes()
        cat = build_catalog(data)
        assert cat.volume_id == "FT0003"
        assert cat.owner == "APOLLO"
        assert cat.backup_uid == "31F49A87.B00071FA"
        assert [(t.file_id, t.section, t.sequence, t.complete) for t in cat.trees] == [
            ("INSTALL", 1, 1, True),
            ("COM", 1, 2, False),
        ]

        install = cat.trees[0]
        assert install.block_count == 65
        files = [e for e in install.entries if not e.is_dir and e.link_target is None]
        dirs = [e for e in install.entries if e.is_dir]
        assert len(files) == 72
        assert len(dirs) == 5
        assert {d.path for d in dirs} == {
            "",
            "com",
            "optional_sw_files",
            "ftn",
            "ftn/com",
        }

        cleanup = next(e for e in files if e.path == "com/cleanup_v1.0")
        assert cleanup.size == 1226 - 32
        assert not cleanup.damaged and not cleanup.partial
        # FILE-record time fields: time1 == time2 == 0x31F49334 (decoded
        # with the reference's field layout; 1986-12-17T21:27:25Z)
        assert cleanup.mtime == apollo_time_to_datetime(0x31F49334)
        assert cleanup.mtime.date() == date(1986, 12, 17)
        assert cleanup.atime == cleanup.mtime
        content = cat.read(cleanup)
        assert len(content) == cleanup.size
        assert content.startswith(b"#!/com/sh")
        assert b"compat_cleanup" in content
        assert (
            hashlib.sha256(content).hexdigest()
            == "3e4f699d17b9b08936e1f1d501bec3c3a56c4d2e978eb64134a32ac4186539e1"
        )

        # a damaged-flagged entry from the absorbed-stale/lost-middle disk:
        # CPT carries [('zerofill', 7, 2348, 3144)] per the reference
        cpt = next(e for e in files if e.path == "com/cpt")
        assert cpt.damaged is True
        assert ("zerofill", 7, 2348, 3144) in cpt.damage_notes

        com = cat.trees[1]
        assert com.complete is False  # EOV -> continues on another volume
        assert com.block_count == 85
        assert len(com.entries) == 2
        orphan, ftn = com.entries
        assert orphan.path == "?"  # NAME record lost to in-block junk
        assert orphan.damaged is True
        assert ftn.path == "ftn_sr9.2"
        assert ftn.partial is True  # cut by EOV
        assert ftn.damaged is True  # also overlaps an L1 zero-fill
        assert ("hole", 85, 128246, 1012) in ftn.damage_notes
        assert ftn.size == 439276 - 32
        prefix = cat.read(ftn)
        assert len(prefix) == 130218 - 32  # available prefix only


# ----------------------------------------------- cross-volume set builders


def syn_content(total: int) -> bytes:
    """Deterministic non-trivial content (no 00-byte pairs: cannot fake
    embedded MARK/POPD record starts inside DATA bodies)."""
    return bytes((i * 7 + 3) % 256 for i in range(total))


def build_cut_volume(
    *,
    volume_id="SYNVA0",
    uid_text="31F49BD4.200071FA",
    volume_uid_text=None,
    uid=SYN_UID,
    file_id="COM",
    sequence=2,
    content=b"",
    chunks=(6000, 2000),
    name=b"BIGFILE",
):
    """Volume A of a set: HDR1 section 1, one FILE declaring
    ``len(content) + 32`` raw bytes but carrying only ``sum(chunks)``
    content bytes (one DATA chunk per block) before the EOV trailer.
    ``volume_uid_text`` overrides the per-volume UVL1 uid (defaults to
    ``uid_text``): real sets stamp a DIFFERENT UVL1 uid on every volume
    while the per-tree UHL1 uid stays invariant."""
    raw_size = len(content) + 32
    b = StreamBuilder()
    b.add_record(label80("VOL1", volume_id=volume_id, owner="APOLLO"))
    b.add_record(label80("UVL1", text=volume_uid_text or uid_text))
    b.add_record(label80("HDR1", file_id=file_id, section=1, sequence=sequence))
    b.add_record(label80("HDR2"))
    b.add_record(label80("UHL1", text=uid_text))
    b.add_tapemark()
    first = (
        sub_rec()
        + mark_rec()
        + name_rec(name)
        + file_rec(raw_size, blocks=-(-raw_size // 1024))
        + data_rec(storage_header(raw_size) + content[: chunks[0]])
    )
    b.add_record(build_block(1, uid, first))
    done = chunks[0]
    for seq, n in enumerate(chunks[1:], start=2):
        b.add_record(
            build_block(seq, uid, sub_rec() + data_rec(content[done : done + n]))
        )
        done += n
    b.add_tapemark()
    b.add_record(
        label80(
            "EOV1",
            file_id=file_id,
            section=1,
            sequence=sequence,
            block_count=len(chunks),
        )
    )
    b.add_record(label80("EOV2"))
    b.add_tapemark()
    return b.finish()


def build_continuation_volume(
    *,
    volume_id="SYNVB0",
    uid_text="31F49BD4.200071FA",
    volume_uid_text=None,
    uid=SYN_UID,
    file_id="COM",
    sequence=2,
    section=2,
    first_seq=3,
    tail=b"",
    follow=(b"FOLLOWON", b"follow-on data"),
    complete=True,
):
    """A continuation volume: HDR1 section >= 2, leading orphan DATA (the
    tail of the file cut on the previous volume), then optionally a
    follow-on NAME/FILE/DATA object, then EOF (complete) or EOV.
    ``volume_uid_text`` overrides the per-volume UVL1 uid (defaults to
    ``uid_text``), mirroring real sets whose UVL1 uids differ per volume."""
    b = StreamBuilder()
    b.add_record(label80("VOL1", volume_id=volume_id, owner="APOLLO"))
    b.add_record(label80("UVL1", text=volume_uid_text or uid_text))
    b.add_record(label80("HDR1", file_id=file_id, section=section, sequence=sequence))
    b.add_record(label80("HDR2"))
    b.add_record(label80("UHL1", text=uid_text))
    b.add_tapemark()
    recs = sub_rec()
    if tail:
        recs += data_rec(tail)
    if follow is not None:
        follow_name, follow_content = follow
        raw = len(follow_content) + 32
        recs += (
            mark_rec()
            + name_rec(follow_name)
            + file_rec(raw)
            + data_rec(storage_header(raw) + follow_content)
        )
    b.add_record(build_block(first_seq, uid, recs))
    b.add_tapemark()
    kind1, kind2 = ("EOF1", "EOF2") if complete else ("EOV1", "EOV2")
    b.add_record(
        label80(
            kind1, file_id=file_id, section=section, sequence=sequence, block_count=1
        )
    )
    b.add_record(label80(kind2))
    b.add_tapemark()
    return b.finish()


def build_two_volume_set(**cont_overrides):
    """Synthetic two-volume backup set; every expected literal derives
    from what this builder wrote.

    Volume A: tree COM seq 2 section 1; FILE 'BIGFILE' declares 9000
    content bytes but carries 8000 (6000 + 2000 across blocks 1-2), EOV.
    Volume B (same backup uid): section 2 opening with the 1000-byte
    orphan tail that completes the file, then a follow-on file, EOF.
    Block seqs run 1,2 on A and 3 on B (continuous).  ``cont_overrides``
    are passed to :func:`build_continuation_volume` (mismatch tests).
    """
    content = syn_content(9000)
    vol_a = build_cut_volume(content=content, chunks=(6000, 2000))
    kw = {"tail": content[8000:], "first_seq": 3}
    kw.update(cont_overrides)
    vol_b = build_continuation_volume(**kw)
    return SimpleNamespace(
        vol_a=vol_a,
        vol_b=vol_b,
        content=content,
        prefix=content[:8000],
        tail=content[8000:],
        uid_text="31F49BD4.200071FA",
        file_id="COM",
        sequence=2,
        follow_path="followon",
        follow_content=b"follow-on data",
    )


def build_three_volume_set():
    """Three-volume chain: A (section 1, 6000 of 9000 content bytes, EOV)
    -> B (section 2, 2000-byte tail, still short, EOV) -> C (section 3,
    final 1000-byte tail, EOF).  Block seqs 1, 2, 3 (continuous)."""
    content = syn_content(9000)
    vol_a = build_cut_volume(content=content, chunks=(6000,))
    vol_b = build_continuation_volume(
        volume_id="SYNVB0",
        section=2,
        first_seq=2,
        tail=content[6000:8000],
        follow=None,
        complete=False,
    )
    vol_c = build_continuation_volume(
        volume_id="SYNVC0",
        section=3,
        first_seq=3,
        tail=content[8000:],
        follow=None,
        complete=True,
    )
    return SimpleNamespace(
        volumes=(vol_a, vol_b, vol_c),
        content=content,
        file_id="COM",
        sequence=2,
    )


class TestOrphanTailRetention:
    def test_continuation_tree_retains_orphan_tail(self):
        s = build_two_volume_set()
        cat = build_catalog(s.vol_b)
        (tree,) = cat.trees
        assert tree.section == 2 and tree.continued_from_previous
        assert len(tree.orphan_tail) == 1000  # == len(s.tail), builder-derived
        assert tree.orphan_tail == s.tail
        assert tree.orphan_damage == []
        # the follow-on object after the tail still catalogs normally
        (follow,) = [e for e in tree.entries if not e.is_dir]
        assert follow.path == s.follow_path
        assert cat.read(follow) == s.follow_content
        # block seq range is recorded for cross-volume continuity checks
        assert (tree.first_block_seq, tree.last_block_seq) == (3, 3)

    def test_section1_trees_have_empty_tail(self):
        # real disks: every committed tree is section 1 -> no tail, and
        # the inventories pinned elsewhere prove the entries are unchanged
        for image in ("disk2.img", "disk8.img"):
            cat = build_catalog((RESOURCES / image).read_bytes())
            assert cat.trees
            for tree in cat.trees:
                assert tree.section == 1
                assert tree.orphan_tail == b""
                assert tree.orphan_damage == []

    def test_orphan_tail_damage_accounted(self):
        # The tail's first DATA chunk is cut by the block end: it claims
        # 8178 bytes but the block holds 8142 -- the missing 36 were never
        # written.  Same zero-fill semantics as owned-file assembly: pad
        # so later chunks land at the right offsets, flag the loss.  The
        # stitched entry inherits the damage at the rebased offset.
        total = 2000 + 8178 + 300
        content = syn_content(total)
        chunk = content[2000 : 2000 + 8142]  # fills block 2 to exactly 8192
        rest = content[2000 + 8178 :]  # 300 bytes after the 36 lost ones
        vol_a = build_cut_volume(content=content, chunks=(2000,))

        b = StreamBuilder()
        b.add_record(label80("VOL1", volume_id="SYNDMG", owner="APOLLO"))
        b.add_record(label80("UVL1", text="31F49BD4.200071FA"))
        b.add_record(label80("HDR1", file_id="COM", section=2, sequence=2))
        b.add_record(label80("HDR2"))
        b.add_record(label80("UHL1", text="31F49BD4.200071FA"))
        b.add_tapemark()
        recs = sub_rec() + struct.pack(">HHH", 1, 8178, 1) + chunk
        payload = struct.pack(">I", 2) + SYN_UID + struct.pack(">H", 8192) + recs
        assert len(payload) == 8192  # the cut DATA chunk ends at the block end
        b.add_record(payload)
        b.add_record(build_block(3, SYN_UID, sub_rec() + data_rec(rest)))
        b.add_tapemark()
        b.add_record(label80("EOF1", file_id="COM", section=2, sequence=2))
        b.add_record(label80("EOF2"))
        b.add_tapemark()
        vol_b = b.finish()

        cat_b = build_catalog(vol_b)
        (tree,) = cat_b.trees
        assert tree.orphan_tail == chunk + bytes(36) + rest
        assert tree.orphan_damage == [("zerofill", 2, 8142, 36)]

        cat_a = build_catalog(vol_a)
        merged = stitch_tree(
            cat_a.trees[0],
            tree,
            prev_uid=cat_a.backup_uid,
            cont_uid=cat_b.backup_uid,
        )
        (stitched,) = [e for e in merged.entries if e.path == "bigfile"]
        assert stitched.partial is False  # declared size reached
        assert stitched.damaged is True  # ... but 36 bytes were zero-filled
        # rebased: 32 (storage header) + 2000 (volume A) + 8142 into raw data
        assert ("zerofill", 2, 32 + 2000 + 8142, 36) in stitched.damage_notes
        got = cat_a.read(stitched)
        assert got == content[: 2000 + 8142] + bytes(36) + content[2000 + 8178 :]


class TestContinuationSpec:
    def test_expected_continuation_for_eov_tree(self):
        # disk8 real: the COM tree (section 1, EOV) expects section 2 of
        # COM seq 2 on the next volume of set FT0003.  The spec's uid is
        # the PER-TREE UHL1 uid (the cross-volume invariant: FT0003's
        # per-volume UVL1 uids differ between disk8 and disk1 while COM's
        # UHL1 is identical on both), read from the catalog at test time,
        # not hardcoded.
        cat = build_catalog((RESOURCES / "disk8.img").read_bytes())
        com = cat.trees[1]
        assert com.complete is False
        spec = expected_continuation(com, cat)
        assert spec == ContinuationSpec(
            file_id="COM",
            sequence=2,
            next_section=2,
            backup_uid=com.uid_text,
            volume_id="FT0003",
        )
        assert spec.backup_uid is not None
        # the per-volume UVL1 uid is NOT the invariant: disk8 alone proves
        # the two differ (UVL1 31F49A87... vs the COM UHL1 31F49BD4...)
        assert spec.backup_uid != cat.backup_uid

    def test_complete_tree_has_no_continuation(self):
        cat = build_catalog((RESOURCES / "disk8.img").read_bytes())
        install = cat.trees[0]
        assert install.complete is True
        assert expected_continuation(install, cat) is None


class TestStitchTree:
    @staticmethod
    def _stitch(vol_a, vol_b):
        cat_a = build_catalog(vol_a)
        cat_b = build_catalog(vol_b)
        merged = stitch_tree(
            cat_a.trees[0],
            cat_b.trees[0],
            prev_uid=cat_a.backup_uid,
            cont_uid=cat_b.backup_uid,
        )
        return cat_a, cat_b, merged

    def test_stitch_completes_cut_file(self):
        s = build_two_volume_set()
        cat_a, cat_b, merged = self._stitch(s.vol_a, s.vol_b)
        prev, cont = cat_a.trees[0], cat_b.trees[0]
        cut = next(e for e in prev.entries if e.path == "bigfile")
        assert cut.partial is True
        assert cat_a.read(cut) == s.prefix  # 8000 of 9000 declared bytes

        stitched = next(e for e in merged.entries if e.path == "bigfile")
        assert stitched.partial is False
        assert stitched.damaged is False
        assert cat_a.read(stitched) == s.content  # all 9000 bytes
        assert merged.complete is True
        assert merged.section == 2  # a further continuation would be 3
        assert (merged.file_id, merged.sequence) == (s.file_id, s.sequence)
        assert merged.block_count == prev.block_count + cont.block_count
        # primary-volume extents are kept: they index the primary image
        assert stitched.extents == cut.extents

        # stitching mutates NEITHER input tree
        assert cut.partial is True and len(cut.raw_data) == 32 + 8000
        assert prev.complete is False
        assert cont.orphan_tail == s.tail

    def test_stitch_appends_follow_on_entries(self):
        s = build_two_volume_set()
        cat_a, cat_b, merged = self._stitch(s.vol_a, s.vol_b)
        follow = next(e for e in merged.entries if e.path == s.follow_path)
        assert cat_a.read(follow) == s.follow_content
        # its bytes live on volume B: no extents into the primary image
        # (the disk map stays primary-volume-only)
        assert follow.extents == []
        # ... while the original entry on volume B's catalog keeps its own
        orig = next(e for e in cat_b.trees[0].entries if e.path == s.follow_path)
        assert orig.extents != []

    def test_stitch_validation_matrix(self):
        s = build_two_volume_set()
        cat_a = build_catalog(s.vol_a)
        prev = cat_a.trees[0]
        uid = cat_a.backup_uid

        def cont_catalog(**kw):
            kw.setdefault("tail", s.tail)
            kw.setdefault("first_seq", 3)
            return build_catalog(build_continuation_volume(**kw))

        bad = cont_catalog(file_id="LIB")
        with pytest.raises(ValueError, match=r"file_id 'LIB'.*'COM'"):
            stitch_tree(prev, bad.trees[0], prev_uid=uid, cont_uid=bad.backup_uid)

        bad = cont_catalog(sequence=3)
        with pytest.raises(ValueError, match=r"sequence 3.*2"):
            stitch_tree(prev, bad.trees[0], prev_uid=uid, cont_uid=bad.backup_uid)

        for wrong_section in (1, 3):
            bad = cont_catalog(section=wrong_section)
            with pytest.raises(
                ValueError, match=rf"section {wrong_section}.*section 2"
            ):
                stitch_tree(prev, bad.trees[0], prev_uid=uid, cont_uid=bad.backup_uid)

        wrong_uid_text = "32A339B7.000071FA"
        bad = cont_catalog(
            uid_text=wrong_uid_text, uid=bytes.fromhex("32a339b7000071fa")
        )
        with pytest.raises(ValueError, match=r"32A339B7\.000071FA.*31F49BD4\.200071FA"):
            stitch_tree(prev, bad.trees[0], prev_uid=uid, cont_uid=bad.backup_uid)

        # validation order: uid outranks file_id when both are wrong
        bad = cont_catalog(
            file_id="LIB",
            uid_text=wrong_uid_text,
            uid=bytes.fromhex("32a339b7000071fa"),
        )
        with pytest.raises(ValueError, match=r"32A339B7\.000071FA"):
            stitch_tree(prev, bad.trees[0], prev_uid=uid, cont_uid=bad.backup_uid)

        # internal consistency: at most one partial entry on the prev side
        broken = WbakTree(
            file_id="COM",
            section=1,
            sequence=2,
            complete=False,
            entries=[
                WbakEntry(path="a", size=10, partial=True),
                WbakEntry(path="b", size=10, partial=True),
            ],
        )
        good = cont_catalog()
        with pytest.raises(ValueError, match=r"partial"):
            stitch_tree(broken, good.trees[0], prev_uid=None, cont_uid=good.backup_uid)

    def test_stitch_with_differing_per_volume_uids(self):
        # FT0003-proven identity nuance: every volume stamps its OWN UVL1
        # uid (disk8 31F49A87.B00071FA vs disk1 31F49D73.D00071FA) while
        # the tree's UHL1 uid is identical on both -- the per-tree uid is
        # the cross-volume invariant.  A stitch keyed on the tree uids
        # must succeed despite the UVL1 mismatch.
        content = syn_content(9000)
        vol_a = build_cut_volume(
            content=content,
            chunks=(6000, 2000),
            volume_uid_text="31F49A87.B00071FA",
        )
        vol_b = build_continuation_volume(
            tail=content[8000:],
            first_seq=3,
            volume_uid_text="31F49D73.D00071FA",
        )
        cat_a = build_catalog(vol_a)
        cat_b = build_catalog(vol_b)
        assert cat_a.backup_uid != cat_b.backup_uid  # per-volume UVL1s differ
        tree_a, tree_b = cat_a.trees[0], cat_b.trees[0]
        assert tree_a.uid_text == tree_b.uid_text  # per-tree UHL1 matches
        # the continuation spec advertises the per-tree uid, not the UVL1
        spec = expected_continuation(tree_a, cat_a)
        assert spec.backup_uid == tree_a.uid_text
        assert spec.backup_uid != cat_a.backup_uid
        merged = stitch_tree(
            tree_a, tree_b, prev_uid=tree_a.uid_text, cont_uid=tree_b.uid_text
        )
        assert merged.complete is True
        stitched = next(e for e in merged.entries if e.path == "bigfile")
        assert stitched.partial is False
        assert cat_a.read(stitched) == content

    def test_three_volume_chain(self):
        s = build_three_volume_set()
        cat_a = build_catalog(s.volumes[0])
        cat_b = build_catalog(s.volumes[1])
        cat_c = build_catalog(s.volumes[2])

        m1 = stitch_tree(
            cat_a.trees[0],
            cat_b.trees[0],
            prev_uid=cat_a.backup_uid,
            cont_uid=cat_b.backup_uid,
        )
        # intermediate result: the EOV continuation leaves the file short
        assert m1.complete is False
        cut = next(e for e in m1.entries if e.path == "bigfile")
        assert cut.partial is True
        assert cat_a.read(cut) == s.content[:8000]
        # ... and the stitched tree advertises the NEXT continuation
        spec = expected_continuation(m1, cat_a)
        assert spec == ContinuationSpec(
            file_id="COM",
            sequence=2,
            next_section=3,
            backup_uid=cat_a.backup_uid,
            volume_id=cat_a.volume_id,
        )

        m2 = stitch_tree(
            m1,
            cat_c.trees[0],
            prev_uid=cat_a.backup_uid,
            cont_uid=cat_c.backup_uid,
        )
        assert m2.complete is True
        done = next(e for e in m2.entries if e.path == "bigfile")
        assert done.partial is False
        assert cat_a.read(done) == s.content
        assert expected_continuation(m2, cat_a) is None

    def test_empty_orphan_tail_boundary_cut(self):
        # the file's last byte landed exactly at the end of volume A: the
        # entry is complete (never flagged partial) but the tree still
        # ends EOV; the continuation opens with NO orphan tail and the
        # stitch just marks the tree complete and appends the new file.
        content = syn_content(9000)
        vol_a = build_cut_volume(content=content, chunks=(6000, 3000))
        vol_b = build_continuation_volume(tail=b"", first_seq=3)
        cat_a, cat_b, merged = self._stitch(vol_a, vol_b)
        big = next(e for e in cat_a.trees[0].entries if e.path == "bigfile")
        assert big.partial is False and cat_a.trees[0].complete is False
        assert cat_b.trees[0].orphan_tail == b""

        assert merged.complete is True
        stitched = next(e for e in merged.entries if e.path == "bigfile")
        assert stitched.partial is False
        assert cat_a.read(stitched) == content
        assert merged.entries[-1].path == "followon"
        assert not any(e.partial for e in merged.entries)

    def test_orphan_tail_without_partial_predecessor_warns(self, caplog):
        # pathological: no partial entry on the prev side, yet the
        # continuation carries a tail -- warn and discard, never fail,
        # and never graft the tail onto a complete file.
        content = syn_content(9000)
        vol_a = build_cut_volume(content=content, chunks=(6000, 3000))
        vol_b = build_continuation_volume(tail=b"\x5a" * 64, first_seq=3)
        with caplog.at_level(logging.WARNING, logger="fatfloppy.core.apollo_wbak"):
            cat_a, _cat_b, merged = self._stitch(vol_a, vol_b)
        assert any("no partial" in r.message for r in caplog.records)
        assert merged.complete is True
        stitched = next(e for e in merged.entries if e.path == "bigfile")
        assert cat_a.read(stitched) == content  # the stray tail went nowhere

    def test_bh_seq_discontinuity_warns_not_fails(self, caplog):
        # volume A's tree ends at block 2; a continuation starting at
        # block 9 is suspicious (lost blocks?) but labels are
        # authoritative: warn and stitch anyway.
        s = build_two_volume_set(first_seq=9)
        cat_a = build_catalog(s.vol_a)
        cat_b = build_catalog(s.vol_b)
        assert cat_a.trees[0].last_block_seq == 2
        assert cat_b.trees[0].first_block_seq == 9
        with caplog.at_level(logging.WARNING, logger="fatfloppy.core.apollo_wbak"):
            merged = stitch_tree(
                cat_a.trees[0],
                cat_b.trees[0],
                prev_uid=cat_a.backup_uid,
                cont_uid=cat_b.backup_uid,
            )
        assert any("discontinuity" in r.message for r in caplog.records)
        stitched = next(e for e in merged.entries if e.path == "bigfile")
        assert stitched.partial is False
        assert cat_a.read(stitched) == s.content

        # the continuous case (block 3 follows block 2) stays silent
        caplog.clear()
        s2 = build_two_volume_set()
        with caplog.at_level(logging.WARNING, logger="fatfloppy.core.apollo_wbak"):
            self._stitch(s2.vol_a, s2.vol_b)
        assert not any("discontinuity" in r.message for r in caplog.records)
