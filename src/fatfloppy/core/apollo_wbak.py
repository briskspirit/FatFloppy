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

Writer pathologies (spec section 2.2, recovery verified against all real
disks by the reference implementation):

- **stale sector**: between records one whole sector retains OLD medium
  content -- often old *well-framed* segments, so detection cannot be
  purely structural.  Every assembled record must *classify* (ANSI label
  tag, or wbak block whose seq/uid chain matches expectations); a
  structurally valid but unexpected sector is skipped as ``stale``.
- **absorbed stale**: a record whose head ended exactly at a sector
  boundary may absorb a stale sector holding valid middle framing and
  assemble 1012 bytes too long; recovery drops sector-aligned full-sector
  middles (earliest first) until the record classifies.
- **lost middles**: the writer never wrote k middles (medium contiguous,
  record k*1012 short); the lost middles are the FIRST k after the head,
  reconstructed as zero-fill with the event flagged damaged.
- **drop-on-miss**: near EOV trailers garbage sectors interleave; the
  unparseable sector is skipped (``gapsec``) and one pending middle is
  zero-filled at its in-order position.

Classification is pluggable via ``FrameParser(data, classifier=...)``; the
default classifier accepts ANSI labels by tag and wbak blocks by the
seq/uid chain maintained in the parser (``expected_seq``/``expected_uid``).
"""

import struct
from dataclasses import dataclass
from itertools import combinations
from typing import Callable, Optional

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

BLOCK_SIZE = 8192  # logical wbak data block (L3)
MIDDLE_SIZE = SECTOR - 12  # payload of a middle segment filling a sector
LABEL_SIZE = 80  # ANSI X3.27 label record
LABEL_TAGS = (
    b"VOL1",
    b"UVL1",
    b"HDR1",
    b"HDR2",
    b"UHL1",
    b"EOF1",
    b"EOF2",
    b"EOV1",
    b"EOV2",
    b"UTL1",
)

_SUB_RECORD_TYPE = (9, 1)  # every block starts with an L4 SUB record
_MAX_ABSORBED_DROPS = 3  # absorbed-stale middles dropped per record
_MAX_GAP_SECTORS = 8  # unparseable sectors skipped inside one record


class StreamError(ValueError):
    """Raised when the image cannot be parsed as an Apollo tape stream."""


@dataclass
class FrameEvent:
    """One L1 event.

    Kinds: ``record`` (payload + classifier category, possibly with damage
    ranges), ``tapemark``, ``filler``, ``stale`` (skipped stale sector or
    dropped absorbed-stale middle), ``gapsec`` (unparseable sector skipped
    inside a record, drop-on-miss), ``badrec`` (well-framed but
    unclassifiable record stepped over), ``gap`` (unparseable mid-sector
    bytes, resync at the next boundary).
    """

    offset: int
    kind: str
    payload: bytes = b""
    note: str = ""
    category: str = ""  # classifier verdict for records: "label" | "block"
    damage: tuple[tuple[int, int], ...] = ()

    @property
    def damaged(self) -> bool:
        return bool(self.damage)


Classifier = Callable[["FrameParser", bytes, Optional[int]], Optional[str]]


def default_classifier(
    parser: "FrameParser", payload: bytes, head_len: Optional[int] = None
) -> Optional[str]:
    """Record-level validation (the reference implementation's ``classify``).

    - 80-byte payloads bearing a known ANSI label tag are labels.
    - With no chain expectation yet (section start), a block must look
      structurally like one: 14-byte header with a sane bh_size followed
      by a SUB (9/1) record.  A damaged head (< 22 bytes) cannot
      establish a chain.
    - Chained blocks validate on the seq/uid chain: u32BE@0 equals the
      expected sequence and bytes 4:12 equal the backup UID -- 10+ exact
      bytes, strong enough to reject stale sectors from older runs.  A
      record whose head segment lost the block header (head_len < 22)
      still validates on the chain alone (12 bytes suffice).
    """
    if len(payload) == LABEL_SIZE and payload[:4] in LABEL_TAGS:
        return "label"
    damaged_head = head_len is not None and head_len < 22
    if parser.expected_seq is None:
        if damaged_head or len(payload) < 22:
            return None
        used, type1, _size, type2 = struct.unpack_from(">HHHH", payload, 12)
        if (type1, type2) == _SUB_RECORD_TYPE and 14 <= used <= BLOCK_SIZE:
            return "block"
        return None
    if len(payload) < (12 if damaged_head else 22):
        return None
    (seq,) = struct.unpack_from(">I", payload, 0)
    if seq == parser.expected_seq and payload[4:12] == parser.expected_uid:
        return "block"
    return None


class FrameParser:
    """L1: walks the floppy-as-tape stream from 0x800, yielding records.

    Records are assembled from segment runs (whole, or first {middle} last)
    with the event offset at the record's first segment, then validated by
    the ``classifier`` and repaired per the writer-pathology rules (see the
    module docstring).  Parsing stops at EOT (two consecutive tapemarks);
    ``eot_offset`` then points just past the second tapemark (the start of
    the untouched tail).  If the medium ends without an EOT, ``eot_offset``
    stays None.

    Tapemark counting for EOT survives filler, stale sectors and gaps (the
    writer may pad to a sector boundary between the two closing tapemarks);
    only an emitted record resets the run, mirroring the reference
    implementation.

    The default classifier tracks the wbak block chain: the first block
    after a section start establishes ``expected_uid`` and the sequence
    base; each accepted block advances ``expected_seq``; an HDR1 label
    resets the chain for the next section.
    """

    def __init__(self, data: bytes, classifier: Optional[Classifier] = None):
        if len(data) < STREAM_START + SECTOR:
            raise StreamError(f"Image too small: {len(data)}")
        self.data = data
        self.eot_offset: Optional[int] = None
        self.events: list[FrameEvent] = []
        self.expected_seq: Optional[int] = None
        self.expected_uid: Optional[bytes] = None
        self._classifier: Classifier = classifier or default_classifier

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

    def _classify(
        self, payload: bytes, head_len: Optional[int] = None
    ) -> Optional[str]:
        return self._classifier(self, payload, head_len)

    def _collect(
        self, pos: int
    ) -> Optional[tuple[list[tuple[int, bytes]], list[int], int]]:
        """Collect a first {middle} last segment chain starting at pos.

        Unparseable whole sectors mid-chain are skipped and recorded as gap
        sectors (drop-on-miss, up to ``_MAX_GAP_SECTORS``).  Returns
        (segments, gap_offsets, next_pos) with segments as a list of
        (medium offset, payload), or None if the chain never completes.
        """
        segment = self._segment_at(pos)
        if segment is None:
            return None
        _flags, payload, p = segment
        segments = [(pos, payload)]
        gap_offsets: list[int] = []
        while True:
            segment = self._segment_at(p)
            if segment is None:
                if (
                    p % SECTOR == 0
                    and len(gap_offsets) < _MAX_GAP_SECTORS
                    and p + SECTOR <= len(self.data)
                ):
                    gap_offsets.append(p)
                    p += SECTOR
                    continue
                return None
            flags, payload, next_p = segment
            if flags not in (FLAG_MIDDLE, FLAG_LAST):
                return None
            segments.append((p, payload))
            p = next_p
            if flags == FLAG_LAST:
                return segments, gap_offsets, p

    def _resolve(
        self, segments: list[tuple[int, bytes]]
    ) -> tuple[Optional[list[tuple[int, bytes]]], Optional[list[int]], Optional[str]]:
        """Drop absorbed-stale middles until the record classifies.

        Candidates are sector-aligned full-sector (1012-byte) middles after
        the head; combinations are tried smallest-count first and earliest
        first (stale sectors follow record joins, so the first candidate is
        the likeliest).  Totals exceeding one block (8192 bytes) can never
        validate, which is what forces the drop for an absorbed stale.
        Returns (kept_segments, dropped_offsets, category) or a None triple.
        """
        total = sum(len(p) for _, p in segments)
        candidates = [
            i
            for i, (off, payload) in enumerate(segments)
            if i > 0 and off % SECTOR == 0 and len(payload) == MIDDLE_SIZE
        ]
        max_drop = min(len(candidates), _MAX_ABSORBED_DROPS)
        for count in range(max_drop + 1):
            if total - MIDDLE_SIZE * count > BLOCK_SIZE:
                continue  # still too long to be a block
            for combo in combinations(candidates, count):
                kept = [s for i, s in enumerate(segments) if i not in combo]
                payload = b"".join(p for _, p in kept)
                category = self._classify(payload, head_len=len(kept[0][1]))
                if category is not None:
                    return kept, [segments[i][0] for i in combo], category
        return None, None, None

    def _reconstruct(
        self, segments: list[tuple[int, bytes]], gap_offsets: "list[int]"
    ) -> tuple[bytes, tuple[tuple[int, int], ...], str]:
        """Rebuild the canonical 8192-byte block from surviving segments.

        Returns (payload, damage_ranges, note).  Damage model (verified by
        the reference implementation):

        - no medium gaps (contiguous segments, k middles missing): buffer
          level loss at the record join -- the lost middles are the FIRST
          k after the head; the survivors are the last ones.
        - gap sectors seen while collecting: one pending middle was dropped
          per missed sector, at its in-order medium position.
        """
        total = sum(len(p) for _, p in segments)
        if total >= BLOCK_SIZE or len(segments) < 2:
            return b"".join(p for _, p in segments), (), ""
        head = segments[0][1]
        tail = segments[-1][1]
        middles = segments[1:-1]
        space = BLOCK_SIZE - len(head) - len(tail)
        if space % MIDDLE_SIZE or any(len(p) != MIDDLE_SIZE for _, p in middles):
            # does not fit the canonical block tiling: keep as-is, mark it
            joined = b"".join(p for _, p in segments)
            return joined, ((0, 0),), "irregular short record"
        slot_count = space // MIDDLE_SIZE
        lost = slot_count - len(middles)
        slots: list[Optional[bytes]] = [None] * slot_count
        if gap_offsets and len(gap_offsets) >= lost:
            # drop-on-miss: replay the medium order, leaving a hole at
            # each missed sector's in-order position
            order = sorted(
                [(off, payload) for off, payload in middles]
                + [(off, None) for off in gap_offsets],
                key=lambda item: item[0],
            )
            for index, (_off, payload) in enumerate(order):
                if index >= slot_count:
                    break  # more gaps than holes: ignore the excess
                slots[index] = payload  # None leaves a zero-filled hole
        else:
            # buffer-level loss: the FIRST `lost` middles are missing
            for i, (_off, payload) in enumerate(middles):
                slots[lost + i] = payload
        out = bytearray(head)
        damage: list[tuple[int, int]] = []
        for slot in slots:
            if slot is None:
                damage.append((len(out), MIDDLE_SIZE))
                out += bytes(MIDDLE_SIZE)
            else:
                out += slot
        out += tail
        note = f"zero-filled {len(damage)} middles" if damage else ""
        return bytes(out), tuple(damage), note

    def _emit(
        self,
        offset: int,
        payload: bytes,
        category: str,
        damage: tuple[tuple[int, int], ...] = (),
        note: str = "",
    ) -> None:
        """Append a record event and advance the block chain state."""
        if category == "label":
            damage = ()  # labels are atomic 80-byte units
            note = ""
        self.events.append(
            FrameEvent(
                offset,
                "record",
                payload,
                note=note,
                category=category,
                damage=damage,
            )
        )
        if category == "label":
            if payload[:4] == b"HDR1":
                # a new file section starts: the block chain restarts
                self.expected_seq = None
                self.expected_uid = None
            return
        if len(payload) >= 12:
            (seq,) = struct.unpack_from(">I", payload, 0)
            self.expected_seq = seq + 1
            self.expected_uid = bytes(payload[4:12])

    def parse(self) -> list[FrameEvent]:
        """Walk the stream from STREAM_START; return the event list."""
        data = self.data
        pos = STREAM_START
        tapemark_run = 0

        while pos + 2 <= len(data):
            if data[pos : pos + 2] == FILLER_WORD:
                run_start = pos
                boundary = self._next_boundary(pos)
                while pos + 2 <= boundary and data[pos : pos + 2] == FILLER_WORD:
                    pos += 2
                note = "" if pos == boundary else "short filler run"
                self.events.append(FrameEvent(run_start, "filler", note=note))
                pos = boundary
                continue

            segment = self._segment_at(pos)
            if segment is not None:
                flags, payload, next_pos = segment

                if flags == FLAG_TAPEMARK:
                    self.events.append(FrameEvent(pos, "tapemark"))
                    tapemark_run += 1
                    pos = next_pos
                    if tapemark_run == 2:
                        self.eot_offset = pos
                        return self.events
                    continue

                if flags == FLAG_WHOLE:
                    category = self._classify(payload)
                    if category is not None:
                        self._emit(pos, payload, category)
                        tapemark_run = 0
                        pos = next_pos
                        continue
                    # structurally valid but unexpected content: fall
                    # through to the stale-sector / gap handling below

                elif flags == FLAG_FIRST:
                    collected = self._collect(pos)
                    if collected is not None:
                        segments, gap_offsets, next_pos = collected
                        kept, dropped, category = self._resolve(segments)
                        if kept is not None:
                            for off in gap_offsets:
                                self.events.append(
                                    FrameEvent(
                                        off,
                                        "gapsec",
                                        note="lost-write sector skipped",
                                    )
                                )
                            for off in dropped or []:
                                self.events.append(
                                    FrameEvent(
                                        off,
                                        "stale",
                                        note="absorbed stale middle dropped",
                                    )
                                )
                            block, damage, note = self._reconstruct(kept, gap_offsets)
                            self._emit(pos, block, category, damage=damage, note=note)
                            tapemark_run = 0
                            pos = next_pos
                            continue
                        if pos % SECTOR != 0:
                            # well-framed but unclassifiable record
                            # (trailer-area debris): note it and step past
                            total = sum(len(p) for _, p in segments)
                            self.events.append(
                                FrameEvent(
                                    pos,
                                    "badrec",
                                    note=f"unclassified record len={total}",
                                )
                            )
                            tapemark_run = 0
                            pos = next_pos
                            continue

                # FLAG_MIDDLE / FLAG_LAST without an open record, or a
                # record attempt that failed: handled below

            if pos % SECTOR == 0:
                # stale sector of old medium content between records
                self.events.append(
                    FrameEvent(pos, "stale", note="stale sector skipped")
                )
                pos += SECTOR
            else:
                self.events.append(FrameEvent(pos, "gap", note="unparseable bytes"))
                pos = self._next_boundary(pos)

        return self.events
