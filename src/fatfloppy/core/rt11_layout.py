"""
Shared on-disk structure definitions for DEC RT-11 volumes.

Neutral core module (like cbm_layout.py): the RT-11 filesystem and any
view-aware driver code import from here. It bundles:

- the RAD50 (Radix-50) character codec used for file names,
- the RT-11 directory date word codec,
- the physical<->logical "view" mappers for raw floppy sector images
  (RX01/RX02/RX50), transcribed from the original DEC driver algorithms
  (RT-11 V04 DY.MAC for RX01/RX02, P/OS V3.2 DZDRV.MAC for RX50, both as
  quoted verbatim in PUTR.ASM),
- home block / directory segment parsing helpers, and
- the directory structure scorer used for format detection.

RT-11 volumes are always addressed in 512-byte logical blocks: block 0 is
the boot block, block 1 the home block, blocks 2-5 are reserved, and the
directory segments normally start at block 6. Raw images of 8-inch floppies
(RX01: 77x26x128, RX02: 77x26x256) and RX50s (80x10x512) store *physical*
sectors, which the DEC handlers spread with a 2:1 interleave plus a track
skew; a "view" maps logical block numbers onto those physical sectors.

Conventions:

- All words are little-endian.
- ``logical_block_to_chs`` returns 0-based (cylinder, head, sector) tuples,
  matching FatFloppy's 0-based logical sector addressing. The DEC formulas
  produce 1-based sector numbers; the conversion happens here, internally.
- RAD50 code 29 decodes to the true ``%`` character. PUTR prints it as
  ``?``, but the DEC documentation defines the charset column as ``$ . %``
  and we keep the real character.
- The home block checksum is stored at offset 0o776, but real DEC factory
  disks routinely ship with a WRONG checksum; it is exposed for diagnostics
  and must never be load-bearing for detection.
"""

import struct
from dataclasses import dataclass, field
from typing import Callable, Optional

BLOCK_SIZE = 512
HOME_BLOCK = 1
DEFAULT_DIR_START = 6
SEGMENT_BLOCKS = 2  # a directory segment is always two 512-byte blocks
SEGMENT_SIZE = SEGMENT_BLOCKS * BLOCK_SIZE
SEGMENT_HEADER_WORDS = 5
ENTRY_BASE_BYTES = 14  # 7 words: status, name x2, type, length, job/chan, date
MAX_SEGMENTS = 31

# Directory entry status word bits (octal, per the V&FF manual table 1-3).
E_PRE = 0o000020  # prefix block(s) present (low byte = file class)
E_TENT = 0o000400  # tentative file
E_MPTY = 0o001000  # empty area (free-list entry)
E_PERM = 0o002000  # permanent file
E_EOS = 0o004000  # end-of-segment marker (may be a bare final word)
E_READ = 0o040000  # write-protected by monitor
E_PROT = 0o100000  # protected permanent file

_KNOWN_STATUS_BITS = E_TENT | E_MPTY | E_PERM | E_EOS | E_READ | E_PROT

# ---------------------------------------------------------------------------
# RAD50 codec
# ---------------------------------------------------------------------------

# DEC Radix-50 (PDP-11 flavour): 40 characters, three per 16-bit word,
# word = c1*1600 + c2*40 + c3. Order: space, A-Z, '$', '.', '%', 0-9.
RAD50_CHARSET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ$.%0123456789"
RAD50_MAX_WORD = 39 * 1600 + 39 * 40 + 39  # "999" = 63999


def rad50_is_valid(word: int) -> bool:
    """Return True if ``word`` is a representable RAD50 word (<= 63999)."""
    return 0 <= word <= RAD50_MAX_WORD


def rad50_decode(word: int) -> str:
    """Decode one RAD50 word into its three characters.

    Raises ValueError for words above 63999 (not representable).
    """
    if not rad50_is_valid(word):
        raise ValueError(f"Invalid RAD50 word {word} (max {RAD50_MAX_WORD})")
    c1, rest = divmod(word, 1600)
    c2, c3 = divmod(rest, 40)
    return RAD50_CHARSET[c1] + RAD50_CHARSET[c2] + RAD50_CHARSET[c3]


def rad50_encode(text: str) -> int:
    """Encode up to three characters into one RAD50 word (space padded).

    Raises ValueError for strings longer than three characters or
    containing characters outside the RAD50 charset.
    """
    if len(text) > 3:
        raise ValueError(f"RAD50 encodes at most 3 characters, got {text!r}")
    word = 0
    for char in text.ljust(3):
        code = RAD50_CHARSET.find(char)
        if code < 0:
            raise ValueError(f"Character {char!r} not in the RAD50 charset")
        word = word * 40 + code
    return word


