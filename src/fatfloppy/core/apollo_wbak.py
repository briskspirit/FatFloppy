"""Apollo DOMAIN wbak backup floppy parser (pure byte-stream, no disk I/O).

Parses the "floppy-as-tape" stream written by the Apollo AEGIS ``wbak``
utility onto 77x2x8x1024 (1,261,568-byte) floppies.  The normative format
description lives in ``docs/superpowers/specs/2026-06-11-apollo-wbak-design.md``
section 2; the empirically proven reference implementation is
``docs/superpowers/research/apollo/empirical/wbak_dump.py``.

L1 grammar (spec section 2.2)::

    stream  := from 0x800: item* EOT ; tail = untouched old medium
    item    := segment | filler | stale_sector
    segment := flags:u16BE len:u32BE payload[len] len:u32BE flags:u16BE
               flags: 0 whole, 1 first, 2 middle, 3 last, 4 tapemark (len 0)
               segments never cross a 1024-byte sector boundary
    filler  := u16BE 0x0006 repeated to the next sector boundary (written
               when remaining space < 13 bytes)
    record  := whole | first {middle} last   (payloads concatenated)
    EOT     := two consecutive tapemarks

Sector 0 carries the Apollo PV label (magic ``APOLLO``); sector 1 is not
part of the stream; everything after EOT is untouched old medium content.

This module currently implements the clean grammar only: bytes that do not
parse as a segment or a filler run produce a ``gap`` event and the parser
resynchronises at the next sector boundary.  Recovery of the documented
writer pathologies (stale sectors, absorbed stales, lost middles,
drop-on-miss; spec section 2.2) replaces that fallback in a later step.
"""

import struct
from dataclasses import dataclass
from typing import Optional

SECTOR = 1024
STREAM_START = 0x800
IMAGE_SIZE = 1_261_568
APOLLO_MAGIC = b"APOLLO"

FLAG_WHOLE = 0
FLAG_FIRST = 1
FLAG_MIDDLE = 2
FLAG_LAST = 3
FLAG_TAPEMARK = 4

FILLER_WORD = b"\x00\x06"


class StreamError(ValueError):
    """Raised when the image cannot be parsed as an Apollo tape stream."""


@dataclass
class FrameEvent:
    """One L1 event: a record, tapemark, filler run, stale sector, or gap."""

    offset: int
    kind: str  # "record" | "tapemark" | "filler" | "stale" | "gap"
    payload: bytes = b""
    note: str = ""


class FrameParser:
    """L1: walks the floppy-as-tape stream from 0x800, yielding records.

    Records are assembled from segment runs (whole, or first {middle} last)
    with the event offset at the record's first segment.  Parsing stops at
    EOT (two consecutive tapemarks); ``eot_offset`` then points just past
    the second tapemark (the start of the untouched tail).  If the medium
    ends without an EOT, ``eot_offset`` stays None.

    Tapemark counting for EOT survives filler and gaps (the writer may pad
    to a sector boundary between the two closing tapemarks); only a data
    record resets the run, mirroring the reference implementation.
    """

    def __init__(self, data: bytes):
        if len(data) < STREAM_START + SECTOR:
            raise StreamError(f"Image too small: {len(data)}")
        self.data = data
        self.eot_offset: Optional[int] = None
        self.events: list[FrameEvent] = []

    @staticmethod
    def _next_boundary(pos: int) -> int:
        return (pos // SECTOR + 1) * SECTOR

    def _segment_at(self, pos: int) -> Optional[tuple[int, bytes, int]]:
        """Validate a segment at pos; return (flags, payload, next_pos) or None.

        A valid segment has 12 framing bytes available, flags <= 4 (with a
        tapemark requiring len 0), lies entirely within the 1024-byte sector
        containing pos, and a trailer mirroring the header (len, flags).
        """
        data = self.data
        if pos + 12 > len(data):
            return None
        flags, length = struct.unpack_from(">HI", data, pos)
        if flags > FLAG_TAPEMARK:
            return None
        if flags == FLAG_TAPEMARK and length != 0:
            return None
        end = pos + 6 + length
        if end + 6 > self._next_boundary(pos):  # would cross the sector boundary
            return None
        if end + 6 > len(data):
            return None
        if struct.unpack_from(">IH", data, end) != (length, flags):
            return None
        return flags, bytes(data[pos + 6 : end]), end + 6

    def parse(self) -> list[FrameEvent]:
        """Walk the stream from STREAM_START; return the event list."""
        data = self.data
        pos = STREAM_START
        tapemark_run = 0
        record_start: Optional[int] = None
        record_parts: list[bytes] = []

        def gap(offset: int, note: str) -> int:
            """Emit a gap event, drop any open record, resync to boundary."""
            nonlocal record_start, record_parts
            self.events.append(FrameEvent(offset, "gap", note=note))
            record_start = None
            record_parts = []
            return self._next_boundary(pos)

        while pos + 2 <= len(data):
            segment = self._segment_at(pos)
            if segment is not None:
                flags, payload, next_pos = segment

                if flags == FLAG_TAPEMARK:
                    if record_parts:
                        pos = gap(record_start, "record interrupted by tapemark")
                        continue
                    self.events.append(FrameEvent(pos, "tapemark"))
                    tapemark_run += 1
                    pos = next_pos
                    if tapemark_run == 2:
                        self.eot_offset = pos
                        return self.events
                    continue

                if flags in (FLAG_WHOLE, FLAG_FIRST):
                    if record_parts:
                        pos = gap(record_start, "record restarted before last")
                        continue
                    tapemark_run = 0
                    if flags == FLAG_WHOLE:
                        self.events.append(FrameEvent(pos, "record", payload))
                    else:
                        record_start = pos
                        record_parts = [payload]
                    pos = next_pos
                    continue

                # FLAG_MIDDLE or FLAG_LAST
                if not record_parts:
                    pos = gap(pos, "continuation segment without first")
                    continue
                record_parts.append(payload)
                if flags == FLAG_LAST:
                    self.events.append(
                        FrameEvent(record_start, "record", b"".join(record_parts))
                    )
                    record_start = None
                    record_parts = []
                pos = next_pos
                continue

            if data[pos : pos + 2] == FILLER_WORD:
                run_start = pos
                boundary = self._next_boundary(pos)
                while pos + 2 <= boundary and data[pos : pos + 2] == FILLER_WORD:
                    pos += 2
                note = "" if pos == boundary else "short filler run"
                self.events.append(FrameEvent(run_start, "filler", note=note))
                pos = boundary
                continue

            pos = gap(pos, "unparseable bytes")

        return self.events
