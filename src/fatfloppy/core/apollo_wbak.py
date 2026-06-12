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

Above L1 the module implements (spec sections 2.3, 2.4 and 4):

- **L2**: :class:`AnsiLabel` -- the 80-byte ANSI X3.27 / ECMA-13 labels
  (``VOL1 UVL1 (HDR1 HDR2 UHL1 TM data TM (EOF1|EOV1)(EOF2|EOV2) TM)*``).
- **L3**: 8192-byte data blocks -- 14-byte header (u32BE seq, u64BE backup
  UID, u16BE bh_size; the latter is unreliable, records self-terminate).
- **L4**: 6-byte-magic object records (SUB/MARK/NAME/FILE/DATA/DIR/POPD/
  LINK; OPT/EMPTY/ACL skipped) consumed into a :class:`WbakCatalog` of
  :class:`WbakTree`/:class:`WbakEntry` by :func:`build_catalog`.  The
  catalog is a *single-disk view*: tree sections continuing from a
  previous volume keep their orphan head data out of the entry list,
  retained verbatim in :attr:`WbakTree.orphan_tail` so a cross-volume
  reassembly (:func:`stitch_tree`) can complete the file cut at the
  previous volume's EOV.
"""

import copy
import logging
import struct
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from typing import Callable, Optional

from .physical_format import PhysicalFormat, TrackFormat

logger = logging.getLogger(__name__)

SECTOR = 1024
STREAM_START = 0x800
IMAGE_SIZE = 1_261_568
APOLLO_MAGIC = b"APOLLO"

# Fixed geometry shared by the driver and the format profile (single
# source of truth): 77 cylinders x 2 heads x 8 sectors x 1024 bytes,
# MFM 500 kb/s, 360 rpm.
CYLINDERS = 77
HEADS = 2
SECTORS_PER_TRACK = 8
RPM = 360
RATE_KBPS = 500


def build_apollo_physical_format() -> PhysicalFormat:
    """Build the fixed PhysicalFormat for Apollo DOMAIN floppies (77x2x8x1024)."""
    tf = TrackFormat(
        track_start=0,
        track_end=CYLINDERS - 1,
        head_start=0,
        head_end=HEADS - 1,
        sectors_per_track=SECTORS_PER_TRACK,
        encoding="MFM",
        rate=RATE_KBPS,
        interleave=1,
        bytes_per_sector=SECTOR,
        id_start=0,
        iam_present=True,
    )
    return PhysicalFormat(
        cylinders=CYLINDERS,
        heads=HEADS,
        rpm=RPM,
        heads_inverted=False,
        bytes_per_sector=SECTOR,
        track_formats=[tf],
    )


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

    ``damage`` lists the zero-filled (payload_offset, length) ranges of a
    reconstructed record.  The sentinel ``((0, 0),)`` marks an *irregular*
    short record whose segments do not fit the canonical block tiling: the
    payload is the surviving bytes joined as-is, the loss cannot be
    located, and consumers must treat the whole record as damaged (the
    catalog does; it derives no hole extents from the sentinel).

    ``spans`` maps payload ranges back to the medium as (payload_offset,
    image_offset, length) tuples; zero-filled ranges have no span.

    ``length`` is the byte count consumed by ``filler`` events (0 for
    other kinds).
    """

    offset: int
    kind: str
    payload: bytes = b""
    note: str = ""
    category: str = ""  # classifier verdict for records: "label" | "block"
    damage: tuple[tuple[int, int], ...] = ()
    spans: tuple[tuple[int, int, int], ...] = ()
    length: int = 0

    @property
    def damaged(self) -> bool:
        return bool(self.damage)


Classifier = Callable[["FrameParser", bytes, Optional[int]], Optional[str]]