# ---------------------------------------------------------------------------
# Date word codec
# ---------------------------------------------------------------------------

_DATE_BASE_YEAR = 1972
_DATE_AGE_SPAN = 32  # each age bit increment extends the epoch by 32 years
_DATE_MAX_YEAR = _DATE_BASE_YEAR + 4 * _DATE_AGE_SPAN - 1  # age 3, year 31


def decode_date_word(word: int) -> Optional[tuple[int, int, int]]:
    """Decode an RT-11 date word to (year, month, day).

    Layout: age(2 bits)<<14 | month<<10 | day<<5 | (year-1972-32*age).
    Returns None for 0 (no date) and for garbage month/day fields.
    """
    if word == 0:
        return None
    age = (word >> 14) & 0o3
    month = (word >> 10) & 0o17
    day = (word >> 5) & 0o37
    year = (word & 0o37) + _DATE_BASE_YEAR + _DATE_AGE_SPAN * age
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    return year, month, day


def encode_date_word(year: int, month: int, day: int) -> int:
    """Encode (year, month, day) into an RT-11 date word.

    Years >= 2004 are represented with the age bits (base years 1972, 2004,
    2036, 2068). Raises ValueError outside 1972-2099 or for invalid
    month/day.
    """
    if not (_DATE_BASE_YEAR <= year <= _DATE_MAX_YEAR):
        raise ValueError(f"Year {year} outside {_DATE_BASE_YEAR}-{_DATE_MAX_YEAR}")
    if not (1 <= month <= 12):
        raise ValueError(f"Month {month} outside 1-12")
    if not (1 <= day <= 31):
        raise ValueError(f"Day {day} outside 1-31")
    age, year_field = divmod(year - _DATE_BASE_YEAR, _DATE_AGE_SPAN)
    return (age << 14) | (month << 10) | (day << 5) | year_field


# ---------------------------------------------------------------------------
# Physical <-> logical view mappers
# ---------------------------------------------------------------------------

VIEWS = ("logical", "rx01", "rx02", "rx50")


@dataclass(frozen=True)
class ViewGeometry:
    """Raw-image geometry for one physical view."""

    tracks: int
    sectors_per_track: int
    bytes_per_sector: int
    total_blocks: int  # device capacity in 512-byte logical blocks

    @property
    def sectors_per_block(self) -> int:
        return BLOCK_SIZE // self.bytes_per_sector

    @property
    def image_size(self) -> int:
        return self.tracks * self.sectors_per_track * self.bytes_per_sector


