"""DEC RT-11 filesystem.

RT-11 always addresses the volume in 512-byte logical blocks: block 0 is
the boot block, block 1 the home block, blocks 2-5 are reserved, and the
directory segments normally begin at block 6. Raw floppy images may store
those blocks in DEC physical sector order (RX01/RX02/RX50 interleave) or
directly in logical block order, and both conventions can share the exact
same file size (a raw RX02 and a logical dump of the same disk are both
512,512 bytes). Profiles therefore supply geometry only; this filesystem
resolves the actual "view" by scoring the directory structure under every
view the open geometry permits (see rt11_layout) and picking the winner.

Geometry -> candidate views:

- 26 sectors x 128 bytes (77 cyls, 1 head)  -> {rx01, logical}
- 26 sectors x 256 bytes (77 cyls, 1 head)  -> {rx02, logical}
- 10 sectors x 512 bytes (80 cyls, 1 head)  -> {rx50, logical}
- any other uniform 128/256/512-byte geometry -> {logical}

Under the "logical" view a block is 512/bytes_per_sector consecutive
sectors in LBA order, i.e. the plain byte stream. The physical views map
through the DEC handler interleave formulas in rt11_layout.

Directory entries record no byte-precise length -- RT-11 files are always
a whole number of blocks -- so FileInfo.size is length * 512. Files are
contiguous block runs; the start block of each entry is implicit (segment
data start + cumulative lengths, in LINKED segment order). E.MPTY entries
form the free list. Prefix (E.PRE) blocks are part of the file's run and
are returned raw by read_file (PUTR behaves the same); the entry carries a
PRE attribute so callers can tell.

Writes follow authentic RT-11 monitor behavior (V&FF manual section 1.1.5):
``write_file`` finds an E.MPTY area, slides the directory entries down to
insert the new permanent entry in front of the (shrunken) empty, splits a
full segment in half into the next available one, and stamps today's date
word. A replace allocates the new copy BEFORE freeing the old entry
(authentic .ENTER ordering: old and new must coexist, so a mid-write
failure never harms the old file). ``delete`` flips the status word to
E.MPTY in place -- adjacent empties are never coalesced (only SQUEEZE does
that, and SQUEEZE is a non-goal). ``format_fs`` lays down a boot block, a
home block with a correct additive checksum, and an empty segment chain
sized by the RT-11 DUP defaults. All planning happens on an in-memory copy
of the directory; nothing touches the disk until the plan is complete
(validate-before-mutate), and all writes go through the same view mapper
as reads.
"""

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Optional

from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat
from ..rt11_layout import (
    BLOCK_SIZE,
    DEFAULT_DIR_START,
    E_MPTY,
    E_PERM,
    E_PRE,
    E_PROT,
    E_TENT,
    MAX_SEGMENTS,
    RAD50_CHARSET,
    SCORE_THRESHOLD,
    SEGMENT_BLOCKS,
    VIEW_GEOMETRY,
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
    rad50_encode,
    score_directory_structure,
    segment_max_entries,
    serialize_segment,
)
from .fs_base import FileInfo, Filesystem

NO_DATE = datetime.datetime(1972, 1, 1)  # epoch of the RT-11 date word

# Characters allowed in the 6.3 name fields: RAD50 minus the space (padding)
# and the dot (the name/type separator).
_NAME_CHARS = frozenset(RAD50_CHARSET) - {" ", "."}

# Characters EMITTED by suggest_import_name: deliberately narrower than
# _NAME_CHARS -- '%' is RAD50-encodable (write_file accepts it) but several
# vintage tools mishandle it (PUTR prints it as '?'), so suggestions stay
# on the conservative A-Z / 0-9 / $ subset.
_SUGGEST_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$")

# (sectors_per_track, bytes_per_sector) -> raw physical view candidate.
_PHYSICAL_VIEW_FOR = {(26, 128): "rx01", (26, 256): "rx02", (10, 512): "rx50"}

# Sector sizes a 512-byte logical block can be assembled from.
_LOGICAL_VIEW_SECTOR_SIZES = (128, 256, 512)

_MAP_COLORS = {
    "system": "#A0A0A0",
    "directory": "#FFFF00",
    "file": "#FF00FF",
    "free": "#808080",
}


@dataclass
class _MutableSegment:
    """In-memory working copy of one directory segment for write planning.

    All directory mutations happen on a list of these; only segments marked
    ``dirty`` are serialized back, and only after the whole plan succeeded.
    """

    number: int
    total_segments: int
    next_segment: int
    highest_in_use: int
    extra_bytes: int
    data_start_block: int
    entries: list[DirEntry]
    eos_word: int
    tail: bytes
    dirty: bool = False

    def entry_start(self, index: int) -> int:
        """Start block of entry ``index`` (implicit on disk: data start plus
        the cumulative lengths of prior entries)."""
        return self.data_start_block + sum(e.length for e in self.entries[:index])


