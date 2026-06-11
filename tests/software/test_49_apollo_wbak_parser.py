# tests/software/test_49_apollo_wbak_parser.py
"""Apollo wbak parser tests: L1 framing, labels, records, catalog."""

import random
import struct
from collections import Counter
from pathlib import Path

import pytest

from fatfloppy.core.apollo_wbak import (
    FrameParser,
    StreamError,
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
