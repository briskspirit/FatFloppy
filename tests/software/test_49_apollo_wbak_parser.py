# tests/software/test_49_apollo_wbak_parser.py
"""Apollo wbak parser tests: L1 framing, labels, records, catalog."""

import struct

import pytest

from fatfloppy.core.apollo_wbak import (
    FrameParser,
    StreamError,
)

SEC = 1024
START = 0x800
IMAGE_SIZE = 1_261_568


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

    def test_multi_segment_record_spans_sectors(self):
        b = StreamBuilder()
        big = bytes(range(256)) * 40  # 10240 bytes -> first/middle/last
        b.add_record(big)
        img = b.finish()
        records = [e for e in FrameParser(img).parse() if e.kind == "record"]
        assert len(records) == 1
        assert records[0].payload == big

    def test_tapemarks_and_eot(self):
        b = StreamBuilder()
        b.add_record(b"X" * 80)
        b.add_tapemark()
        b.add_record(b"Y" * 80)
        img = b.finish()
        p = FrameParser(img)
        events = p.parse()
        kinds = [e.kind for e in events]
        assert kinds.count("tapemark") >= 3  # 1 explicit + 2 EOT
        assert p.eot_offset is not None
        # nothing after EOT is parsed
        assert all(e.offset < p.eot_offset + 24 for e in events)

    def test_filler_skipped(self):
        b = StreamBuilder()
        b.add_record(b"Z" * (SEC - 12 - 5))  # leaves <13 bytes in the sector
        b.add_record(b"W" * 80)  # builder inserts filler first
        img = b.finish()
        records = [e for e in FrameParser(img).parse() if e.kind == "record"]
        assert [len(r.payload) for r in records] == [SEC - 17, 80]

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