def _block_header_ok(payload: bytes) -> bool:
    """Structural block-header check: sane bh_size and a SUB (9/1) first."""
    if len(payload) < 22:
        return False
    used, type1, _size, type2 = struct.unpack_from(">HHHH", payload, 12)
    return (type1, type2) == _SUB_RECORD_TYPE and 14 <= used <= BLOCK_SIZE


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
      still validates on the chain alone (12 bytes suffice); a chained
      block that fails the structural SUB check returns ``block-chain``
      so the emitted event is noted as chain-only validated.
    """
    if len(payload) == LABEL_SIZE and payload[:4] in LABEL_TAGS:
        return "label"
    damaged_head = head_len is not None and head_len < 22
    if parser.expected_seq is None:
        if damaged_head or not _block_header_ok(payload):
            return None
        return "block"
    if len(payload) < (12 if damaged_head else 22):
        return None
    (seq,) = struct.unpack_from(">I", payload, 0)
    if seq == parser.expected_seq and payload[4:12] == parser.expected_uid:
        return "block" if _block_header_ok(payload) else "block-chain"
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

    @staticmethod
    def _join_with_spans(
        segments: list[tuple[int, bytes]],
    ) -> tuple[bytes, tuple[tuple[int, int, int], ...]]:
        """Concatenate segment payloads, recording their medium spans."""
        spans = []
        cursor = 0
        for off, payload in segments:
            spans.append((cursor, off + 6, len(payload)))
            cursor += len(payload)
        return b"".join(p for _, p in segments), tuple(spans)

    def _reconstruct(
        self, segments: list[tuple[int, bytes]], gap_offsets: list[int]
    ) -> tuple[
        bytes, tuple[tuple[int, int], ...], tuple[tuple[int, int, int], ...], str
    ]:
        """Rebuild the canonical 8192-byte block from surviving segments.

        Returns (payload, damage_ranges, spans, note).  Damage model
        (verified by the reference implementation):

        - no medium gaps (contiguous segments, k middles missing): buffer
          level loss at the record join -- the lost middles are the FIRST
          k after the head; the survivors are the last ones.
        - gap sectors seen while collecting: one pending middle was dropped
          per missed sector, at its in-order medium position.  When the
          gap sectors outnumber the missing slots, the in-order replay
          fills every slot before the medium item list runs out and the
          excess trailing items are dropped -- including middles displaced
          past the last slot, which are debris by construction (a record
          can never carry more middles than its zero-filled holes admit).
        """
        total = sum(len(p) for _, p in segments)
        if total >= BLOCK_SIZE or len(segments) < 2:
            payload, spans = self._join_with_spans(segments)
            return payload, (), spans, ""
        head_off, head = segments[0]
        tail_off, tail = segments[-1]
        middles = segments[1:-1]
        space = BLOCK_SIZE - len(head) - len(tail)
        if space % MIDDLE_SIZE or any(len(p) != MIDDLE_SIZE for _, p in middles):
            # does not fit the canonical block tiling: keep as-is, mark it
            # with the ((0, 0)) sentinel (see FrameEvent.damage)
            payload, spans = self._join_with_spans(segments)
            return payload, ((0, 0),), spans, "irregular short record"
        slot_count = space // MIDDLE_SIZE
        lost = slot_count - len(middles)
        slots: list[Optional[tuple[int, bytes]]] = [None] * slot_count
        if gap_offsets and len(gap_offsets) >= lost:
            # drop-on-miss: replay the medium order, leaving a hole at
            # each missed sector's in-order position
            order = sorted(
                [(off, payload) for off, payload in middles]
                + [(off, None) for off in gap_offsets],
                key=lambda item: item[0],
            )
            for index, (off, payload) in enumerate(order):
                if index >= slot_count:
                    break  # excess items dropped (see docstring)
                # a gap (payload None) leaves a zero-filled hole
                slots[index] = None if payload is None else (off, payload)
        else:
            # buffer-level loss: the FIRST `lost` middles are missing
            for i, (off, payload) in enumerate(middles):
                slots[lost + i] = (off, payload)
        out = bytearray(head)
        spans = [(0, head_off + 6, len(head))]
        damage: list[tuple[int, int]] = []
        for slot in slots:
            if slot is None:
                damage.append((len(out), MIDDLE_SIZE))
                out += bytes(MIDDLE_SIZE)
            else:
                off, payload = slot
                spans.append((len(out), off + 6, len(payload)))
                out += payload
        spans.append((len(out), tail_off + 6, len(tail)))
        out += tail
        note = f"zero-filled {len(damage)} middles" if damage else ""
        return bytes(out), tuple(damage), tuple(spans), note

    def _emit(
        self,
        offset: int,
        payload: bytes,
        category: str,
        damage: tuple[tuple[int, int], ...] = (),
        spans: tuple[tuple[int, int, int], ...] = (),
        note: str = "",
    ) -> None:
        """Append a record event and advance the block chain state."""
        if category == "block-chain":
            # accepted on the seq/uid chain alone (structural SUB check
            # failed, e.g. the head segment lost the block header)
            category = "block"
            note = f"{note}; chain-only validation" if note else "chain-only validation"
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
                spans=spans,
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
                self.events.append(
                    FrameEvent(
                        run_start, "filler", note=note, length=boundary - run_start
                    )
                )
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
                        self._emit(
                            pos,
                            payload,
                            category,
                            spans=((0, pos + 6, len(payload)),),
                        )
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
                            block, damage, spans, note = self._reconstruct(
                                kept, gap_offsets
                            )
                            self._emit(
                                pos,
                                block,
                                category,
                                damage=damage,
                                spans=spans,
                                note=note,
                            )
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


# ====================================================================== L2

APOLLO_EPOCH_UNIX = 315_532_800  # 1980-01-01T00:00:00Z
_APOLLO_UNITS_PER_SECOND = 3.814697265625  # 250000 4us ticks / 2**16


def apollo_time_to_datetime(hi32: int) -> datetime:
    """Convert an Apollo timestamp (high 32 bits of the 4-microsecond tick
    counter since 1980-01-01 UTC, i.e. ticks >> 16) to an aware datetime.

    ``unix = hi32 / 3.814697265625 + 315532800`` (spec section 2.4).
    """
    unix = hi32 / _APOLLO_UNITS_PER_SECOND + APOLLO_EPOCH_UNIX
    return datetime.fromtimestamp(unix, tz=timezone.utc)


def _label_text(raw: bytes, position: int, length: int) -> str:
    """ECMA-13 field access by 1-based byte position."""
    return raw[position - 1 : position - 1 + length].decode("ascii", "replace")


def _label_int(raw: bytes, position: int, length: int) -> Optional[int]:
    text = _label_text(raw, position, length).strip()
    return int(text) if text.isdigit() else None


def _label_date(raw: bytes, position: int) -> Optional[date]:
    """Decode an ECMA-13 ' YYDDD' date field; junk decodes to None."""
    text = _label_text(raw, position, 6)
    year_text, day_text = text[1:3], text[3:6]
    if not (year_text.isdigit() and day_text.isdigit()):
        return None
    day = int(day_text)
    if not 1 <= day <= 366:
        return None
    return date(1900 + int(year_text), 1, 1) + timedelta(days=day - 1)


@dataclass
class AnsiLabel:
    """One decoded 80-byte ANSI X3.27 / ECMA-13 label (spec section 2.3).

    Field availability depends on ``kind``; fields a label type does not
    carry stay None.  Numeric fields are junk-tolerant: non-digit content
    decodes to None instead of raising (real media exist with destroyed
    trailer labels, e.g. disk10's EOF1).
    """

    kind: str
    # VOL1
    volume_id: Optional[str] = None
    owner: Optional[str] = None
    # HDR1 / EOF1 / EOV1
    file_id: Optional[str] = None
    set_id: Optional[str] = None
    section: Optional[int] = None
    sequence: Optional[int] = None
    created: Optional[date] = None
    block_count: Optional[int] = None
    # HDR2 / EOF2 / EOV2
    record_format: Optional[str] = None
    block_len: Optional[int] = None
    record_len: Optional[int] = None
    # UVL1 / UHL1 / UTL1
    uid_text: Optional[str] = None
    date_text: Optional[str] = None  # UHL1 "YYYY/MM/DD"
    time_text: Optional[str] = None  # UHL1 "HH:MM:SS"

    @staticmethod
    def parse(raw: bytes) -> "AnsiLabel":
        if len(raw) != LABEL_SIZE or raw[:4] not in LABEL_TAGS:
            raise ValueError(f"Not an ANSI label: {raw[:4]!r}")
        kind = raw[:4].decode("ascii")
        label = AnsiLabel(kind)
        if kind == "VOL1":
            label.volume_id = _label_text(raw, 5, 6).strip()
            label.owner = _label_text(raw, 38, 14).strip()
        elif kind in ("UVL1", "UHL1", "UTL1"):
            tokens = _label_text(raw, 5, 76).split()
            label.uid_text = tokens[0] if tokens else None
            if kind == "UHL1":
                label.date_text = tokens[1] if len(tokens) > 1 else None
                label.time_text = tokens[2] if len(tokens) > 2 else None
        elif kind in ("HDR1", "EOF1", "EOV1"):
            label.file_id = _label_text(raw, 5, 17).strip()
            label.set_id = _label_text(raw, 22, 6).strip()
            label.section = _label_int(raw, 28, 4)
            label.sequence = _label_int(raw, 32, 4)
            label.created = _label_date(raw, 42)
            label.block_count = _label_int(raw, 55, 6)
        elif kind in ("HDR2", "EOF2", "EOV2"):
            label.record_format = _label_text(raw, 5, 1)
            label.block_len = _label_int(raw, 6, 5)
            label.record_len = _label_int(raw, 11, 5)
        return label


# =================================================================== L3/L4

# (type1, type2) object record types, SR9.5 (spec section 2.4)
REC_FILE = (0, 1)
REC_DATA = (1, 1)
REC_NAME = (2, 1)
REC_DIR = (3, 2)
REC_POPD = (4, 1)
REC_LINK = (5, 1)
REC_OPT = (6, 1)
REC_MARK = (8, 1)  # object-start delimiter; also the EMPTY filler
REC_SUB = (9, 1)
REC_ACL = (10, 1)

_FILE_MAGIC = b"\x00\x00\x90\x00"
_DIR_MAGIC = b"\x00\x01\x90\x00"
_STORAGE_HEADER_MAGIC = b"\x00\x20\x00\x01"
STORAGE_HEADER_SIZE = 32
_MAX_DATA_CHUNK = BLOCK_SIZE - 14  # the most a block could ever hold
_MARK_BYTES = b"\x00\x08\x00\x04\x00\x01\x00\x00\x00\x00"  # a full MARK record


def decode_wbak_name(raw: bytes) -> str:
    """Decode a stored wbak name (rbak rule): uppercase becomes lowercase;
    a ``:X`` escape yields the literal character X (uppercase preserved).
    """
    text = raw.decode("latin-1")
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == ":" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(char.lower())
            i += 1
    return "".join(out)


def _printable(chunk: bytes) -> bool:
    return all(31 < byte < 127 for byte in chunk)


def _record_valid(payload: bytes, pos: int, lim: int, want_data: int = 0) -> bool:
    """Strict structural validity of an object-record header at ``pos``.

    ``want_data`` > 0 admits DATA records (a file is open and incomplete);
    DATA is the riskiest type to accept blindly after junk.
    """
    if pos + 6 > lim:
        return False
    type1, size, type2 = struct.unpack_from(">HHH", payload, pos)
    end = pos + 6 + size
    if type1 == 8:  # MARK / EMPTY
        return type2 == 1 and size == 4 and payload[pos + 6 : pos + 10] == bytes(4)
    if type1 == 9:  # SUB
        return type2 == 1 and 12 < size < 256
    if type1 == 2:  # NAME: u64 uid | u32 0 | printable relpath
        if not (type2 == 1 and 12 < size < 1000 and end <= lim):
            return False
        if payload[pos + 14 : pos + 18] != bytes(4):
            return False
        return _printable(payload[pos + 18 : end])
    if type1 == 0:  # FILE: 64 bytes, magic 0x00009000
        return type2 == 1 and size == 64 and payload[pos + 6 : pos + 10] == _FILE_MAGIC
    if type1 == 3:  # DIR: >= 80-byte attrs, magic 0x00019000, name
        return (
            type2 == 2
            and 80 <= size < 1100
            and payload[pos + 6 : pos + 10] == _DIR_MAGIC
            and (size == 80 or _printable(payload[pos + 86 : end]))
        )
    if type1 == 4:  # POPD: u32 0 + printable parent path
        return (
            type2 == 1
            and 4 <= size < 1000
            and end <= lim
            and payload[pos + 6 : pos + 10] == bytes(4)
            and _printable(payload[pos + 10 : end])
        )
    if type1 == 5:  # LINK: u32 0, u16 pathlen, path, target
        if not (type2 == 1 and 6 < size < 2000 and end <= lim):
            return False
        if payload[pos + 6 : pos + 10] != bytes(4):
            return False
        (path_len,) = struct.unpack_from(">H", payload, pos + 10)
        return path_len + 6 <= size and _printable(payload[pos + 12 : end])
    if type1 == 1:  # DATA: the claimed size may overrun the block end
        return type2 == 1 and 0 < size <= _MAX_DATA_CHUNK and want_data > 0
    if type1 in (6, 10):  # OPT / ACL
        return type2 in (1, 2) and size < 4096
    return False


def _resync(payload: bytes, pos: int, lim: int, want_data: int = 0) -> Optional[int]:
    """Scan even offsets for the next valid record start.

    DATA gets one-step chaining: it is accepted only when followed by the
    block end or another valid record.  Returns the offset or None.
    """
    cursor = pos + (pos & 1)
    while cursor + 6 <= lim:
        (type1,) = struct.unpack_from(">H", payload, cursor)
        if type1 in (8, 2, 0, 3, 4, 5) and _record_valid(payload, cursor, lim):
            return cursor
        if type1 == 1 and want_data and _record_valid(payload, cursor, lim, want_data):
            (size,) = struct.unpack_from(">H", payload, cursor + 2)
            after = cursor + 6 + size + (size & 1)
            if (
                after >= lim - 1
                or _record_valid(payload, after, lim, want_data)
                or _record_valid(payload, after, lim)
            ):
                return cursor
        cursor += 2
    return None


def _recover_clipped_name(junk: bytes, uid: bytes) -> Optional[bytes]:
    """Recover a NAME record's path from a clipped-NAME junk remnant.

    One damage class is mechanically recoverable (proven on disk2, block
    seq 25 at image offset 0x03041C): a NAME record lost its leading
    bytes -- the 6-byte record header plus the first bytes of its 8-byte
    object UID -- so the tiler junk-skips the surviving remnant and
    resyncs on the very next FILE record.  The remnant then reads

        [uid tail (1..7 bytes)] [u32 zero] [printable path]

    where the uid tail equals the LAST bytes of the FILE record's UID
    (``uid``): the NAME and FILE records of one object carry the same
    UID, so the overlap proves the remnant is that object's clipped NAME.

    Tails shorter than 4 bytes are rejected: a 4-byte tail already pins
    32 exact bits against the adjacent FILE record's UID, and with the
    mandatory 32-bit zero word and the printable-path requirement the
    accidental-match chance on arbitrary junk is negligible (>= 64 exact
    bits) -- while 1..3-byte tails are plausible by chance in the
    zero-heavy junk real disks carry (68k code, zero-padded text debris:
    disk3/disk4/disk8).  Tails are tried longest first so an ambiguous
    repeated-byte UID resolves to the most-evidence split.  A single
    trailing NUL is tolerated and stripped (the writer pads odd-length
    records to even).  Returns the raw path bytes, or None (no proof).
    """
    for tail_len in range(7, 3, -1):  # 7..4, longest (strongest) first
        if len(junk) < tail_len + 4 + 1:
            continue
        if junk[:tail_len] != uid[8 - tail_len :]:
            continue
        if junk[tail_len : tail_len + 4] != bytes(4):
            continue
        path = junk[tail_len + 4 :]
        if path.endswith(b"\x00"):
            path = path[:-1]  # the even-length pad byte
        if path and _printable(path):
            return bytes(path)
    return None


def _embedded_object_start(
    payload: bytes, body_off: int, body: bytes, lim: int
) -> Optional[int]:
    """Find an embedded object start inside an abandoned DATA span.

    The writer may abandon a DATA chunk mid-way and start the next object
    (MARK marker, or a POPD group) inside the claimed span.  Returns the
    body-relative offset of the embedded start, or None.
    """
    rel = 0
    while True:
        found = body.find(_MARK_BYTES, rel)
        if found < 0:
            break
        if found % 2 == 0 and _record_valid(payload, body_off + found + 10, lim):
            return found
        rel = found + 2
    rel = 0
    while True:  # POPD group: 0004 size 0001 00000000 <printable>
        found = body.find(b"\x00\x04", rel)
        if found < 0 or found + 6 > len(body):
            break
        if found % 2 == 0 and _record_valid(payload, body_off + found, lim):
            (size,) = struct.unpack_from(">H", body, found + 2)
            after = body_off + found + 6 + size + (size & 1)
            if _record_valid(payload, after, lim):
                return found
        rel = found + 2
    return None


def _tile_block(
    payload: bytes, want_data_fn: Optional[Callable[[], int]] = None
) -> list[tuple]:
    """Tile one block's interior into object records with junk resync.

    Returns ``("rec", offset, type1, type2, body, claimed_size)`` and
    ``("junk", offset, length)`` items.  ``claimed_size`` may exceed
    ``len(body)`` when a DATA chunk is cut by the block end (the file
    continues in the next block's DATA record; the missing claimed bytes
    were never written).  A DATA chunk abandoned mid-way with the next
    object's records embedded inside its claimed span is truncated at the
    embedded object start.
    """
    lim = min(len(payload), BLOCK_SIZE)
    want = want_data_fn or (lambda: 0)
    items: list[tuple] = []
    pos = 14
    while pos + 6 <= lim:
        type1, size, type2 = struct.unpack_from(">HHH", payload, pos)
        # sequential tiling accepts DATA even with no file open (the
        # tiling is authoritative); the want-context guards resync only
        if not _record_valid(payload, pos, lim, want_data=max(want(), 1)):
            found = _resync(payload, pos, lim, want_data=want())
            items.append(("junk", pos, (lim if found is None else found) - pos))
            if found is None:
                break
            pos = found
            continue
        body = payload[pos + 6 : min(pos + 6 + size, lim)]
        if (type1, type2) == REC_DATA:
            embedded = _embedded_object_start(payload, pos + 6, body, lim)
            if embedded is not None:
                items.append(("rec", pos, type1, type2, body[:embedded], embedded))
                pos = pos + 6 + embedded
                continue
        items.append(("rec", pos, type1, type2, body, size))
        pos += 6 + size + (size & 1)
    return items


@dataclass
class _ObjectHeader:
    """SR9.5 FILE/DIR attribute header.

    Field positions verified against the reference implementation's
    actual unpacking (``decode_file_header``; its docstring differs
    subtly -- the code wins): u32 magic; u64 uid; then 8 x u32 at offset
    12: type, zero, acl, zero, size (incl. the 32-byte storage header),
    blocks (1024-byte pages), time1 (mtime), time2 (atime); u64 parent
    uid at offset 44; 3 x u32 spare.
    """

    uid: bytes
    type_code: int
    size: int
    blocks: int
    mtime_raw: int
    atime_raw: int
    parent_uid: bytes


def _decode_object_header(body: bytes) -> Optional[_ObjectHeader]:
    if len(body) < 52:
        return None
    type_code, _z1, _acl, _z2, size, blocks, time1, time2 = struct.unpack_from(
        ">8I", body, 12
    )
    return _ObjectHeader(
        uid=bytes(body[4:12]),
        type_code=type_code,
        size=size,
        blocks=blocks,
        mtime_raw=time1,
        atime_raw=time2,
        parent_uid=bytes(body[44:52]),
    )


def _extents_for_range(
    spans: tuple[tuple[int, int, int], ...], start: int, end: int
) -> list[tuple[int, int]]:
    """Map a payload byte range to medium (image_offset, length) extents."""
    extents = []
    for payload_off, image_off, length in spans:
        lo = max(start, payload_off)
        hi = min(end, payload_off + length)
        if lo < hi:
            extents.append((image_off + (lo - payload_off), hi - lo))
    return extents


# ================================================================= catalog


@dataclass
class WbakEntry:
    """One object from a backup tree (file, directory or symbolic link).

    ``path`` is the decoded (lowercased, ``:X`` unescaped) name relative
    to the tree: the stored path's leading ``file_id`` component is
    stripped when present and the tree root directory itself gets path
    "".  Objects whose NAME record was destroyed keep the reference
    implementation's placeholder "?" -- unless the junk remnant directly
    preceding the FILE record proves the clipped NAME's path via UID
    overlap (:func:`_recover_clipped_name`); ``name_recovered`` marks
    such entries, whose size, damage semantics and content are untouched
    by the recovery.

    ``size`` is the content size in bytes: the FILE header's size minus
    the 32-byte storage header, floored at 0.

    ``extents`` lists the medium-backed (image_offset, length) byte
    ranges of the raw DATA chunks in stream order, including the storage
    header bytes -- for disk-map / file-allocation use.  Zero-filled
    losses have no medium bytes and are omitted; ``damage_notes`` carries
    them instead.

    ``damage_notes`` holds detail tuples mirroring the reference
    implementation: ``("zerofill", block_seq, data_offset, length)`` for
    a chunk cut at a block end, ``("hole", block_seq, data_offset,
    length)`` for content overlapping an L1 zero-filled range, and
    ``("irregular", block_seq)`` for content fed from an irregular short
    block (whole record damaged, no locatable holes).

    ``partial`` marks the file that was still open when its tree section
    ended without an EOF trailer (cut by end-of-volume); reading it
    yields the available prefix.  ``raw_data`` is the assembled raw
    object stream (storage header included), kept verbatim for
    :meth:`WbakCatalog.read`.
    """

    path: str
    is_dir: bool = False
    size: int = 0
    mtime: Optional[datetime] = None
    atime: Optional[datetime] = None
    damaged: bool = False
    partial: bool = False
    link_target: Optional[str] = None
    raw_name: bytes = b""
    name_recovered: bool = False
    extents: list[tuple[int, int]] = field(default_factory=list)
    damage_notes: list[tuple] = field(default_factory=list)
    raw_data: bytes = field(default=b"", repr=False)


@dataclass
class WbakTree:
    """One HDR1..EOF1/EOV1 tree section as seen on this disk.

    ``complete`` is True only for an EOF trailer; EOV (or a missing
    trailer) means the tree continues on another volume.  For sections
    with ``section >= 2`` (this disk continues a tree started elsewhere)
    ``continued_from_previous`` is set and the orphan head data arriving
    before the first NAME/FILE record -- the tail of the file cut at the
    previous volume's EOV -- is retained in ``orphan_tail`` (it has no
    owning entry on *this* disk) with its damage detail in
    ``orphan_damage``, using the same note format as
    :attr:`WbakEntry.damage_notes`.  :func:`stitch_tree` consumes it.
    Sections whose HDR1 section number was destroyed (decoded as 0) get
    the same treatment when their stream opens with ownerless DATA.

    ``first_block_seq``/``last_block_seq`` record the wbak block sequence
    range fed to this tree; block numbering continues across volumes
    within a tree, so a continuation should start at
    ``last_block_seq + 1`` (checked, warn-only, by :func:`stitch_tree`).
    """

    file_id: str
    section: int
    sequence: int
    complete: bool
    uid_text: Optional[str] = None
    created: Optional[date] = None
    entries: list[WbakEntry] = field(default_factory=list)
    root_path: Optional[str] = None  # backup source path from the SUB record
    block_count: int = 0
    continued_from_previous: bool = False
    orphan_tail: bytes = field(default=b"", repr=False)
    orphan_damage: list[tuple] = field(default_factory=list)
    first_block_seq: Optional[int] = None
    last_block_seq: Optional[int] = None


@dataclass
class WbakCatalog:
    """Single-disk view of a wbak backup volume.

    ``created`` is decoded from the backup UID's timestamp word (the
    UVL1 text ``"%8x.%8x"``); VOL1 itself carries no date.

    ``frame_events`` retains the full L1 event list and ``eot_found``
    whether the stream terminated with an EOT -- consumers (the
    filesystem's disk map and consistency check) need them without
    re-parsing the image.
    """

    volume_id: Optional[str] = None
    owner: Optional[str] = None
    created: Optional[datetime] = None
    backup_uid: Optional[str] = None
    trees: list[WbakTree] = field(default_factory=list)
    eot_found: bool = False
    frame_events: list[FrameEvent] = field(default_factory=list, repr=False)

    def read(self, entry: WbakEntry) -> bytes:
        """Assemble a file entry's content.

        Strips the 32-byte storage header when present and truncates to
        the declared size.  Zero-filled gaps are already embedded in the
        assembled data; partial entries (cut by end-of-volume) yield the
        available prefix.  Links have no content (b"").
        """
        if entry.is_dir:
            raise IsADirectoryError(entry.path)
        data = entry.raw_data
        if data[:4] == _STORAGE_HEADER_MAGIC:
            data = data[STORAGE_HEADER_SIZE:]
        return bytes(data[: entry.size])


def _uid_datetime(uid_text: Optional[str]) -> Optional[datetime]:
    """Decode the timestamp word of a "%8x.%8x" backup/tree UID."""
    if not uid_text:
        return None
    head = uid_text.split(".")[0]
    if not 1 <= len(head) <= 8:
        return None
    try:
        return apollo_time_to_datetime(int(head, 16))
    except ValueError:
        return None


class _TreeAccumulator:
    """L4 object-stream consumer building one tree's entries.

    Mirrors the reference implementation's ``Tree`` (file state machine:
    NAME announces, FILE opens, DATA appends, MARK/POPD/next-object
    closes) plus the catalog semantics from spec section 4 (decoded
    tree-relative paths, damage/partial flags, orphan-tail retention for
    continuing sections, medium extents).
    """

    def __init__(self, tree: WbakTree):
        self.tree = tree
        self.cur: Optional[WbakEntry] = None
        self.cur_buf = bytearray()
        self.cur_size = 0  # raw FILE size (storage header included)
        self.cur_need = 0  # max(size, blocks * 1024)
        self.pending_name: Optional[bytes] = None
        self.saw_object = False  # NAME/FILE/DIR/LINK seen: orphan head over
        self.orphan_buf = bytearray()  # synced to tree.orphan_tail at finalize

    # -- helpers

    def want_data(self) -> int:
        """Remaining byte count of the open file (DATA resync context).

        During a continuing section's orphan head the cut file's declared
        size lives on the previous volume: any amount of continuation
        DATA is welcome (matches the reference's cross-volume chain mode,
        where the file is still open and its want is genuinely positive).
        """
        if self.cur is not None:
            return max(0, self.cur_need - len(self.cur_buf))
        if self._in_orphan_head():
            return _MAX_DATA_CHUNK
        return 0

    def _in_orphan_head(self) -> bool:
        """True while a continuing section's head data has no owner here."""
        return (
            self.tree.continued_from_previous or self.tree.section == 0
        ) and not self.saw_object

    def _object_seen(self) -> None:
        """First NAME/FILE/DIR/LINK record: the orphan head (if any) ends."""
        self.saw_object = True

    def _orphan_data(
        self,
        block_seq: int,
        off: int,
        body: bytes,
        claimed: int,
        damage: tuple[tuple[int, int], ...],
        irregular: bool,
    ) -> None:
        """Retain a continuing section's ownerless head DATA chunk.

        Same zero-fill/damage accounting as owned-file assembly, except
        the zero-fill pad for a chunk cut by the block end is uncapped:
        the cut file's declared size is unknown on this volume and
        :func:`stitch_tree` truncates the stitched data to it.  No
        extents are recorded -- the tail's bytes belong to *this* image,
        not the primary volume the stitched tree will be viewed under.
        """
        start = off + 6
        if irregular:
            note = ("irregular", block_seq)
            if note not in self.tree.orphan_damage:
                self.tree.orphan_damage.append(note)
        else:
            for hole_off, hole_len in damage:
                lo = max(start, hole_off)
                hi = min(start + len(body), hole_off + hole_len)
                if lo < hi:
                    self.tree.orphan_damage.append(
                        (
                            "hole",
                            block_seq,
                            len(self.orphan_buf) + (lo - start),
                            hi - lo,
                        )
                    )
        self.orphan_buf += body
        if len(body) < claimed:
            pad = claimed - len(body)
            self.tree.orphan_damage.append(
                ("zerofill", block_seq, len(self.orphan_buf), pad)
            )
            self.orphan_buf += bytes(pad)

    def _relative_path(self, raw_name: bytes) -> str:
        decoded = decode_wbak_name(raw_name)
        prefix = decode_wbak_name(self.tree.file_id.encode("ascii", "replace"))
        if decoded == prefix:
            return ""  # the tree root directory itself
        if prefix and decoded.startswith(prefix + "/"):
            return decoded[len(prefix) + 1 :]
        return decoded

    def close(self) -> None:
        """Finish the open file entry and append it to the tree."""
        if self.cur is None:
            return
        entry = self.cur
        entry.raw_data = bytes(self.cur_buf)
        entry.damaged = bool(entry.damage_notes) or len(entry.raw_data) < self.cur_size
        self.tree.entries.append(entry)
        self.cur = None
        self.cur_buf = bytearray()
        self.cur_size = 0
        self.cur_need = 0

    def finalize(self, complete: bool) -> None:
        """Close the section.  The file still open at an EOV (or missing)
        trailer was cut by the end of volume: flag it partial, and judge
        its damage on its notes alone (shortness is expected)."""
        open_entry = self.cur
        cut_short = self.cur is not None and len(self.cur_buf) < self.cur_size
        self.close()
        self.tree.complete = complete
        self.tree.orphan_tail = bytes(self.orphan_buf)
        if not complete and open_entry is not None and cut_short:
            open_entry.partial = True
            open_entry.damaged = bool(open_entry.damage_notes)

    # -- record consumption

    def feed_block(self, event: FrameEvent) -> None:
        """Tile one block event and feed its records."""
        payload = event.payload
        self.tree.block_count += 1
        block_seq = struct.unpack_from(">I", payload, 0)[0] if len(payload) >= 4 else 0
        if self.tree.first_block_seq is None:
            self.tree.first_block_seq = block_seq
        self.tree.last_block_seq = block_seq
        irregular = event.damage == ((0, 0),)
        damage = () if irregular else event.damage
        prev_junk: Optional[tuple[int, int]] = None
        for item in _tile_block(payload, self.want_data):
            if item[0] != "rec":
                # junk run: skipped by the tiler, but remembered -- a run
                # ending exactly at a FILE record may be a clipped NAME
                prev_junk = (item[1], item[2])
                continue
            _kind, off, type1, type2, body, claimed = item
            junk_before = b""
            if (
                prev_junk is not None
                and (type1, type2) == REC_FILE
                and prev_junk[0] + prev_junk[1] == off
            ):
                junk_before = bytes(payload[prev_junk[0] : off])
            prev_junk = None
            self._feed(
                block_seq,
                off,
                type1,
                type2,
                body,
                claimed,
                event.spans,
                damage,
                irregular,
                junk_before=junk_before,
            )

    def _feed(
        self,
        block_seq: int,
        off: int,
        type1: int,
        type2: int,
        body: bytes,
        claimed: int,
        spans: tuple[tuple[int, int, int], ...],
        damage: tuple[tuple[int, int], ...],
        irregular: bool,
        junk_before: bytes = b"",
    ) -> None:
        kind = (type1, type2)
        if kind == REC_SUB:
            if self.tree.root_path is None and len(body) >= 12:
                (path_len,) = struct.unpack_from(">H", body, 10)
                self.tree.root_path = body[12 : 12 + path_len].decode("latin-1")
            return
        if kind == REC_MARK:  # object delimiter (and the EMPTY filler)
            self.close()
            return
        if kind == REC_NAME:
            self._object_seen()
            self.pending_name = bytes(body[12:])
            return
        if kind == REC_FILE:
            self._object_seen()
            self.close()
            header = _decode_object_header(body)
            raw_name = self.pending_name
            name_recovered = False
            if raw_name is None and junk_before and len(body) >= 12:
                # no NAME arrived and junk directly precedes this FILE
                # record: it may be the clipped NAME's remnant, provable
                # via UID overlap (see _recover_clipped_name)
                raw_name = _recover_clipped_name(junk_before, bytes(body[4:12]))
                name_recovered = raw_name is not None
                if name_recovered:
                    logger.info(
                        "wbak block %d: recovered clipped NAME %r via UID overlap",
                        block_seq,
                        raw_name,
                    )
            if raw_name is None:
                raw_name = b"?"
            raw_size = header.size if header else 0
            self.cur = WbakEntry(
                path=self._relative_path(raw_name),
                size=max(0, raw_size - STORAGE_HEADER_SIZE),
                mtime=apollo_time_to_datetime(header.mtime_raw) if header else None,
                atime=apollo_time_to_datetime(header.atime_raw) if header else None,
                raw_name=raw_name,
                name_recovered=name_recovered,
            )
            self.cur_buf = bytearray()
            self.cur_size = raw_size
            self.cur_need = max(raw_size, (header.blocks if header else 0) * 1024)
            self.pending_name = None
            return
        if kind == REC_DATA:
            if self.cur is None:
                if self._in_orphan_head():
                    self._orphan_data(block_seq, off, body, claimed, damage, irregular)
                return
            entry = self.cur
            start = off + 6
            entry.extents += _extents_for_range(spans, start, start + len(body))
            if irregular:
                # irregular short block: the whole record is damaged and
                # the loss cannot be located -- no hole extents
                note = ("irregular", block_seq)
                if note not in entry.damage_notes:
                    entry.damage_notes.append(note)
            else:
                for hole_off, hole_len in damage:
                    lo = max(start, hole_off)
                    hi = min(start + len(body), hole_off + hole_len)
                    if lo < hi:
                        entry.damage_notes.append(
                            (
                                "hole",
                                block_seq,
                                len(self.cur_buf) + (lo - start),
                                hi - lo,
                            )
                        )
            self.cur_buf += body
            if len(body) < claimed:
                # chunk cut by the block end: the claimed remainder was
                # never written; zero-fill (capped at what the file still
                # needs) so later chunks land at the right offsets
                pad = min(claimed - len(body), self.want_data())
                if pad > 0:
                    entry.damage_notes.append(
                        ("zerofill", block_seq, len(self.cur_buf), pad)
                    )
                    self.cur_buf += bytes(pad)
            return
        if kind == REC_DIR:
            self._object_seen()
            self.close()
            header = _decode_object_header(body)
            name = bytes(body[80:]) if len(body) > 80 else b""
            raw_name = name or self.pending_name or b"?"
            self.tree.entries.append(
                WbakEntry(
                    path=self._relative_path(raw_name),
                    is_dir=True,
                    mtime=(
                        apollo_time_to_datetime(header.mtime_raw) if header else None
                    ),
                    atime=(
                        apollo_time_to_datetime(header.atime_raw) if header else None
                    ),
                    raw_name=raw_name,
                )
            )
            self.pending_name = None
            return
        if kind == REC_LINK:
            self._object_seen()
            self.close()
            path_len = struct.unpack_from(">H", body, 4)[0] if len(body) >= 6 else 0
            raw_name = bytes(body[6 : 6 + path_len])
            target = body[6 + path_len :].decode("latin-1", "replace")
            self.tree.entries.append(
                WbakEntry(
                    path=self._relative_path(raw_name),
                    link_target=target,
                    raw_name=raw_name,
                )
            )
            self.pending_name = None
            return
        if kind == REC_POPD:
            self.close()
            return
        # OPT / ACL / unknown types: skipped, like real rbak