# RX01/RX02 device capacity excludes physical track 0 (the DEC scheme skips
# it): 76 tracks x 26 sectors. RX50 uses all 80 tracks (track 0 wraps to the
# end of the logical order).
VIEW_GEOMETRY = {
    "rx01": ViewGeometry(77, 26, 128, 76 * 26 * 128 // BLOCK_SIZE),  # 494
    "rx02": ViewGeometry(77, 26, 256, 76 * 26 * 256 // BLOCK_SIZE),  # 988
    "rx50": ViewGeometry(80, 10, 512, 80 * 10),  # 800
}


def _rx_interleave(lsn: int, nsect: int) -> tuple[int, int]:
    """RX01/RX02 sector map, from RT-11 V04 DY.MAC (via PUTR.ASM):

        ISEC=(ISEC-1)*2
        IF(ISEC.GE.26) ISEC=ISEC-25
        ISEC=MOD(ISEC+ITRK*6,26)+1
        ITRK=ITRK+1

    2:1 interleave, 6-sector track-to-track skew, track offset 1 (physical
    track 0 is never used by the filesystem). ``lsn`` is the 0-based
    sequential sector index of the logical block stream; returns a 1-based
    (track, sector) pair.
    """
    itrk, isec = divmod(lsn, nsect)
    isec *= 2
    if isec >= nsect:
        isec -= nsect - 1
    isec = (isec + itrk * 6) % nsect + 1
    return itrk + 1, isec


def _rx50_interleave(lsn: int) -> tuple[int, int]:
    """RX50 sector map, from P/OS V3.2 DZDRV.MAC (via PUTR.ASM):

        ISEC=(ISEC-1)*2
        IF(ISEC.GE.10) ISEC=ISEC-9
        ISEC=MOD(ISEC+ITRK*2,10)+1
        ITRK=MOD(ITRK+1,80)

    2:1 interleave, 2-sector track-to-track skew, track offset 1 WITH
    wraparound: physical track 0 becomes the *last* logical track, so all
    80 tracks are used. (This is NOT the DEC Rainbow RX50 scheme.)
    """
    itrk, isec = divmod(lsn, 10)
    isec *= 2
    if isec >= 10:
        isec -= 9
    isec = (isec + itrk * 2) % 10 + 1
    itrk = (itrk + 1) % 80
    return itrk, isec


def logical_block_to_chs(
    view: str,
    block: int,
    sectors_per_track: Optional[int] = None,
    heads: int = 1,
) -> list[tuple[int, int, int]]:
    """Map one 512-byte logical block to its physical sectors under a view.

    Returns the ordered list of 0-based (cylinder, head, sector) tuples
    whose concatenated contents form the block (4 sectors for rx01, 2 for
    rx02, 1 for rx50/logical).

    The "logical" view is the identity over a uniform 512-byte-sector
    geometry and requires ``sectors_per_track`` (and ``heads`` if > 1);
    sectors fill head 0 of a cylinder, then head 1, then the next cylinder.
    """
    if view == "logical":
        if sectors_per_track is None or sectors_per_track <= 0 or heads <= 0:
            raise ValueError("logical view requires a positive sectors_per_track")
        if block < 0:
            raise ValueError(f"Block {block} out of range")
        cylinder, rest = divmod(block, sectors_per_track * heads)
        head, sector = divmod(rest, sectors_per_track)
        return [(cylinder, head, sector)]
    geometry = VIEW_GEOMETRY.get(view)
    if geometry is None:
        raise ValueError(f"Unknown view {view!r} (expected one of {VIEWS})")
    if not 0 <= block < geometry.total_blocks:
        raise ValueError(
            f"Block {block} out of range 0-{geometry.total_blocks - 1} for {view}"
        )
    result = []
    for index in range(geometry.sectors_per_block):
        lsn = block * geometry.sectors_per_block + index
        if view == "rx50":
            track, sector = _rx50_interleave(lsn)
        else:
            track, sector = _rx_interleave(lsn, geometry.sectors_per_track)
        result.append((track, 0, sector - 1))
    return result


# ---------------------------------------------------------------------------
# Home block / directory segment parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HomeBlock:
    """Decoded fields of the home block (logical block 1)."""

    pack_cluster_size: int
    dir_start: int
    system_version: Optional[str]  # decoded RAD50, None if unrepresentable
    volume_id: str  # 12 ASCII chars
    owner: str  # 12 ASCII chars
    system_id: str  # 12 ASCII chars ("DECRT11A    " on RT-11 volumes)
    checksum_stored: int
    checksum_computed: int  # often != stored on real DEC factory disks


def parse_home_block(data: bytes) -> HomeBlock:
    """Parse the home block fields (offsets per the V&FF manual table 1-1)."""
    if len(data) < BLOCK_SIZE:
        raise ValueError(f"Home block needs {BLOCK_SIZE} bytes, got {len(data)}")

    def word(offset: int) -> int:
        return struct.unpack_from("<H", data, offset)[0]

    def text(offset: int, length: int) -> str:
        return data[offset : offset + length].decode("ascii", "replace")

    version_word = word(0o726)
    return HomeBlock(
        pack_cluster_size=word(0o722),
        dir_start=word(0o724),
        system_version=(
            rad50_decode(version_word) if rad50_is_valid(version_word) else None
        ),
        volume_id=text(0o730, 12),
        owner=text(0o744, 12),
        system_id=text(0o760, 12),
        checksum_stored=word(0o776),
        checksum_computed=sum(struct.unpack_from("<255H", data, 0)) & 0xFFFF,
    )


@dataclass(frozen=True)
class SegmentHeader:
    """The five-word directory segment header."""

    total_segments: int  # valid range 1-31
    next_segment: int  # link to next segment, 0 = end of chain
    highest_in_use: int  # maintained in segment 1 only
    extra_bytes: int  # extra bytes per entry, always even
    data_start_block: int  # where this segment's data area begins


@dataclass(frozen=True)
class DirEntry:
    """One directory entry. ``start_block`` is implicit on disk: it is the
    segment's data start plus the cumulative lengths of prior entries."""

    status: int
    name_words: tuple[int, int, int]
    length: int
    job_channel: int
    date_word: int
    start_block: int
    extra: bytes = b""
    name: Optional[str] = field(default=None)  # 6 chars, None if junk RAD50
    file_type: Optional[str] = field(default=None)  # 3 chars, None if junk

    @property
    def is_permanent(self) -> bool:
        return bool(self.status & E_PERM)

    @property
    def is_empty(self) -> bool:
        return bool(self.status & E_MPTY)

    @property
    def is_tentative(self) -> bool:
        return bool(self.status & E_TENT)

    @property
    def is_protected(self) -> bool:
        return bool(self.status & E_PROT)


@dataclass(frozen=True)
class ParsedSegment:
    header: SegmentHeader
    entries: tuple[DirEntry, ...]
    eos_found: bool
    # Lossless round-trip fields: the exact end-of-segment marker word and
    # the (undefined, often non-zero on real disks) bytes following it.
    eos_word: int = E_EOS
    tail: bytes = b""


def parse_segment(data: bytes) -> ParsedSegment:
    """Parse one two-block (1024-byte) directory segment.

    Entries are returned in directory order with computed start blocks; the
    end-of-segment marker (which may be a bare final status word) terminates
    the list and is reported via ``eos_found``. Never raises on entry
    contents -- junk RAD50 name words simply decode to None. The marker word
    and the undefined bytes after it are preserved (``eos_word``/``tail``)
    so ``serialize_segment`` can round-trip real disks byte-identically.
    """
    if len(data) < SEGMENT_SIZE:
        raise ValueError(f"Segment needs {SEGMENT_SIZE} bytes, got {len(data)}")
    header = SegmentHeader(*struct.unpack_from("<5H", data, 0))
    entry_size = ENTRY_BASE_BYTES + max(header.extra_bytes, 0)
    entries = []
    eos_found = False
    eos_word = E_EOS
    tail = b""
    start_block = header.data_start_block
    offset = SEGMENT_HEADER_WORDS * 2
    while offset + 2 <= SEGMENT_SIZE:
        status = struct.unpack_from("<H", data, offset)[0]
        if status & E_EOS:
            eos_found = True
            eos_word = status
            tail = bytes(data[offset + 2 : SEGMENT_SIZE])
            break
        if offset + ENTRY_BASE_BYTES > SEGMENT_SIZE:
            break
        words = struct.unpack_from("<6H", data, offset + 2)
        name1, name2, type_word, length, job_channel, date_word = words
        name = file_type = None
        if rad50_is_valid(name1) and rad50_is_valid(name2):
            name = rad50_decode(name1) + rad50_decode(name2)
        if rad50_is_valid(type_word):
            file_type = rad50_decode(type_word)
        extra = bytes(
            data[offset + ENTRY_BASE_BYTES : min(offset + entry_size, SEGMENT_SIZE)]
        )
        entries.append(
            DirEntry(
                status=status,
                name_words=(name1, name2, type_word),
                length=length,
                job_channel=job_channel,
                date_word=date_word,
                start_block=start_block,
                extra=extra,
                name=name,
                file_type=file_type,
            )
        )
        start_block += length
        offset += entry_size
    return ParsedSegment(
        header=header,
        entries=tuple(entries),
        eos_found=eos_found,
        eos_word=eos_word,
        tail=tail,
    )


def segment_max_entries(extra_bytes: int) -> int:
    """Maximum entries one segment can hold alongside its EOS marker word.

    For plain entries this is 72, matching the manual's integer formula
    S = (512 - 5) / (7 + N) (section 1.1.4) -- and the committed BASIC-11
    disk really does carry a full 72-entry segment.
    """
    if extra_bytes < 0 or extra_bytes % 2:
        raise ValueError(f"extra_bytes must be even and >= 0, got {extra_bytes}")
    overhead = SEGMENT_HEADER_WORDS * 2 + 2  # header + EOS marker word
    return (SEGMENT_SIZE - overhead) // (ENTRY_BASE_BYTES + extra_bytes)


def serialize_segment(segment: ParsedSegment) -> bytes:
    """Serialize one directory segment back to its 1024-byte on-disk form.

    Exact inverse of ``parse_segment`` for EOS-terminated segments: per-entry
    ``extra`` bytes, the marker word and the undefined tail bytes after it
    all round-trip, so editing a real volume is lossless. ``start_block`` is
    implicit on disk and therefore ignored here.

    Raises ValueError for segments without an end-of-segment marker or with
    more entries than fit ahead of the marker.
    """
    header = segment.header
    if not segment.eos_found:
        raise ValueError("Segment has no end-of-segment marker; refusing to write")
    if not segment.eos_word & E_EOS:
        raise ValueError(f"EOS word {segment.eos_word:#o} lacks the E.EOS bit")
    extra_bytes = header.extra_bytes
    if len(segment.entries) > segment_max_entries(extra_bytes):
        raise ValueError(
            f"{len(segment.entries)} entries overflow a segment with "
            f"{extra_bytes} extra bytes per entry "
            f"(max {segment_max_entries(extra_bytes)})"
        )
    out = bytearray(
        struct.pack(
            "<5H",
            header.total_segments,
            header.next_segment,
            header.highest_in_use,
            extra_bytes,
            header.data_start_block,
        )
    )
    for entry in segment.entries:
        out += struct.pack(
            "<7H",
            entry.status,
            *entry.name_words,
            entry.length,
            entry.job_channel,
            entry.date_word,
        )
        out += entry.extra[:extra_bytes].ljust(extra_bytes, b"\x00")
    out += struct.pack("<H", segment.eos_word)
    out += segment.tail[: SEGMENT_SIZE - len(out)]
    out += b"\x00" * (SEGMENT_SIZE - len(out))
    return bytes(out)


# ---------------------------------------------------------------------------
# Volume initialization helpers
# ---------------------------------------------------------------------------

# Default directory segment counts by device capacity, from RT-11 V5.4D
# DUP.SAV (via the rtnseg table in PUTR.ASM, confirmed there against the
# RT-11 V04.00 SUG device table: RX01 -> 1, RX02 -> 4, RK05 -> 16, ...).
# The V&FF manual (section 1.1.2) defers the per-device defaults to DUP.
_DEFAULT_SEGMENT_TABLE = ((512, 1), (2048, 4), (12288, 16))


def default_segment_count(total_blocks: int) -> int:
    """RT-11 INIT's default directory segment count for a device size."""
    for limit, segments in _DEFAULT_SEGMENT_TABLE:
        if total_blocks <= limit:
            return segments
    return MAX_SEGMENTS


_HOME_PACK_CLUSTER_OFFSET = 0o722
_HOME_DIR_START_OFFSET = 0o724
_HOME_VERSION_OFFSET = 0o726
_HOME_VOLUME_ID_OFFSET = 0o730
_HOME_OWNER_OFFSET = 0o744
_HOME_SYSTEM_ID_OFFSET = 0o760  # also probed by the structure scorer below
_HOME_CHECKSUM_OFFSET = 0o776


def _home_text_field(value: str, length: int) -> bytes:
    return value.ljust(length)[:length].encode("ascii", "replace")


def encode_home_block(
    volume_id: str = "RT11A",
    owner: str = "",
    dir_start: int = DEFAULT_DIR_START,
    system_version: str = "V05",
) -> bytes:
    """Build a home block (logical block 1) for a freshly initialized volume.

    Field defaults follow the V&FF manual table 1-1 (pack cluster size 1,
    directory at block 6, system id "DECRT11A"). Unlike many real DEC
    factory disks, the stored checksum is CORRECT: per section 1.1.1 it is
    the simple additive (FILES-11 ODS style) sum of the other 255 words,
    stored in the final word at offset 0o776. Detection never trusts it.
    """
    block = bytearray(BLOCK_SIZE)
    struct.pack_into("<H", block, _HOME_PACK_CLUSTER_OFFSET, 1)
    struct.pack_into("<H", block, _HOME_DIR_START_OFFSET, dir_start)
    struct.pack_into("<H", block, _HOME_VERSION_OFFSET, rad50_encode(system_version))
    block[_HOME_VOLUME_ID_OFFSET : _HOME_VOLUME_ID_OFFSET + 12] = _home_text_field(
        volume_id, 12
    )
    block[_HOME_OWNER_OFFSET : _HOME_OWNER_OFFSET + 12] = _home_text_field(owner, 12)
    block[_HOME_SYSTEM_ID_OFFSET : _HOME_SYSTEM_ID_OFFSET + 12] = _home_text_field(
        "DECRT11A", 12
    )
    checksum = sum(struct.unpack_from("<255H", block, 0)) & 0xFFFF
    struct.pack_into("<H", block, _HOME_CHECKSUM_OFFSET, checksum)
    return bytes(block)


# ---------------------------------------------------------------------------
# RT11Config
# ---------------------------------------------------------------------------


@dataclass
class RT11Config:
    """Detected RT-11 volume parameters, carried between detection and the
    filesystem (mirrors the per-filesystem config pattern)."""

    view: str
    total_blocks: int
    dir_start: int = DEFAULT_DIR_START
    volume_id: str = ""
    owner: str = ""
    system_id: str = ""

    @staticmethod
    def matches(config1, config2) -> bool:
        """True if both configs address the same volume layout (view and
        capacity); identity fields are informational only."""
        return (
            isinstance(config1, RT11Config)
            and isinstance(config2, RT11Config)
            and config1.view == config2.view
            and config1.total_blocks == config2.total_blocks
        )


# ---------------------------------------------------------------------------
# Directory structure scorer
# ---------------------------------------------------------------------------

SCORE_THRESHOLD = 40
_SCORE_HEADER = 50
_SCORE_ENTRY_CHAIN = 25
_SCORE_SYSTEM_ID = 15
_MAX_EXTRA_BYTES = 64  # sane upper bound for extra bytes per entry


def score_directory_structure(
    read_block_fn: Callable[[int], bytes],
    total_blocks: int,
    dir_start: int = DEFAULT_DIR_START,
) -> int:
    """Score how much the volume behind ``read_block_fn`` looks like an
    RT-11 directory structure (0-90, detection threshold 40). Never raises.

    The load-bearing signal is the segment-1 structure: +50 for a valid
    header, +25 for a walkable entry chain (valid RAD50 names, plausible
    lengths summing within the device), +15 bonus for a "DECRT11A" system
    id in the home block. Home-block signals alone stay below the
    threshold because real DEC factory disks carry junk there (including
    wrong checksums, which are never weighted at all).
    """
    try:
        return _score_directory_structure(read_block_fn, total_blocks, dir_start)
    except Exception:
        return 0


def _read_segment(
    read_block_fn: Callable[[int], bytes], dir_start: int, segment_number: int
) -> Optional[ParsedSegment]:
    first_block = dir_start + (segment_number - 1) * SEGMENT_BLOCKS
    data = bytes(read_block_fn(first_block)) + bytes(read_block_fn(first_block + 1))
    if len(data) < SEGMENT_SIZE:
        return None
    return parse_segment(data)


def _score_directory_structure(
    read_block_fn: Callable[[int], bytes],
    total_blocks: int,
    dir_start: int,
) -> int:
    score = 0
    try:
        home = bytes(read_block_fn(HOME_BLOCK))
        if home[_HOME_SYSTEM_ID_OFFSET : _HOME_SYSTEM_ID_OFFSET + 8] == b"DECRT11A":
            score += _SCORE_SYSTEM_ID
    except Exception:
        pass  # a missing home block proves nothing either way

    segment = _read_segment(read_block_fn, dir_start, 1)
    if segment is None:
        return score
    header = segment.header
    header_valid = (
        1 <= header.total_segments <= MAX_SEGMENTS
        and header.next_segment <= header.total_segments
        and header.extra_bytes % 2 == 0
        and header.extra_bytes < _MAX_EXTRA_BYTES
        and (
            dir_start + SEGMENT_BLOCKS * header.total_segments
            <= header.data_start_block
            <= total_blocks
        )
    )
    if not header_valid:
        return score
    score += _SCORE_HEADER

    # Walk the segment chain; every reached segment must be clean.
    total_entries = 0
    segment_number = 1
    visited = set()
    while segment_number:
        if segment_number in visited or len(visited) >= MAX_SEGMENTS:
            return score  # looped or runaway link chain
        visited.add(segment_number)
        if segment is None or not segment.eos_found:
            return score
        used_blocks = 0
        for entry in segment.entries:
            if entry.status & ~(_KNOWN_STATUS_BITS | 0xFF):
                return score  # unknown high status bits
            if not entry.status & (E_TENT | E_MPTY | E_PERM):
                return score
            if not entry.is_empty and (entry.name is None or entry.file_type is None):
                return score  # live entry with junk RAD50 name
            used_blocks += entry.length
        if segment.header.data_start_block + used_blocks > total_blocks:
            return score  # entry lengths overrun the device
        total_entries += len(segment.entries)
        segment_number = segment.header.next_segment
        if segment_number:
            if not 1 <= segment_number <= header.total_segments:
                return score
            segment = _read_segment(read_block_fn, dir_start, segment_number)
    if total_entries:
        score += _SCORE_ENTRY_CHAIN
    return score