class RT11Filesystem(Filesystem):
    """DEC RT-11 filesystem with physical/logical view resolution."""

    filesystem_type: ClassVar[str] = "RT11"
    filesystem_aliases: ClassVar[list[str]] = ["RT-11"]
    validity_threshold: ClassVar[int] = SCORE_THRESHOLD  # 40
    config_class: ClassVar[type] = RT11Config

    def __init__(self, disk: Disk, config: Optional[Any] = None):
        super().__init__(disk, config)
        self._initialized = False
        # Cached (view, total_blocks, dir_start, score); None until scored.
        self._resolved: Optional[tuple[str, int, int, int]] = None
        self._view: str = ""
        self._total_blocks: int = 0
        self._dir_start: int = DEFAULT_DIR_START

    # -- plugin plumbing -----------------------------------------------------

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        from .formats.rt11_formats import RT11_FORMATS

        return RT11_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        return RT11Config.matches(config1, config2)

    @staticmethod
    def create_config_from_params(
        format_info: dict[str, Any], physical_format: PhysicalFormat
    ) -> Optional[RT11Config]:
        """Builds an RT11Config from a geometry (plus optional overrides).

        The view defaults to the raw physical view canonical for the
        geometry (rx01/rx02/rx50) and falls back to "logical" for any other
        uniform 128/256/512-byte-sector layout. ``format_info`` may carry
        explicit "view"/"dir_start" keys (custom profile dialogs). Both are
        live: an explicit view is honored on open when it scores >= the
        detection threshold (see ``_resolve``) and selects the write mapping
        in ``format_fs``; dir_start is written to the home block at format
        time (on open, the home block content always wins).
        """
        geometry = _uniform_geometry(physical_format)
        if geometry is None:
            return None
        cylinders, heads, spt, bps = geometry
        view = format_info.get("view")
        if view is None:
            view = _PHYSICAL_VIEW_FOR.get((spt, bps))
            if view is not None and (
                heads != 1 or cylinders != VIEW_GEOMETRY[view].tracks
            ):
                view = None
            if view is None:
                if bps not in _LOGICAL_VIEW_SECTOR_SIZES:
                    return None
                view = "logical"
        if view == "logical":
            total_blocks = physical_format.total_bytes // BLOCK_SIZE
        elif view in VIEW_GEOMETRY:
            total_blocks = VIEW_GEOMETRY[view].total_blocks
        else:
            return None
        return RT11Config(
            view=view,
            total_blocks=total_blocks,
            dir_start=format_info.get("dir_start", DEFAULT_DIR_START),
        )

    # -- view resolution -----------------------------------------------------

    def _candidate_views(self) -> list[str]:
        """Views the open geometry permits, physical candidate first so a
        genuine raw image wins any (theoretical) score tie."""
        geometry = _uniform_geometry(self.disk.physical_format)
        if geometry is None:
            return []
        cylinders, heads, spt, bps = geometry
        candidates = []
        view = _PHYSICAL_VIEW_FOR.get((spt, bps))
        if view is not None and heads == 1 and cylinders == VIEW_GEOMETRY[view].tracks:
            candidates.append(view)
        if bps in _LOGICAL_VIEW_SECTOR_SIZES:
            candidates.append("logical")
        return candidates

    def _view_total_blocks(self, view: str) -> int:
        if view == "logical":
            return self.disk.physical_format.total_bytes // BLOCK_SIZE
        return VIEW_GEOMETRY[view].total_blocks

    def _block_chs_list(self, view: str, block: int) -> list[tuple[int, int, int]]:
        """0-based (cylinder, head, sector) tuples composing one block.

        The bound check covers the "logical" branch too (the core mapper
        does not bound that view itself).
        """
        total = self._view_total_blocks(view)
        if not 0 <= block < total:
            raise ValueError(f"Block {block} out of range 0-{total - 1} ({view})")
        if view != "logical":
            return logical_block_to_chs(view, block)
        pf = self.disk.physical_format
        tf = pf.track_formats[0]
        spt, heads = tf.sectors_per_track, pf.heads
        sectors_per_block = BLOCK_SIZE // tf.bytes_per_sector
        result = []
        for index in range(sectors_per_block):
            lba = block * sectors_per_block + index
            cylinder, rest = divmod(lba, spt * heads)
            head, sector = divmod(rest, spt)
            result.append((cylinder, head, sector))
        return result

    def _read_block_view(self, view: str, block: int) -> bytes:
        return b"".join(
            self.disk.read_sector(cylinder, head, sector)
            for cylinder, head, sector in self._block_chs_list(view, block)
        )

    def _read_block(self, block: int) -> bytes:
        return self._read_block_view(self._view, block)

    def _write_block_view(self, view: str, block: int, data: bytes) -> None:
        """Write one 512-byte logical block through a view's sector map."""
        if len(data) != BLOCK_SIZE:
            raise ValueError(f"Block write needs {BLOCK_SIZE} bytes, got {len(data)}")
        chs_list = self._block_chs_list(view, block)
        chunk = BLOCK_SIZE // len(chs_list)
        for index, (cylinder, head, sector) in enumerate(chs_list):
            self.disk.write_sector(
                cylinder, head, sector, data[index * chunk : (index + 1) * chunk]
            )

    def _write_block(self, block: int, data: bytes) -> None:
        self._write_block_view(self._view, block, data)

    def _dir_start_for(self, view: str, total_blocks: int) -> int:
        """Directory start from the home block, defaulting to 6 on junk."""
        try:
            home = parse_home_block(self._read_block_view(view, 1))
        except (OSError, ValueError):
            return DEFAULT_DIR_START
        if 2 <= home.dir_start <= total_blocks - SEGMENT_BLOCKS:
            return home.dir_start
        return DEFAULT_DIR_START

    def _score_view(self, view: str) -> Optional[tuple[str, int, int, int]]:
        """(view, total_blocks, dir_start, score) for one candidate view,
        or None when the view cannot be scored at all."""
        try:
            total_blocks = self._view_total_blocks(view)
            if total_blocks < DEFAULT_DIR_START + SEGMENT_BLOCKS:
                return None
            dir_start = self._dir_start_for(view, total_blocks)
            score = score_directory_structure(
                lambda block, _v=view: self._read_block_view(_v, block),
                total_blocks,
                dir_start,
            )
        except Exception:  # the scorer never raises, but stay paranoid
            return None
        return (view, total_blocks, dir_start, score)

    def _resolve(self) -> tuple[str, int, int, int]:
        """Scores candidate views and returns the winning
        (view, total_blocks, dir_start, score). Never raises.

        An explicit view carried by an injected config (format profiles,
        custom profile dialogs) is honored when it scores >= threshold --
        this is what disambiguates a genuinely ambiguous container.
        Otherwise every candidate is scored and the best wins, physical
        candidate first on a tie.
        """
        if self._resolved is not None:
            return self._resolved
        try:
            candidates = self._candidate_views()
        except Exception:
            candidates = []
        explicit = self.config.view if isinstance(self.config, RT11Config) else None
        if explicit in candidates:
            scored = self._score_view(explicit)
            if scored is not None and scored[3] >= self.validity_threshold:
                self._resolved = scored
                return scored
        best = ("", 0, DEFAULT_DIR_START, 0)
        for view in candidates:
            scored = self._score_view(view)
            if scored is not None and scored[3] > best[3]:
                best = scored
        self._resolved = best
        return best

    def _initialize(self) -> None:
        if self._initialized:
            return
        view, total_blocks, dir_start, score = self._resolve()
        if score < self.validity_threshold:
            raise ValueError(
                f"No RT-11 directory structure found (best score {score} "
                f"< {self.validity_threshold})"
            )
        self._view = view
        self._total_blocks = total_blocks
        self._dir_start = dir_start
        volume_id = owner = system_id = ""
        try:
            home = parse_home_block(self._read_block_view(view, 1))
            volume_id, owner, system_id = home.volume_id, home.owner, home.system_id
        except (OSError, ValueError) as exc:
            self.logger.warning(f"Home block unreadable: {exc}")
        self.config = RT11Config(
            view=view,
            total_blocks=total_blocks,
            dir_start=dir_start,
            volume_id=volume_id,
            owner=owner,
            system_id=system_id,
        )
        self._initialized = True
        self.logger.info(
            f"RT-11 volume resolved: view={view}, {total_blocks} blocks, "
            f"directory at {dir_start} (score {score})"
        )

    # -- directory -------------------------------------------------------------

    def _read_segment(self, segment_number: int) -> ParsedSegment:
        first = self._dir_start + (segment_number - 1) * SEGMENT_BLOCKS
        return parse_segment(self._read_block(first) + self._read_block(first + 1))

    def _linked_segments(self) -> list[tuple[int, ParsedSegment]]:
        """(segment_number, segment) pairs in LINKED order (start blocks are
        implicit in this order, NOT in physical segment order), cycle-safe."""
        self._initialize()
        segments: list[tuple[int, ParsedSegment]] = []
        segment_number = 1
        total_segments = MAX_SEGMENTS
        visited: set[int] = set()
        while segment_number and segment_number not in visited:
            visited.add(segment_number)
            segment = self._read_segment(segment_number)
            segments.append((segment_number, segment))
            if segment_number == 1:
                total_segments = min(segment.header.total_segments, MAX_SEGMENTS)
            segment_number = segment.header.next_segment
            if segment_number and not 1 <= segment_number <= total_segments:
                self.logger.warning(
                    f"Directory segment link {segment_number} out of range; "
                    "truncating the chain"
                )
                break
        return segments

    def _segments(self) -> list[ParsedSegment]:
        return [segment for _number, segment in self._linked_segments()]

    def _entries(self) -> list[DirEntry]:
        return [entry for segment in self._segments() for entry in segment.entries]

    def _entry_is_live(self, entry: DirEntry) -> bool:
        """Listable and addressable: permanent or tentative, not an empty
        run, with decodable RAD50 names (junk names warn and are skipped)."""
        if entry.is_empty or not (entry.is_permanent or entry.is_tentative):
            return False
        if entry.name is None or entry.file_type is None:
            self.logger.warning(
                f"Skipping live entry with junk RAD50 name words {entry.name_words}"
            )
            return False
        return True

    def _live_entries(self) -> list[DirEntry]:
        """Permanent and tentative entries (empty runs hidden), with junk
        RAD50 names skipped defensively."""
        return [entry for entry in self._entries() if self._entry_is_live(entry)]

    @staticmethod
    def _entry_filename(entry: DirEntry) -> str:
        base = entry.name.rstrip()
        extension = entry.file_type.rstrip()
        return f"{base}.{extension}" if extension else base

    def _entry_datetime(self, entry: DirEntry) -> datetime.datetime:
        decoded = decode_date_word(entry.date_word)
        if decoded is None:
            return NO_DATE
        try:
            return datetime.datetime(*decoded)
        except ValueError:
            # decode_date_word range-checks fields but can still hand back
            # calendar-invalid tuples (e.g. Feb 31) off corrupt disks.
            self.logger.warning(f"Calendar-invalid RT-11 date {decoded}; using epoch")
            return NO_DATE

    def _entry_to_fileinfo(self, entry: DirEntry) -> FileInfo:
        attributes = []
        if entry.status & E_PROT:
            attributes.append("PROT")
        if entry.status & E_TENT:
            attributes.append("TENT")
        if entry.status & E_PRE:
            attributes.append("PRE")
        return FileInfo(
            name=self._entry_filename(entry),
            # RT-11 stores no byte length; files are whole blocks.
            size=entry.length * BLOCK_SIZE,
            is_dir=False,
            datetime=self._entry_datetime(entry),
            attributes=",".join(attributes),
            starting_cluster=entry.start_block,
            extra_data={
                "status": entry.status,
                "raw_words": entry.name_words,
                "start_block": entry.start_block,
                "length_blocks": entry.length,
                "job": entry.job_channel & 0xFF,
                "channel": (entry.job_channel >> 8) & 0xFF,
            },
        )

    def _find_entry(self, path: str) -> DirEntry:
        name = path.lstrip("/").upper()
        for entry in self._live_entries():
            if self._entry_filename(entry) == name:
                return entry
        raise FileNotFoundError(f"No such file: {path}")

    # -- read interface ----------------------------------------------------------

    def list_directory(self, path: str) -> list[FileInfo]:
        self._initialize()
        if path not in ("", "/"):
            raise FileNotFoundError(f"RT-11 has no directories: {path}")
        return [self._entry_to_fileinfo(entry) for entry in self._live_entries()]

    def read_file(self, path: str) -> bytes:
        self._initialize()
        entry = self._find_entry(path)
        end = entry.start_block + entry.length
        if not (entry.start_block >= 0 and end <= self._total_blocks):
            raise ValueError(
                f"{path}: blocks {entry.start_block}-{end - 1} overrun the "
                f"{self._total_blocks}-block volume"
            )
        # Contiguous run; prefix (E.PRE) blocks are part of it and returned
        # raw, like PUTR does. The PRE attribute flags such files.
        return b"".join(
            self._read_block(block) for block in range(entry.start_block, end)
        )

    def get_file_allocation_units(self, path: str) -> list[int]:
        self._initialize()
        entry = self._find_entry(path)
        return list(range(entry.start_block, entry.start_block + entry.length))

    def get_allocated_units(self) -> list[int]:
        """Allocated 512-byte block numbers: boot/home/reserved/directory
        area plus every live file's run (E.MPTY runs are free)."""
        try:
            segments = self._segments()
        except (OSError, ValueError):
            return []
        allocated = set(range(segments[0].header.data_start_block))
        for entry in self._live_entries():
            allocated.update(range(entry.start_block, entry.start_block + entry.length))
        return sorted(block for block in allocated if block < self._total_blocks)

    def get_free_space(self) -> tuple[int, int]:
        """(free, total) bytes. Free is the E.MPTY runs; total is the
        directory-covered data area (sum of ALL entry lengths), which is
        identical for a raw image and its logical dump even when the
        containers differ in size."""
        try:
            entries = self._entries()
        except (OSError, ValueError):
            return 0, 0
        free_blocks = sum(entry.length for entry in entries if entry.is_empty)
        total_blocks = sum(entry.length for entry in entries)
        return free_blocks * BLOCK_SIZE, total_blocks * BLOCK_SIZE

    @property
    def allocation_unit_size(self) -> int:
        """RT-11 allocates whole 512-byte blocks, always."""
        return BLOCK_SIZE

    def get_specific_config(self) -> Optional[RT11Config]:
        try:
            self._initialize()
        except (OSError, ValueError):
            return None
        return self.config

    def get_validity_score(self) -> int:
        # Never raises: the registry probes every filesystem on every disk.
        try:
            return min(self._resolve()[3], 100)
        except Exception:
            return 0

    def get_volume_label(self) -> Optional[str]:
        try:
            self._initialize()
        except (OSError, ValueError):
            return None
        label = self.config.volume_id.strip()
        return label or None

    @staticmethod
    def _fragmentation(entries: list[DirEntry]) -> int:
        """Count of E.MPTY runs preceding the last live entry in linked
        order (between live entries, plus any leading hole before the
        first one).

        Adjacent empties count separately: RT-11 never coalesces them
        (only SQUEEZE does), so each one costs a directory slot and splits
        the free space a contiguous file could use. Trailing empties (after
        the last live entry) are normal free space, not fragmentation.
        """
        last_live = -1
        for index, entry in enumerate(entries):
            # Deliberately NOT _entry_is_live: junk-named permanent entries
            # still occupy blocks, and counting must not log skip warnings.
            if not entry.is_empty and (entry.is_permanent or entry.is_tentative):
                last_live = index
        if last_live < 0:
            return 0
        return sum(1 for entry in entries[:last_live] if entry.is_empty)

    def get_display_info(self) -> dict[str, str]:
        try:
            self._initialize()
            segments = self._segments()
        except (OSError, ValueError) as exc:
            return {"Error": f"Not a recognizable RT-11 volume: {exc}"}
        entries = self._entries()
        permanent = sum(1 for e in entries if e.is_permanent and not e.is_empty)
        tentative = sum(1 for e in entries if e.is_tentative and not e.is_empty)
        free_bytes, _total = self.get_free_space()
        fragmentation = self._fragmentation(entries)
        header = segments[0].header
        view_label = (
            "logical (block order)"
            if self._view == "logical"
            else f"{self._view} (physical sector order)"
        )
        info = {
            "Filesystem": "RT-11",
            "View": view_label,
            "Total Blocks": str(self._total_blocks),
            "Directory Start Block": str(self._dir_start),
            "Directory Segments": f"{header.highest_in_use}/{header.total_segments}",
            "Files": str(permanent),
            "Tentative Files": str(tentative),
            "Free Blocks": str(free_bytes // BLOCK_SIZE),
            "Fragmentation": f"{fragmentation} free run(s) between files",
            "Volume ID": self.config.volume_id.strip(),
            "Owner": self.config.owner.strip(),
            "System ID": self.config.system_id.strip(),
        }
        try:
            home = parse_home_block(self._read_block(1))
            if home.system_version:
                info["System Version"] = home.system_version.strip()
        except (OSError, ValueError):
            pass
        return info

    def get_disk_map_layout(self) -> dict[str, Any]:
        try:
            segments = self._segments()
        except (OSError, ValueError):
            return {}
        directory_blocks = set(
            range(
                self._dir_start,
                self._dir_start + SEGMENT_BLOCKS * segments[0].header.total_segments,
            )
        )
        file_blocks: set[int] = set()
        for entry in self._live_entries():
            file_blocks.update(
                range(entry.start_block, entry.start_block + entry.length)
            )

        # Reverse map: physical (cylinder, head, sector) -> logical block.
        # Physical sectors outside the block space (e.g. the unused track 0
        # of RX01/RX02 raw images) classify as "system".
        sector_to_block: dict[tuple[int, int, int], int] = {}
        for block in range(self._total_blocks):
            for chs in self._block_chs_list(self._view, block):
                sector_to_block[chs] = block
        pf = self.disk.physical_format
        tf = pf.track_formats[0]
        spt, heads = tf.sectors_per_track, pf.heads

        def get_sector_type(lba: int) -> str:
            cylinder, rest = divmod(lba, spt * heads)
            head, sector = divmod(rest, spt)
            block = sector_to_block.get((cylinder, head, sector))
            if block is None or block < self._dir_start:
                return "system"  # boot, home, reserved or unmapped sectors
            if block in directory_blocks:
                return "directory"
            if block in file_blocks:
                return "file"
            return "free"

        sectors_per_block = BLOCK_SIZE // tf.bytes_per_sector
        return {
            "legend": [
                ("Boot/Home/Reserved", _MAP_COLORS["system"]),
                ("Directory", _MAP_COLORS["directory"]),
                ("File Data", _MAP_COLORS["file"]),
                ("Free", _MAP_COLORS["free"]),
            ],
            "get_sector_type": get_sector_type,
            "allocation_unit_size_sectors": sectors_per_block,
            # Nominal: the disk map's FILE-HIGHLIGHT/tooltip unit math
            # ((lba - first_data_sector) // unit_size) assumes the data area
            # is LBA-contiguous, which the interleaved rx01/rx02/rx50 views
            # violate -- the same known limitation skewed CP/M profiles ship
            # with (their first_data_sector is in logical-sector terms).
            # Per-sector type COLORING goes through get_sector_type and
            # stays exact regardless.
            "first_data_sector": segments[0].header.data_start_block
            * sectors_per_block,
            "type_color_map": dict(_MAP_COLORS),
        }

    def check(self) -> bool:
        """Directory-walk consistency check (read-only, CBM-check style).

        RT-11 entry start blocks are implicit (segment data start plus
        cumulative lengths), so block-level consistency reduces to the
        directory itself: each chained segment must carry an end-of-segment
        marker, its data start must equal the previous segment's cumulative
        end (a lower start makes file runs OVERLAP -- data-loss risk; a
        higher one only strands unreachable blocks), the runs must not
        overrun the device, and segment 1's data area must not overlap the
        directory blocks. Fragmentation (E.MPTY runs between files) and
        tentative entries are reported as findings, never failures.

        Verdict rule:
        - FAILURE (False): unwalkable directory, a segment chain that never
          terminates (cyclic or out-of-range link), missing end-of-segment
          marker, overlapping runs, runs overrunning the device, data area
          overlapping the directory.
        - WARNING only: unreachable gap blocks between segments.
        - INFO only: a data area ending short of the container (normal for
          logical dumps in oversized containers), fragmentation and
          tentative counts.

        Corrupt volumes produce findings, not raises.
        """
        try:
            self._initialize()
            chain = self._linked_segments()
        except (OSError, ValueError) as exc:
            self.logger.warning(f"check(): unwalkable directory: {exc}")
            return False
        ok = True
        head = chain[0][1].header
        directory_end = self._dir_start + SEGMENT_BLOCKS * head.total_segments
        if head.data_start_block < directory_end:
            self.logger.warning(
                f"check(): data area starts at block {head.data_start_block}, "
                f"inside the directory (blocks {self._dir_start}-"
                f"{directory_end - 1})"
            )
            ok = False
        expected_start: Optional[int] = None
        for number, segment in chain:
            if not segment.eos_found:
                self.logger.warning(
                    f"check(): segment {number} has no end-of-segment marker"
                )
                ok = False
            start = segment.header.data_start_block
            if expected_start is not None and start != expected_start:
                if start < expected_start:
                    self.logger.warning(
                        f"check(): segment {number} data start {start} is "
                        f"before the previous chain end {expected_start}: "
                        f"file runs overlap (data-loss risk)"
                    )
                    ok = False
                else:
                    self.logger.warning(
                        f"check(): segment {number} data start {start} leaves "
                        f"{start - expected_start} unreachable block(s) after "
                        f"the previous chain end {expected_start}"
                    )
            cursor = start
            for entry in segment.entries:
                cursor += entry.length
            if cursor > self._total_blocks:
                self.logger.warning(
                    f"check(): segment {number} entry runs end at block "
                    f"{cursor}, overrunning the {self._total_blocks}-block "
                    f"device"
                )
                ok = False
            expected_start = cursor
        # _linked_segments only exits cleanly on a zero link; anything else
        # means it truncated a cyclic or out-of-range chain.
        tail_number, tail_segment = chain[-1]
        tail_link = tail_segment.header.next_segment
        if tail_link != 0:
            self.logger.warning(
                f"check(): directory chain does not terminate: segment "
                f"{tail_number} links to segment {tail_link} (cyclic or "
                f"out-of-range; RT-11 DIR would loop forever)"
            )
            ok = False
        if expected_start is not None and expected_start < self._total_blocks:
            self.logger.info(
                f"check(): directory covers blocks up to {expected_start} of "
                f"{self._total_blocks} (normal for a logical dump in an "
                f"oversized container)"
            )
        entries = [entry for _number, segment in chain for entry in segment.entries]
        tentative = sum(1 for e in entries if e.is_tentative and not e.is_empty)
        self.logger.info(
            f"check(): {sum(1 for e in entries if e.is_empty)} free run(s), "
            f"{self._fragmentation(entries)} between files (fragmentation), "
            f"{tentative} tentative entries"
        )
        return ok

    # -- write interface -------------------------------------------------------

    @staticmethod
    def _parse_write_name(path: str) -> tuple[str, str]:
        """Validates a write path into (name, type), 6.3 format.

        Case is normalized to uppercase, structure is validated strictly
        with clear errors and no silent truncation (the CP/M write-name
        philosophy; lookups stay lenient).
        """
        name = path.lstrip("/").upper()
        base, _dot, file_type = name.partition(".")
        if not base:
            raise ValueError(f"RT-11 file name must not be empty: {path!r}")
        if len(base) > 6 or len(file_type) > 3 or "." in file_type:
            raise ValueError(
                f"RT-11 names are 6.3 format (name <= 6 chars, type <= 3 "
                f"chars, one dot): {path!r}"
            )
        bad = set(base + file_type) - _NAME_CHARS
        if bad:
            raise ValueError(
                f"Character(s) {''.join(sorted(bad))!r} not encodable in an "
                f"RT-11 name (RAD50: A-Z, 0-9, $, %): {path!r}"
            )
        return base, file_type

    def _load_directory_model(self) -> list[_MutableSegment]:
        """The directory as mutable working copies, in linked order."""
        model = []
        for number, segment in self._linked_segments():
            if not segment.eos_found:
                raise ValueError(
                    f"Directory segment {number} has no end-of-segment "
                    "marker; refusing to write to a damaged directory"
                )
            model.append(
                _MutableSegment(
                    number=number,
                    total_segments=segment.header.total_segments,
                    next_segment=segment.header.next_segment,
                    highest_in_use=segment.header.highest_in_use,
                    extra_bytes=segment.header.extra_bytes,
                    data_start_block=segment.header.data_start_block,
                    entries=list(segment.entries),
                    eos_word=segment.eos_word,
                    tail=segment.tail,
                )
            )
        return model

    def _store_directory_model(self, model: list[_MutableSegment]) -> None:
        """Serialize every dirty segment back to its on-disk slot.

        Written in REVERSE linked order: after a split that is the new
        segment first, then the segment now linking to it, then segment 1's
        header -- so an interrupted write never leaves a link pointing at a
        stale segment slot.
        """
        for segment in reversed(model):
            if not segment.dirty:
                continue
            data = serialize_segment(
                ParsedSegment(
                    header=SegmentHeader(
                        total_segments=segment.total_segments,
                        next_segment=segment.next_segment,
                        highest_in_use=segment.highest_in_use,
                        extra_bytes=segment.extra_bytes,
                        data_start_block=segment.data_start_block,
                    ),
                    entries=tuple(segment.entries),
                    eos_found=True,
                    eos_word=segment.eos_word,
                    tail=segment.tail,
                )
            )
            first = self._dir_start + (segment.number - 1) * SEGMENT_BLOCKS
            self._write_block(first, data[:BLOCK_SIZE])
            self._write_block(first + 1, data[BLOCK_SIZE:])

    def _model_find_live(
        self, model: list[_MutableSegment], filename: str
    ) -> Optional[tuple[_MutableSegment, int]]:
        """(segment, entry index) of the live entry named ``filename``."""
        for segment in model:
            for index, entry in enumerate(segment.entries):
                if not self._entry_is_live(entry):
                    continue
                if self._entry_filename(entry) == filename:
                    return segment, index
        return None

    def _check_unprotected(self, entry: DirEntry, action: str) -> None:
        """Refuse to touch protected (E.PROT) files."""
        if entry.status & E_PROT:
            raise PermissionError(
                f"{self._entry_filename(entry)} is a protected (E.PROT) "
                f"file; refusing to {action}"
            )

    def _model_mark_empty(
        self, segment: _MutableSegment, index: int, action: str
    ) -> DirEntry:
        """Flip a live entry to E.MPTY in place (authentic RT-11 delete
        semantics), refusing protected files. Returns the old entry."""
        entry = segment.entries[index]
        self._check_unprotected(entry, action)
        segment.entries[index] = replace(entry, status=E_MPTY)
        segment.dirty = True
        return entry

    def _model_mark_empty_entry(
        self, model: list[_MutableSegment], entry: DirEntry, action: str
    ) -> DirEntry:
        """Mark a specific ``entry`` E.MPTY, locating it by object identity:
        planning steps between lookup and deletion (allocation insertions,
        segment splits) may have moved it to another index or segment, and
        on a replace the freshly allocated entry shares the name -- with
        TWO live same-named entries a name-based lookup is ambiguous and
        could mark the NEW entry empty, losing the write."""
        for segment in model:
            for index, candidate in enumerate(segment.entries):
                if candidate is entry:
                    return self._model_mark_empty(segment, index, action)
        raise ValueError(
            f"Directory entry {self._entry_filename(entry)} vanished from "
            "the write plan"
        )

    @staticmethod
    def _split_index(entries: list[DirEntry]) -> int:
        """Index of the first entry that moves to the new segment: a
        permanent or tentative entry near the middle (manual section 1.1.5),
        keeping both halves non-empty."""
        mid = len(entries) // 2
        for index in sorted(range(1, len(entries)), key=lambda i: (abs(i - mid), i)):
            if entries[index].status & (E_PERM | E_TENT):
                return index
        return mid  # no live entries at all: split at the structural middle

    def _split_segment(
        self, model: list[_MutableSegment], segment: _MutableSegment
    ) -> None:
        """Split a full ``segment`` in half per manual section 1.1.5.

        The lowest segment number not yet in the chain is opened; the upper
        half of the entries moves to it; the new segment inherits the old
        link while the old segment links to the new one (this is what makes
        the chain order diverge from numeric order on real volumes); segment
        1's highest-in-use counter is updated.
        """
        head = model[0]  # the chain always starts at segment 1
        in_use = {seg.number for seg in model}
        available = [
            number
            for number in range(1, min(head.total_segments, MAX_SEGMENTS) + 1)
            if number not in in_use
        ]
        if not available:
            raise OSError(
                f"Directory full: all {head.total_segments} segments are in "
                "use (RT-11 needs a SQUEEZE or more segments at INIT time)"
            )
        new_number = available[0]
        if len(segment.entries) < 2:
            raise OSError("Directory full: cannot split a near-empty segment")
        mid = self._split_index(segment.entries)
        new_segment = _MutableSegment(
            number=new_number,
            total_segments=head.total_segments,
            # The new segment inherits the old segment's link...
            next_segment=segment.next_segment,
            highest_in_use=0,  # maintained in segment 1 only (manual 1.1.2.1)
            extra_bytes=segment.extra_bytes,
            data_start_block=segment.entry_start(mid),
            entries=segment.entries[mid:],
            eos_word=segment.eos_word,
            tail=b"",
            dirty=True,
        )
        # ...and the old segment links to the new one.
        segment.entries = segment.entries[:mid]
        segment.next_segment = new_number
        segment.dirty = True
        head.highest_in_use = max(head.highest_in_use, new_number)
        head.dirty = True
        model.insert(model.index(segment) + 1, new_segment)

    def _plan_allocation(
        self,
        model: list[_MutableSegment],
        filename: str,
        name_words: tuple[int, int, int],
        length: int,
    ) -> int:
        """First-fit allocation: inserts the new permanent entry into the
        model and returns its start block. Splits full segments as needed.

        Only E.MPTY runs are allocation targets; tentative entries are
        skipped (not empty) but their blocks stay allocated. On a replace,
        ``write_file`` calls this while the OLD entry is still live in the
        model (authentic .ENTER ordering), so the old file's run is never a
        candidate and its blocks are never overwritten mid-plan. NOTE: real
        RT-11 .ENTER uses best-fit (manual section 1.1.3); first-fit is this
        implementation's pinned, deterministic policy (PUTR does the same).
        NOTE: segments are filled to the structural 72-entry capacity
        (manual section 1.1.4) and split only on demand; real RT-11 INIT
        reserves ~3 slots per segment (69 usable) and splits earlier, so
        our directories split later than the monitor's would.
        """
        today = datetime.date.today()
        try:
            date_word = encode_date_word(today.year, today.month, today.day)
        except ValueError:  # host clock outside the representable 1972-2099
            date_word = 0
        # Termination: every `continue` follows a split that either raises
        # (directory full) or strictly grows the chain, which is capped at
        # MAX_SEGMENTS (31) segments.
        while True:
            found = None
            for segment in model:
                for index, entry in enumerate(segment.entries):
                    if (
                        entry.is_empty
                        and entry.length >= length
                        # Distrust runs overrunning the device (possible on
                        # damaged-but-detected volumes): never scribble past
                        # the block space or over unrelated sectors.
                        and segment.entry_start(index) + entry.length
                        <= self._total_blocks
                    ):
                        found = (segment, index)
                        break
                if found:
                    break
            if found is None:
                largest = max(
                    (e.length for s in model for e in s.entries if e.is_empty),
                    default=0,
                )
                raise OSError(
                    f"Not enough contiguous free space for {filename}: need "
                    f"{length} block(s), largest free area is {largest} "
                    "(RT-11 files are contiguous; free runs never coalesce "
                    "without a SQUEEZE)"
                )
            segment, index = found
            empty = segment.entries[index]
            leftover = empty.length - length
            added_entries = 1 if leftover else 0
            if len(segment.entries) + added_entries > segment_max_entries(
                segment.extra_bytes
            ):
                self._split_segment(model, segment)
                continue  # re-run the search against the split directory
            start_block = segment.entry_start(index)
            new_entry = DirEntry(
                status=E_PERM,
                name_words=name_words,
                length=length,
                job_channel=0,
                date_word=date_word,
                start_block=start_block,
                extra=b"\x00" * segment.extra_bytes,
                name=filename.partition(".")[0].ljust(6),
                file_type=filename.partition(".")[2].ljust(3),
            )
            replacement = [new_entry]
            if leftover:
                # The found empty slides down behind the new file, shrunk.
                replacement.append(
                    replace(empty, length=leftover, start_block=start_block + length)
                )
            segment.entries[index : index + 1] = replacement
            segment.dirty = True
            return start_block

    def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` as a permanent file, replacing any same-named one.

        Replace follows authentic .ENTER ordering: the NEW allocation is
        planned while the old entry is still live (its blocks are therefore
        never allocation candidates), and only after allocation succeeds
        does the old entry flip to E.MPTY -- so a replace needs room for
        old and new simultaneously, and a mid-write I/O failure can never
        corrupt the old file (real RT-11 likewise .ENTERs a tentative file
        in NEW space and deletes the old at close). The whole plan
        (allocation + any segment split + delete-old) is computed on an
        in-memory directory copy first; a plan failure (OSError for no
        fitting run or full directory) leaves the disk byte-identical.
        """
        base, file_type = self._parse_write_name(path)
        filename = f"{base}.{file_type}" if file_type else base
        self._initialize()
        name_words = (
            rad50_encode(base[:3]),
            rad50_encode(base[3:6]),
            rad50_encode(file_type),
        )
        length = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE

        model = self._load_directory_model()
        existing = self._model_find_live(model, filename)
        old_entry: Optional[DirEntry] = None
        if existing is not None:
            found_segment, found_index = existing
            old_entry = found_segment.entries[found_index]
            self._check_unprotected(old_entry, action="replace")
        start_block = self._plan_allocation(model, filename, name_words, length)
        if old_entry is not None:
            # Allocation succeeded with the old entry still live; NOW the
            # old copy is deleted, by identity (allocation insertions or a
            # segment split may have moved it).
            self._model_mark_empty_entry(model, old_entry, action="replace")

        # Plan complete -- only now touch the disk: data blocks first, the
        # directory last, so a failed data write never orphans an entry.
        for index in range(length):
            chunk = data[index * BLOCK_SIZE : (index + 1) * BLOCK_SIZE]
            self._write_block(start_block + index, chunk.ljust(BLOCK_SIZE, b"\x00"))
        self._store_directory_model(model)
        self.disk.flush()
        self.logger.info(f"Wrote {filename}: {length} block(s) at block {start_block}")

    def delete(self, path: str) -> None:
        """Authentic RT-11 delete: the entry's status word flips to E.MPTY
        in place. Name, length and date remain (DIR /DELETED lists them);
        adjacent empties are deliberately NOT coalesced -- only the SQUEEZE
        operation consolidates free space, and SQUEEZE is a non-goal."""
        self._initialize()
        filename = path.lstrip("/").upper()
        model = self._load_directory_model()
        found = self._model_find_live(model, filename)
        if found is None:
            raise FileNotFoundError(f"No such file: {path}")
        entry = self._model_mark_empty(*found, action="delete")
        self._store_directory_model(model)
        self.disk.flush()
        self.logger.info(f"Deleted {filename} ({entry.length} block(s) freed)")

    def delete_recursive(self, path: str) -> bool:
        try:
            self.delete(path)
            return True
        except (OSError, ValueError):
            return False

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("RT-11 has no directories")

    # -- name policy -------------------------------------------------------------

    def suggest_import_name(
        self,
        host_name: str,
        existing_names: Iterable[str],
        is_dir: bool = False,
    ) -> str:
        """
        Derives a valid, unique RT-11 6.3 name from a host filename.

        Characters outside the conservative suggestion charset (A-Z, 0-9,
        '$') are MAPPED to '$' rather than dropped -- dropping would
        silently merge distinct host names and could empty the base
        entirely; '$' is the placeholder convention here, mirroring the
        siblings' '_'/'-' (which are not RAD50-encodable). Note the
        asymmetry with write_file: writes accept the full RAD50 name
        charset including '%', but suggestions never emit it (several
        vintage tools mishandle '%'; PUTR prints it as '?'). Uniqueness
        uses plain digit suffixes within the 6-char base (NAME1, NAME2,
        ...) because '~' is not in RAD50. The result always passes
        _parse_write_name().

        Args:
            host_name: The filename from the host filesystem.
            existing_names: Names already present on the disk.
            is_dir: True when importing a directory entry (RT-11 has none,
                but the base contract is preserved).

        Returns:
            A valid, unique on-disk 6.3 name.

        Raises:
            ValueError: If a unique name cannot be generated after 999
                attempts.
        """
        existing = {n.upper() for n in existing_names}
        cleaned = "".join(
            ch if (ch in _SUGGEST_CHARS or ch == ".") else "$"
            for ch in host_name.upper()
        )
        if "." in cleaned.strip(".") and not is_dir:
            base, _dot, ext = cleaned.rpartition(".")
            base = base.replace(".", "$")[:6]
            ext = ext[:3]
        else:
            base, ext = cleaned.replace(".", "$")[:6], ""
        if not base:
            base = "$FILE"
        candidate = f"{base}.{ext}" if ext else base
        if candidate.upper() not in existing:
            return candidate
        for counter in range(1, 1000):
            suffix = str(counter)
            new_base = base[: 6 - len(suffix)] + suffix
            candidate = f"{new_base}.{ext}" if ext else new_base
            if candidate.upper() not in existing:
                return candidate
        raise ValueError(f"Cannot generate unique RT-11 name for {host_name!r}")

    def name_hint(self) -> str:
        """Short description of RT-11 naming rules, for dialogs."""
        return "6.3 RAD50 (A-Z, 0-9, $)"

    def format_fs(
        self, profile: FormatProfile, volume_label: Optional[str] = None
    ) -> None:
        """Initialize the volume: boot block, home block, empty directory.

        The view to write through comes from the profile's RT11Config (or
        is derived from the geometry). The directory gets the RT-11 DUP
        default segment count for the device size (RX01 1, RX02/RX50 4; see
        rt11_layout.default_segment_count) and a single E.MPTY entry
        covering the whole data area. The home block checksum is written
        CORRECTLY (manual section 1.1.1) even though detection never trusts
        it. ``volume_label`` lands in the 12-byte volume ID field.
        """
        config = profile.filesystem_config
        if not isinstance(config, RT11Config):
            physical = profile.physical_format or self.disk.physical_format
            config = self.create_config_from_params({}, physical)
        if config is None:
            raise ValueError(
                "FormatProfile for RT-11 must carry an RT11Config or a "
                "uniform 128/256/512-byte-sector geometry"
            )
        view = config.view
        candidates = self._candidate_views()
        if view not in candidates:
            raise ValueError(
                f"View {view!r} is not addressable on the open geometry "
                f"(candidates: {candidates or 'none'})"
            )
        total_blocks = self._view_total_blocks(view)
        dir_start = config.dir_start
        if dir_start < 2:  # blocks 0/1 are the boot and home blocks
            raise ValueError(f"Directory cannot start before block 2: {dir_start}")
        segments = default_segment_count(total_blocks)
        data_start = dir_start + SEGMENT_BLOCKS * segments
        if data_start >= total_blocks:
            raise ValueError(
                f"Device of {total_blocks} blocks cannot hold a directory "
                f"of {segments} segment(s) at block {dir_start}"
            )
        label = (volume_label or "").strip() or "RT11A"

        zero = b"\x00" * BLOCK_SIZE
        for block in range(dir_start):  # boot block + reserved blocks
            if block != 1:
                self._write_block_view(view, block, zero)
        self._write_block_view(
            view, 1, encode_home_block(volume_id=label, dir_start=dir_start)
        )
        for block in range(dir_start, data_start):
            self._write_block_view(view, block, zero)
        segment_one = serialize_segment(
            ParsedSegment(
                header=SegmentHeader(
                    total_segments=segments,
                    next_segment=0,
                    highest_in_use=1,
                    extra_bytes=0,
                    data_start_block=data_start,
                ),
                entries=(
                    DirEntry(
                        status=E_MPTY,
                        # Names on empties are ignored by RT-11; "EMPTY.FIL"
                        # keeps DIR /DELETED output tidy (xferx convention).
                        name_words=(
                            rad50_encode("EMP"),
                            rad50_encode("TY"),
                            rad50_encode("FIL"),
                        ),
                        length=total_blocks - data_start,
                        job_channel=0,
                        date_word=0,
                        start_block=data_start,
                    ),
                ),
                eos_found=True,
            )
        )
        self._write_block_view(view, dir_start, segment_one[:BLOCK_SIZE])
        self._write_block_view(view, dir_start + 1, segment_one[BLOCK_SIZE:])

        # Re-resolve from the freshly written content; the explicit config
        # view pins the resolution for the (rare) ambiguous container.
        self._resolved = None
        self._initialized = False
        self.config = RT11Config(
            view=view,
            total_blocks=total_blocks,
            dir_start=dir_start,
            volume_id=label,
        )
        self.disk.flush()
        self.logger.info(
            f"Formatted RT-11 volume: view={view}, {total_blocks} blocks, "
            f"{segments} directory segment(s), volume ID {label!r}"
        )


def _uniform_geometry(
    physical_format: Optional[PhysicalFormat],
) -> Optional[tuple[int, int, int, int]]:
    """(cylinders, heads, spt, bps) when every track shares one shape, else
    None (zoned/variable geometries cannot carry an RT-11 view)."""
    if physical_format is None or not physical_format.track_formats:
        return None
    first = physical_format.track_formats[0]
    for track_format in physical_format.track_formats:
        if (
            track_format.sectors_per_track != first.sectors_per_track
            or track_format.bytes_per_sector != first.bytes_per_sector
        ):
            return None
    return (
        physical_format.cylinders,
        physical_format.heads,
        first.sectors_per_track,
        first.bytes_per_sector,
    )