def build_catalog(data: bytes) -> WbakCatalog:
    """Parse a whole wbak floppy image into a :class:`WbakCatalog`.

    Walks the L1 frame events: ANSI labels drive the volume/tree state,
    block records are tiled into object records and consumed per tree.
    An APOLLO container without a wbak tape stream (e.g. an AEGIS-native
    filesystem) yields an EMPTY catalog (``volume_id`` None, no trees)
    rather than raising; :class:`StreamError` is still raised for
    truncated images.
    """
    parser = FrameParser(data)
    events = parser.parse()
    catalog = WbakCatalog(eot_found=parser.eot_offset is not None, frame_events=events)
    accumulator: Optional[_TreeAccumulator] = None

    def finish_section(complete: bool) -> None:
        nonlocal accumulator
        if accumulator is not None:
            accumulator.finalize(complete)
            accumulator = None

    for event in events:
        if event.kind != "record":
            continue
        if event.category == "label":
            label = AnsiLabel.parse(event.payload)
            if label.kind == "VOL1":
                catalog.volume_id = label.volume_id
                catalog.owner = label.owner
            elif label.kind == "UVL1":
                catalog.backup_uid = label.uid_text
                catalog.created = _uid_datetime(label.uid_text)
            elif label.kind == "HDR1":
                finish_section(False)  # missing trailer: not complete
                tree = WbakTree(
                    file_id=label.file_id or "",
                    section=label.section or 0,
                    sequence=label.sequence or 0,
                    complete=False,
                    created=label.created,
                    continued_from_previous=(label.section or 0) >= 2,
                )
                catalog.trees.append(tree)
                accumulator = _TreeAccumulator(tree)
            elif label.kind == "UHL1" and accumulator is not None:
                accumulator.tree.uid_text = label.uid_text
            elif label.kind in ("EOF1", "EOV1"):
                finish_section(label.kind == "EOF1")
            # HDR2 / EOF2 / EOV2 / UTL1 carry no catalog state
        elif accumulator is not None:
            accumulator.feed_block(event)
    finish_section(False)
    return catalog


