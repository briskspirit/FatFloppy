"""DEC RT-11 filesystem (read side).

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

Write support is not implemented yet (read-only milestone).
"""

import datetime
from typing import Any, ClassVar, Optional

from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat
from ..rt11_layout import (
    BLOCK_SIZE,
    DEFAULT_DIR_START,
    E_PRE,
    E_PROT,
    E_TENT,
    MAX_SEGMENTS,
    SCORE_THRESHOLD,
    SEGMENT_BLOCKS,
    VIEW_GEOMETRY,
    DirEntry,
    ParsedSegment,
    RT11Config,
    decode_date_word,
    logical_block_to_chs,
    parse_home_block,
    parse_segment,
    score_directory_structure,
)
from .fs_base import FileInfo, Filesystem

NO_DATE = datetime.datetime(1972, 1, 1)  # epoch of the RT-11 date word

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


class RT11Filesystem(Filesystem):
    """Read-only DEC RT-11 filesystem with physical/logical view resolution."""

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
        explicit "view"/"dir_start" keys (custom profile dialogs).
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

    def _dir_start_for(self, view: str, total_blocks: int) -> int:
        """Directory start from the home block, defaulting to 6 on junk."""
        try:
            home = parse_home_block(self._read_block_view(view, 1))
        except (OSError, ValueError):
            return DEFAULT_DIR_START
        if 2 <= home.dir_start <= total_blocks - SEGMENT_BLOCKS:
            return home.dir_start
        return DEFAULT_DIR_START

    def _resolve(self) -> tuple[str, int, int, int]:
        """Scores every candidate view and returns the best
        (view, total_blocks, dir_start, score). Never raises."""
        if self._resolved is not None:
            return self._resolved
        best = ("", 0, DEFAULT_DIR_START, 0)
        try:
            candidates = self._candidate_views()
        except Exception:
            candidates = []
        for view in candidates:
            try:
                total_blocks = self._view_total_blocks(view)
                if total_blocks < DEFAULT_DIR_START + SEGMENT_BLOCKS:
                    continue
                dir_start = self._dir_start_for(view, total_blocks)
                score = score_directory_structure(
                    lambda block, _v=view: self._read_block_view(_v, block),
                    total_blocks,
                    dir_start,
                )
            except Exception:  # the scorer never raises, but stay paranoid
                continue
            if score > best[3]:
                best = (view, total_blocks, dir_start, score)
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

    def _segments(self) -> list[ParsedSegment]:
        """Directory segments in LINKED order (start blocks are implicit in
        this order, NOT in physical segment order), cycle-safe."""
        self._initialize()
        segments: list[ParsedSegment] = []
        segment_number = 1
        total_segments = MAX_SEGMENTS
        visited: set[int] = set()
        while segment_number and segment_number not in visited:
            visited.add(segment_number)
            segment = self._read_segment(segment_number)
            segments.append(segment)
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

    def _entries(self) -> list[DirEntry]:
        return [entry for segment in self._segments() for entry in segment.entries]

    def _live_entries(self) -> list[DirEntry]:
        """Permanent and tentative entries (empty runs hidden), with junk
        RAD50 names skipped defensively."""
        live = []
        for entry in self._entries():
            if entry.is_empty or not (entry.is_permanent or entry.is_tentative):
                continue
            if entry.name is None or entry.file_type is None:
                self.logger.warning(
                    f"Skipping live entry with junk RAD50 name words {entry.name_words}"
                )
                continue
            live.append(entry)
        return live

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

    def get_display_info(self) -> dict[str, str]:
        try:
            self._initialize()
            segments = self._segments()
        except (OSError, ValueError) as exc:
            return {"Error": f"Not a recognizable RT-11 volume: {exc}"}
        files = self._live_entries()
        free_bytes, _total = self.get_free_space()
        header = segments[0].header
        info = {
            "Filesystem": "RT-11",
            "View": self._view,
            "Total Blocks": str(self._total_blocks),
            "Directory Start Block": str(self._dir_start),
            "Directory Segments": f"{header.highest_in_use}/{header.total_segments}",
            "Files": str(len(files)),
            "Free Blocks": str(free_bytes // BLOCK_SIZE),
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
            # Nominal: under the interleaved views the data area is not
            # LBA-contiguous; per-sector coloring stays exact regardless.
            "first_data_sector": segments[0].header.data_start_block
            * sectors_per_block,
            "type_color_map": dict(_MAP_COLORS),
        }

    # -- write interface (Task 4) --------------------------------------------------

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError("RT-11 write support is not implemented yet")

    def delete(self, path: str) -> None:
        raise NotImplementedError("RT-11 write support is not implemented yet")

    def delete_recursive(self, path: str) -> bool:
        raise NotImplementedError("RT-11 write support is not implemented yet")

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("RT-11 has no directories")

    def format_fs(
        self, profile: FormatProfile, volume_label: Optional[str] = None
    ) -> None:
        raise NotImplementedError("RT-11 formatting is not implemented yet")


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