# ===================================================== cross-volume stitching


@dataclass(frozen=True)
class ContinuationSpec:
    """Identity of the volume expected to continue an incomplete tree.

    ``backup_uid`` is the PER-TREE UHL1 uid (:attr:`WbakTree.uid_text`),
    the cross-volume stitching invariant.  The per-volume UVL1 uid is NOT
    invariant -- every volume of a set stamps its own (FT0003: disk8's
    UVL1 differs from disk1's while the COM tree's UHL1 is identical on
    both) -- so it must never be used for validation.  ``volume_id`` is
    informational: each floppy carries its own VOL1 id.
    """

    file_id: str
    sequence: int
    next_section: int
    backup_uid: Optional[str]
    volume_id: Optional[str]


def expected_continuation(
    tree: WbakTree, catalog: WbakCatalog
) -> Optional[ContinuationSpec]:
    """The continuation an EOV-cut tree expects on the next volume.

    Returns None for complete trees.  ``catalog`` must be the catalog
    the tree was built from (supplies the volume id).  The spec's uid is
    the tree's own UHL1 uid -- the cross-volume invariant; the catalog's
    per-volume UVL1 uid differs across a set's volumes.
    """
    if tree.complete:
        return None
    return ContinuationSpec(
        file_id=tree.file_id,
        sequence=tree.sequence,
        next_section=tree.section + 1,
        backup_uid=tree.uid_text,
        volume_id=catalog.volume_id,
    )


def stitch_tree(
    prev: WbakTree,
    cont: WbakTree,
    *,
    prev_uid: Optional[str],
    cont_uid: Optional[str],
) -> WbakTree:
    """Merge a continuation section into its predecessor.

    Pure: returns a NEW tree, mutating neither input.  Validation (in
    order: backup uid when both sides know theirs, file_id, sequence,
    section == prev.section + 1) raises :class:`ValueError` naming the
    offending identity.  A block-sequence discontinuity between the two
    sides is logged as a warning, never an error: real disks lose
    trailers, the ANSI labels are authoritative.

    Merge semantics: the cut entry (prev's single ``partial`` file; at
    most one exists by construction) has its raw data extended with
    ``cont.orphan_tail``, truncated to the FILE record's declared size --
    ``partial`` clears iff the size is reached, damage flags OR.  When
    prev has no partial entry the cut fell between files and the tail
    must be empty (warn and discard otherwise).  The continuation's own
    entries are appended; their extents (and the tail's bytes) live on
    the continuation volume's image, not the primary one the merged tree
    is viewed under, so stitched-in content carries NO extents -- the
    disk map stays primary-volume-only.  ``complete`` is taken from the
    continuation (EOF ends the tree, EOV expects yet another volume).
    """
    if prev.complete:
        raise ValueError(
            f"Tree {prev.file_id!r} sequence {prev.sequence} is already "
            "complete; nothing to stitch"
        )
    if prev_uid is not None and cont_uid is not None and prev_uid != cont_uid:
        raise ValueError(f"Wrong volume: backup UID {cont_uid} (expected {prev_uid})")
    if cont.file_id != prev.file_id:
        raise ValueError(
            f"Wrong continuation: file_id {cont.file_id!r} (expected {prev.file_id!r})"
        )
    if cont.sequence != prev.sequence:
        raise ValueError(
            f"Wrong continuation: sequence {cont.sequence} (expected {prev.sequence})"
        )
    if cont.section != prev.section + 1:
        raise ValueError(
            f"Wrong continuation: section {cont.section} "
            f"(expected section {prev.section + 1} of {prev.file_id!r})"
        )
    if (
        prev.last_block_seq is not None
        and cont.first_block_seq is not None
        and cont.first_block_seq != prev.last_block_seq + 1
    ):
        logger.warning(
            "wbak stitch %r section %d: block sequence discontinuity "
            "(%d -> %d); labels are authoritative, proceeding",
            prev.file_id,
            cont.section,
            prev.last_block_seq,
            cont.first_block_seq,
        )

    entries = copy.deepcopy(prev.entries)
    partials = [e for e in entries if e.partial]
    if len(partials) > 1:
        raise ValueError(
            f"Tree {prev.file_id!r} has {len(partials)} partial entries; "
            "expected at most one (the file cut at end-of-volume)"
        )
    if partials:
        _extend_cut_entry(partials[0], cont)
    elif cont.orphan_tail:
        logger.warning(
            "wbak stitch %r section %d: %d-byte orphan tail but no partial "
            "predecessor entry; tail discarded",
            prev.file_id,
            cont.section,
            len(cont.orphan_tail),
        )
    for entry in cont.entries:
        appended = copy.deepcopy(entry)
        appended.extents = []  # bytes live on the continuation volume
        entries.append(appended)

    return WbakTree(
        file_id=prev.file_id,
        section=cont.section,  # a further continuation must be section + 1
        sequence=prev.sequence,
        complete=cont.complete,
        uid_text=prev.uid_text or cont.uid_text,
        created=prev.created or cont.created,
        entries=entries,
        root_path=prev.root_path or cont.root_path,
        block_count=prev.block_count + cont.block_count,
        continued_from_previous=prev.continued_from_previous,
        orphan_tail=prev.orphan_tail,  # prev's own unowned head, if any
        orphan_damage=list(prev.orphan_damage),
        first_block_seq=(
            prev.first_block_seq
            if prev.first_block_seq is not None
            else cont.first_block_seq
        ),
        last_block_seq=(
            cont.last_block_seq
            if cont.last_block_seq is not None
            else prev.last_block_seq
        ),
    )


def _extend_cut_entry(entry: WbakEntry, cont: WbakTree) -> None:
    """Extend the EOV-cut entry (a stitch-local copy) with the tail.

    The declared raw size is reconstructed as ``entry.size + 32``
    (:class:`WbakEntry` stores the FILE record's size minus the storage
    header).  Tail damage notes are rebased onto the entry's raw-data
    offsets; notes falling entirely past the truncation point are
    dropped with the excess tail bytes they describe.
    """
    declared_raw = entry.size + STORAGE_HEADER_SIZE
    base = len(entry.raw_data)
    added_damage = False
    for note in cont.orphan_damage:
        if note[0] in ("hole", "zerofill"):
            kind, block_seq, tail_off, length = note
            start = base + tail_off
            if start >= declared_raw:
                continue
            entry.damage_notes.append(
                (kind, block_seq, start, min(length, declared_raw - start))
            )
        else:  # ("irregular", block_seq): whole-block damage, no offsets
            entry.damage_notes.append(note)
        added_damage = True
    entry.raw_data = (entry.raw_data + cont.orphan_tail)[:declared_raw]
    entry.partial = len(entry.raw_data) < declared_raw
    entry.damaged = entry.damaged or added_damage
