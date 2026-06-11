import contextlib
import datetime
import struct
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger
from .fs_base import FileInfo, Filesystem

HDOS_BYTES_PER_SECTOR = 256
HDOS_SECTORS_PER_TRACK = 10
HDOS_TRACKS = 40
HDOS_DIR_ENTRY_SIZE = 23
HDOS_DEFAULT_DATETIME = datetime.datetime(1970, 1, 1)

DIR_BLOCK_SECTORS = 2
DIR_BLOCK_BYTES = DIR_BLOCK_SECTORS * HDOS_BYTES_PER_SECTOR
DIR_ENTRIES_PER_BLOCK = 22

DIR_ZERO_OFFSET = 506
DIR_ENTRYLEN_OFFSET = 507
DIR_THIS_BLOCK_PTR_OFFSET = 508
DIR_NEXT_BLOCK_PTR_OFFSET = 510

HDOS_LABEL_SECTOR_LBA = 9
HDOS_RGT_SECTOR_LBA = 10
# (cylinders, sectors_per_track) shapes an HDOS controller can produce:
# H17/H37 (40 or 80 tracks x 10 sectors) and H47 (77 tracks x 26 sectors),
# single- or double-sided, always 256-byte sectors.
HDOS_PLAUSIBLE_SHAPES = frozenset({(40, 10), (80, 10), (77, 26)})
HDOS_SYSTEM_FILES = {"RGT.SYS", "GRT.SYS", "HDOS.SYS"}
HDOS_DIRECT_SYS = {"DIRECT.SYS"}

FLAGS_SYSTEM_CORE = 0xF0
FLAGS_DIRECT = 0xE0


@dataclass
class HDOSDirectoryEntry:
    """Represents a single 23-byte HDOS directory entry (pre-3.0 format)."""

    raw_name: bytes
    name: str
    ext: str
    cluster_factor: int
    first_group: int
    last_group: int
    last_sector_index: int
    creation_date: datetime.datetime
    modification_date: datetime.datetime
    attributes: str = "-"

    def get_filename(self) -> str:
        """Constructs the full 8.3 filename from the entry."""
        return f"{self.name}.{self.ext}" if self.ext else self.name


@dataclass
class HDOSLabelRecord:
    """
    Represents data from the Label Identification Sector (Track 0, Sector 10).
    """

    title: str
    volume_number: int
    cluster_factor: int
    dir_start_block: int
    grt_start_block: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "HDOSLabelRecord":
        """
        Parses a 256-byte sector based on the precise documented layout.

        Args:
            data: The 256-byte raw sector data.

        Returns:
            An instance of HDOSLabelRecord.

        Raises:
            ValueError: If the data length is incorrect.
        """
        if len(data) < HDOS_BYTES_PER_SECTOR:
            raise ValueError(
                f"Label sector data must be {HDOS_BYTES_PER_SECTOR} bytes."
            )

        dir_start_block = struct.unpack_from("<H", data, 3)[0]
        grt_start_block = struct.unpack_from("<H", data, 5)[0]
        cluster_factor = data[7]
        volume_number = data[0]

        title = data[17:77].decode("ascii", errors="ignore").strip("\x00").strip()

        return cls(
            title, volume_number, cluster_factor, dir_start_block, grt_start_block
        )

    def is_valid(self, max_blocks: int) -> bool:
        """
        Performs a basic sanity check on the parsed label record values.

        A cluster_factor of 0 is valid and implies 1 sector per group.

        Args:
            max_blocks: The total number of blocks (sectors) on the disk.

        Returns:
            True if the label values seem plausible, False otherwise.
        """
        return (
            0 <= self.cluster_factor <= 16
            and HDOS_SECTORS_PER_TRACK <= self.dir_start_block < max_blocks
            and HDOS_SECTORS_PER_TRACK <= self.grt_start_block < max_blocks
        )


class HDOSFilesystem(Filesystem):
    """
    Provides an interface to an HDOS filesystem on a disk image.
    """

    filesystem_type: ClassVar[str] = "HDOS"
    filesystem_aliases: ClassVar[list[str]] = []
    validity_threshold: ClassVar[int] = 95
    VALIDITY_THRESHOLD = validity_threshold

    config_class: ClassVar[type] = HDOSLabelRecord

    def __init__(self, disk: Disk, config: Optional[HDOSLabelRecord] = None):
        """
        Initializes the HDOSFilesystem object.

        Args:
            disk: The Disk object to operate on.
            config: Optional Volume Label Record for this filesystem.
        """
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self.label: Optional[HDOSLabelRecord] = config
        self._grt: Optional[bytearray] = None
        self._rgt: Optional[bytearray] = None
        self._dir_entries: Optional[list[HDOSDirectoryEntry]] = None
        self._init_completed: bool = False
        self._cached_validity_score: Optional[int] = None
        self._data_base_lba_cache: Optional[int] = None
        self._num_groups_on_disk: int = 0

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        """
        Returns all HDOS format definitions provided by this plugin.

        Returns:
            Dictionary mapping format names to FormatProfile objects.
        """
        from .formats.hdos_formats import HDOS_FORMATS

        return HDOS_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        """
        Checks if two filesystem configurations match.

        Args:
            config1: First configuration to compare.
            config2: Second configuration to compare.

        Returns:
            True if both are HDOSLabelRecord instances, False otherwise.
        """
        return not (
            not isinstance(config1, HDOSLabelRecord)
            or not isinstance(config2, HDOSLabelRecord)
        )

    @property
    def allocation_unit_size(self) -> int:
        """
        Returns the size of a single allocation unit (group) in bytes.

        Returns:
            The group size in bytes, or 0 if the filesystem is not valid.
        """
        if self.get_validity_score() < self.validity_threshold:
            return 0
        self._initialize()
        if not self.label:
            return 0

        sectors_per_group = (
            self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        )
        return sectors_per_group * HDOS_BYTES_PER_SECTOR

    def create_directory(self, path: str) -> None:
        """
        HDOS does not support creating directories.

        Raises:
            NotImplementedError: Always raised.
        """
        raise NotImplementedError("HDOS does not support directories.")

    def delete(self, path: str) -> None:
        """
        Deletes a file by marking its directory entry and freeing its groups.

        Args:
            path: The full path of the file to delete.

        Raises:
            IOError: If the filesystem is invalid or a system file is targeted.
            FileNotFoundError: If the specified file does not exist.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid.")
        self._initialize()

        filename_upper = path.strip("/").upper()
        parts = filename_upper.split(".")
        name_part = parts[0][:8] if parts else ""
        ext_part = parts[1][:3] if len(parts) > 1 else ""
        search_filename = f"{name_part}.{ext_part}" if ext_part else name_part

        all_protected = HDOS_SYSTEM_FILES | HDOS_DIRECT_SYS
        if search_filename in all_protected or filename_upper in all_protected:
            raise OSError(f"Cannot delete system file: {filename_upper}")

        found_entry = None
        entry_dir_lba = 0
        entry_offset_in_block = 0

        current_dir_lba = self.label.dir_start_block
        for _ in range(20):
            if current_dir_lba == 0:
                break
            try:
                dir_data = self._read_lba(current_dir_lba) + self._read_lba(
                    current_dir_lba + 1
                )
            except (OSError, ValueError) as e:
                self.logger.error(
                    f"Could not read directory block at LBA {current_dir_lba}: {e}"
                )
                break

            end_of_dir = False
            for i in range(DIR_ENTRIES_PER_BLOCK):
                offset = i * HDOS_DIR_ENTRY_SIZE
                # Honour the 0xFE end-of-directory marker so delete() sees exactly
                # the same entries as the reader and never matches stale entries
                # past the marker (audit hdos_fs.py:249).
                if dir_data[offset] == 0xFE:
                    end_of_dir = True
                    break
                entry = self._parse_single_dir_entry(
                    dir_data[offset : offset + HDOS_DIR_ENTRY_SIZE]
                )
                if entry and entry.get_filename().upper() == search_filename:
                    found_entry = entry
                    entry_dir_lba = current_dir_lba
                    entry_offset_in_block = offset
                    break
            if found_entry or end_of_dir:
                break
            try:
                current_dir_lba = struct.unpack_from(
                    "<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET
                )[0]
            except struct.error:
                break

        if not found_entry:
            raise FileNotFoundError(f"File not found: {path}")

        file_groups_to_free = []
        if found_entry.first_group != 0:
            current = found_entry.first_group
            for _ in range(len(self._grt)):
                if current == 0 or current >= len(self._grt):
                    break
                file_groups_to_free.append(current)
                if current == found_entry.last_group:
                    break
                next_group = self._grt[current]
                if next_group == current:
                    self.logger.warning(
                        f"Circular reference in file chain at group {current}"
                    )
                    break
                current = next_group

        self.logger.info(
            f"Deleting file '{path}' which uses {len(file_groups_to_free)} "
            f"groups: {file_groups_to_free}"
        )

        if file_groups_to_free:
            if self._grt[0] == 0:
                self.logger.info("Rebuilding free chain from scratch after deletion")

                current_allocated = set(self.get_allocated_units())
                deleted_file_groups_set = set(file_groups_to_free)
                current_allocated -= deleted_file_groups_set

                all_groups = set(range(1, self._num_groups_on_disk + 1))

                rgt_locked_groups = set()
                if self._rgt:
                    for g in range(
                        1, min(self._num_groups_on_disk + 1, len(self._rgt))
                    ):
                        if self._is_group_locked_out(g):
                            rgt_locked_groups.add(g)
                    self.logger.debug(
                        f"Excluding {len(rgt_locked_groups)} RGT locked-out "
                        "groups from free chain"
                    )

                free_groups = sorted(all_groups - current_allocated - rgt_locked_groups)

                for i in range(len(free_groups) - 1):
                    self._grt[free_groups[i]] = free_groups[i + 1]
                if free_groups:
                    self._grt[free_groups[-1]] = 0
                    self._grt[0] = free_groups[0]

                self.logger.info(f"Built free chain with {len(free_groups)} groups")
            else:
                old_free_start = self._grt[0]

                for i in range(len(file_groups_to_free) - 1):
                    self._grt[file_groups_to_free[i]] = file_groups_to_free[i + 1]

                self._grt[file_groups_to_free[-1]] = old_free_start
                self._grt[0] = file_groups_to_free[0]

                self.logger.info(
                    f"Prepended {len(file_groups_to_free)} freed groups to free chain"
                )

            try:
                self._write_lba(self.label.grt_start_block, self._grt)
            except (OSError, ValueError) as e:
                self.logger.error(f"Failed to write updated GRT: {e}")
                raise OSError("Failed to update GRT after file deletion") from e

        try:
            dir_block_data = bytearray(
                self._read_lba(entry_dir_lba) + self._read_lba(entry_dir_lba + 1)
            )
            dir_block_data[entry_offset_in_block] = 0xFF
            self._write_lba(entry_dir_lba, dir_block_data[:HDOS_BYTES_PER_SECTOR])
            self._write_lba(entry_dir_lba + 1, dir_block_data[HDOS_BYTES_PER_SECTOR:])
        except (OSError, ValueError) as e:
            self.logger.error(f"Failed to update directory after deleting {path}: {e}")
            raise OSError("Failed to update directory entry") from e

        self.disk.flush()

        self._dir_entries = None
        self._grt = None
        self._rgt = None
        self._init_completed = False
        self._data_base_lba_cache = None
        self._cached_validity_score = None

        self.logger.info(
            f"Deleted file '{path}' and freed {len(file_groups_to_free)} groups"
        )

    def delete_recursive(self, path: str) -> bool:
        """
        Deletes a file. Since HDOS has no directories, this is not recursive.

        Args:
            path: The path of the file to delete.

        Returns:
            True if deletion was successful, False otherwise.
        """
        try:
            self.delete(path)
            return True
        except (OSError, FileNotFoundError):
            return False

    def format_fs(
        self, profile: FormatProfile, volume_label: Optional[str] = None
    ) -> None:
        """
        Formats the disk with a blank HDOS filesystem.

        Args:
            profile: The FormatProfile containing filesystem and physical
                parameters.
            volume_label: An optional volume title to apply to the disk.

        Raises:
            ValueError: If the profile does not contain a valid
                HDOSLabelRecord.
        """
        if not isinstance(profile.filesystem_config, HDOSLabelRecord):
            raise ValueError(
                "FormatProfile must contain an HDOSLabelRecord for formatting."
            )

        label_template = profile.filesystem_config
        if volume_label:
            label_template.title = volume_label

        pf = profile.physical_format
        spg = label_template.cluster_factor if label_template.cluster_factor > 0 else 1

        data_area_start_lba = 2
        total_data_sectors = pf.total_sectors - data_area_start_lba
        num_groups_on_disk = total_data_sectors // spg

        dir_start_group = (
            (label_template.dir_start_block - data_area_start_lba) // spg
        ) + 1
        dir_groups_needed = (DIR_BLOCK_SECTORS + spg - 1) // spg
        dir_reserved_groups = set(
            range(dir_start_group, dir_start_group + dir_groups_needed)
        )

        grt_reserved_groups = set()
        if label_template.grt_start_block >= data_area_start_lba:
            grt_group = (
                (label_template.grt_start_block - data_area_start_lba) // spg
            ) + 1
            if 1 <= grt_group <= num_groups_on_disk:
                grt_reserved_groups.add(grt_group)

        track0_end_lba = HDOS_SECTORS_PER_TRACK - 1
        track0_groups = set()
        for g in range(1, num_groups_on_disk + 1):
            group_start_lba = data_area_start_lba + (g - 1) * spg
            if group_start_lba <= track0_end_lba:
                track0_groups.add(g)

        self.logger.info(f"Track 0 groups to lock out: {sorted(track0_groups)}")

        # Reserve the groups holding the on-disk metadata sectors (volume label
        # and RGT) so the allocator's free chain can never hand them out. Without
        # this, the RGT sector's group is free and the first file written after
        # formatting overwrites the RGT (audit hdos_fs.py:439).
        metadata_reserved_groups = set()
        for meta_lba in (HDOS_LABEL_SECTOR_LBA, HDOS_RGT_SECTOR_LBA):
            if meta_lba >= data_area_start_lba:
                meta_group = ((meta_lba - data_area_start_lba) // spg) + 1
                if 1 <= meta_group <= num_groups_on_disk:
                    metadata_reserved_groups.add(meta_group)

        reserved_groups = (
            dir_reserved_groups
            | grt_reserved_groups
            | track0_groups
            | metadata_reserved_groups
        )

        grt_data = bytearray(HDOS_BYTES_PER_SECTOR)
        free_chain_head = 0
        for i in range(num_groups_on_disk, 0, -1):
            if i >= len(grt_data):
                continue
            if i in reserved_groups:
                grt_data[i] = 0xFF
            else:
                grt_data[i] = free_chain_head
                free_chain_head = i
        grt_data[0] = free_chain_head

        locked_groups = track0_groups | metadata_reserved_groups
        rgt_data = bytearray(HDOS_BYTES_PER_SECTOR)
        for i in range(len(rgt_data)):
            if i == 0:
                rgt_data[i] = 0x00
            elif i in locked_groups:
                rgt_data[i] = 0xFF
            else:
                rgt_data[i] = 0x01

        dir_data = bytearray(DIR_BLOCK_BYTES)
        dir_data[0] = 0xFE
        struct.pack_into("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET, 0)

        label_bytes = self._create_label_sector_bytes(label_template)
        self._write_lba(HDOS_LABEL_SECTOR_LBA, label_bytes)
        self._write_lba(HDOS_RGT_SECTOR_LBA, rgt_data)
        self._write_lba(label_template.grt_start_block, grt_data)
        self._write_lba(
            label_template.dir_start_block, dir_data[:HDOS_BYTES_PER_SECTOR]
        )
        self._write_lba(
            label_template.dir_start_block + 1, dir_data[HDOS_BYTES_PER_SECTOR:]
        )

        self.disk.flush()
        self._init_completed = False
        self._data_base_lba_cache = None

        self.logger.info(
            f"Disk formatted with profile '{profile.name}', "
            f"reserved: {len(dir_reserved_groups)} dir + "
            f"{len(grt_reserved_groups)} grt + {len(track0_groups)} track0 "
            "groups"
        )

    def get_allocated_units(self) -> list[int]:
        """
        Returns a sorted list of allocated group numbers.

        In HDOS, the GRT free chain is the authoritative source for free
        groups. The RGT marks groups that are locked out.

        Returns:
            A sorted list of integers representing allocated group numbers.
        """
        if self.get_validity_score() < self.validity_threshold:
            return []
        self._initialize()
        if not self._grt or not self.label:
            return []

        num_groups_on_disk = self._num_groups_on_disk
        if num_groups_on_disk <= 0:
            return []

        all_groups = set(range(1, num_groups_on_disk + 1))
        free_groups = set()
        current_group = self._grt[0]
        visited = set()

        self.logger.debug(
            f"get_allocated_units: GRT[0]={current_group}, "
            f"num_groups={num_groups_on_disk}"
        )

        if current_group == 0:
            self.logger.info(
                "GRT[0]=0, no free chain - using fallback to scan file chains"
            )

            file_groups = set()
            for entry in self._dir_entries:
                if entry.first_group > 0:
                    current = entry.first_group
                    for _ in range(len(self._grt)):
                        if current == 0 or current > num_groups_on_disk:
                            break
                        file_groups.add(current)
                        if current == entry.last_group:
                            break
                        if current >= len(self._grt):
                            break
                        current = self._grt[current]

            data_base = self._data_base_lba()
            spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1

            system_groups = set()
            cur = self.label.dir_start_block
            visited_dirs = set()
            while cur and cur not in visited_dirs:
                visited_dirs.add(cur)
                for s_offset in range(DIR_BLOCK_SECTORS):
                    s = cur + s_offset
                    if s >= data_base:
                        g = ((s - data_base) // spg) + 1
                        if 1 <= g <= num_groups_on_disk:
                            system_groups.add(g)
                try:
                    blk = self._read_lba(cur) + self._read_lba(cur + 1)
                    nxt = struct.unpack_from("<H", blk, DIR_NEXT_BLOCK_PTR_OFFSET)[0]
                    if nxt == cur or nxt == 0:
                        break
                    cur = nxt
                except (OSError, ValueError, struct.error):
                    break

            if self.label.grt_start_block >= data_base:
                grt_group = ((self.label.grt_start_block - data_base) // spg) + 1
                if 1 <= grt_group <= num_groups_on_disk:
                    system_groups.add(grt_group)

            rgt_locked_groups = set()
            if self._rgt:
                for g in range(1, min(num_groups_on_disk + 1, len(self._rgt))):
                    if self._is_group_locked_out(g):
                        rgt_locked_groups.add(g)

            allocated_groups = file_groups | system_groups | rgt_locked_groups
            self.logger.debug(
                f"Fallback found {len(file_groups)} file groups, "
                f"{len(system_groups)} system groups, "
                f"{len(rgt_locked_groups)} RGT-locked groups"
            )
            return sorted(allocated_groups)

        while current_group != 0 and 0 < current_group <= num_groups_on_disk:
            if current_group in visited:
                self.logger.warning(
                    f"Circular reference in free chain at group {current_group}"
                )
                break
            if current_group >= len(self._grt):
                self.logger.warning(
                    f"Free chain group {current_group} beyond GRT bounds"
                )
                break

            visited.add(current_group)
            free_groups.add(current_group)
            try:
                current_group = self._grt[current_group]
            except IndexError:
                break

        allocated_groups = all_groups - free_groups

        if self._rgt:
            for g in range(1, min(num_groups_on_disk + 1, len(self._rgt))):
                if self._is_group_locked_out(g):
                    allocated_groups.add(g)

        self.logger.debug(
            f"Normal traversal: {len(free_groups)} free, "
            f"{len(allocated_groups)} allocated"
        )
        return sorted(allocated_groups)

    def get_disk_map_layout(self) -> dict[str, Any]:
        """
        Provides data for visualizing the disk layout.

        Returns:
            A dictionary containing legend, color maps, and a function
            to determine sector type for visualization.
        """
        if self.get_validity_score() < self.validity_threshold:
            return {}
        self._initialize()

        system_lbas = set(range(HDOS_SECTORS_PER_TRACK))

        sectors_per_group = (
            self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        )
        data_area_start_lba = self._data_base_lba()

        rgt_lbas = set()
        if data_area_start_lba <= HDOS_RGT_SECTOR_LBA:
            rgt_group = (
                (HDOS_RGT_SECTOR_LBA - data_area_start_lba) // sectors_per_group
            ) + 1
            rgt_group_start_lba = (
                data_area_start_lba + (rgt_group - 1) * sectors_per_group
            )
            for s_offset in range(sectors_per_group):
                rgt_lbas.add(rgt_group_start_lba + s_offset)

        grt_lbas = set()
        if self.label.grt_start_block >= data_area_start_lba:
            grt_group = (
                (self.label.grt_start_block - data_area_start_lba) // sectors_per_group
            ) + 1
            grt_group_start_lba = (
                data_area_start_lba + (grt_group - 1) * sectors_per_group
            )
            for s_offset in range(sectors_per_group):
                grt_lbas.add(grt_group_start_lba + s_offset)

        directory_lbas = set()
        current_block_lba = self.label.dir_start_block
        for _ in range(20):
            if current_block_lba == 0:
                break
            directory_lbas.add(current_block_lba)
            directory_lbas.add(current_block_lba + 1)

            try:
                dir_data = self._read_lba(current_block_lba) + self._read_lba(
                    current_block_lba + 1
                )
                next_lba = struct.unpack_from(
                    "<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET
                )[0]
                if next_lba == current_block_lba:
                    break
                current_block_lba = next_lba
            except (OSError, ValueError):
                break

        allocated_groups = set(self.get_allocated_units())

        def get_sector_type(lba: int) -> str:
            if lba in system_lbas:
                return "system"
            if lba in rgt_lbas:
                return "rgt"
            if lba in grt_lbas:
                return "grt"
            if lba in directory_lbas:
                return "directory"

            if lba >= data_area_start_lba:
                group_num = ((lba - data_area_start_lba) // sectors_per_group) + 1
                if group_num in allocated_groups:
                    return "data_used"
                else:
                    return "data_free"
            return "unknown"

        legend_colors = {
            "System Track": "#A0A0A0",
            "RGT": "#FF6B6B",
            "Directory": "#FFFF00",
            "GRT": "#FFA500",
            "Used Data": "#FF00FF",
            "Free Data": "#808080",
        }

        legend = [
            ("System Track", legend_colors["System Track"]),
            ("RGT", legend_colors["RGT"]),
            ("Directory", legend_colors["Directory"]),
            ("GRT", legend_colors["GRT"]),
            ("Used Data", legend_colors["Used Data"]),
            ("Free Data", legend_colors["Free Data"]),
        ]

        type_map = {
            "system": legend_colors["System Track"],
            "rgt": legend_colors["RGT"],
            "directory": legend_colors["Directory"],
            "grt": legend_colors["GRT"],
            "data_used": legend_colors["Used Data"],
            "data_free": legend_colors["Free Data"],
            "unknown": "#008B8B",
        }

        return {
            "legend": legend,
            "get_sector_type": get_sector_type,
            "allocation_unit_size_sectors": sectors_per_group,
            "first_data_sector": data_area_start_lba,
            "type_color_map": type_map,
        }

    def get_display_info(self) -> dict[str, str]:
        """
        Returns a dictionary of key filesystem parameters for display.

        Returns:
            A dictionary with human-readable information about the HDOS volume.
        """
        if self.get_validity_score() < self.validity_threshold:
            return {"Error": "HDOS not detected or not valid."}
        self._initialize()
        if not self.label:
            return {"Error": "HDOS label could not be read."}
        return {
            "Filesystem Type": "HDOS",
            "Volume Title": self.label.title,
            "Volume Number": str(self.label.volume_number),
            "Dir Cluster Factor": str(self.label.cluster_factor),
            "Directory Start LBA": str(self.label.dir_start_block),
            "GRT Start LBA": str(self.label.grt_start_block),
        }

    def get_file_allocation_units(self, path: str) -> list[int]:
        """
        Gets the list of group numbers allocated to a specific file.

        Args:
            path: The full path to the file.

        Returns:
            A list of group numbers used by the file, in chain order.
            Returns an empty list if the file doesn't exist or has no
            allocated groups.

        Raises:
            IOError: If the filesystem is not valid or a read error occurs.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as HDOS.")

        self._initialize()

        filename_upper = path.strip("/").upper()
        parts = filename_upper.split(".")
        name_part = parts[0][:8] if parts else ""
        ext_part = parts[1][:3] if len(parts) > 1 else ""
        search_filename = f"{name_part}.{ext_part}" if ext_part else name_part

        target_entry = None
        for e in self._dir_entries:
            entry_filename = e.get_filename().upper()
            if entry_filename == search_filename:
                target_entry = e
                break

        if not target_entry:
            self.logger.warning(f"File '{path}' not found in directory.")
            return []

        if target_entry.first_group == 0:
            return []

        file_groups = []
        current_group = target_entry.first_group

        for _ in range(len(self._grt)):
            if current_group == 0:
                break

            if not (0 < current_group < len(self._grt)):
                self.logger.error(
                    f"Corrupt file chain for '{path}': group "
                    f"{current_group} is out of bounds."
                )
                break

            file_groups.append(current_group)

            if current_group == target_entry.last_group:
                break

            next_group = self._grt[current_group]

            if next_group == current_group or next_group in file_groups:
                self.logger.error(
                    f"Circular reference detected in file chain for '{path}' "
                    f"at group {current_group}."
                )
                break

            current_group = next_group

        self.logger.debug(
            f"File '{path}' uses {len(file_groups)} groups: {file_groups}"
        )
        return file_groups

    def get_free_space(self) -> tuple[int, int]:
        """
        Calculates free and total space on the disk.

        Returns:
            A tuple containing (free_bytes, total_bytes).
        """
        if self.get_validity_score() < self.validity_threshold:
            return 0, 0
        self._initialize()
        if not self._grt or not self.label or not self.disk.physical_format:
            return 0, 0

        data_base = self._data_base_lba()
        total_data_sectors = self.disk.physical_format.total_sectors - data_base
        total_bytes = total_data_sectors * HDOS_BYTES_PER_SECTOR
        spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1

        num_groups_on_disk = self._num_groups_on_disk
        if num_groups_on_disk <= 0:
            return 0, total_bytes

        allocated_count = len(self.get_allocated_units())
        free_group_count = num_groups_on_disk - allocated_count
        free_group_count = max(0, free_group_count)

        group_size_bytes = spg * HDOS_BYTES_PER_SECTOR
        free_bytes = free_group_count * group_size_bytes

        self.logger.debug(
            f"Free space: {free_group_count} groups × {group_size_bytes} "
            f"bytes = {free_bytes} bytes"
        )

        return free_bytes, total_bytes

    def get_specific_config(self) -> Optional[HDOSLabelRecord]:
        """
        Returns the parsed HDOSLabelRecord for the filesystem.

        Returns:
            The HDOSLabelRecord object, or None if not initialized.
        """
        self._initialize()
        return self.label

    def get_validity_score(self) -> int:
        """
        Scores the likelihood that the disk contains a valid HDOS filesystem.

        Returns:
            A score from 0 to 100 indicating the likelihood of a valid HDOS FS.
        """
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        if not self.disk:
            return 0
        original_pf = self.disk.physical_format

        # Scoring force-swaps to the canonical H17 geometry below, so without
        # a gate any disk whose bytes at canonical LBA 9 happen to parse as a
        # plausible label record gets claimed (e.g. file data on a Commodore
        # D64). HDOS only ever lives on geometries its controllers support,
        # so refuse disks whose known geometry is not an HDOS shape.
        if original_pf is not None and not self._geometry_plausible_for_hdos(
            original_pf
        ):
            self.logger.debug(
                "HDOS validation skipped: disk geometry is not an HDOS shape"
            )
            self._cached_validity_score = 0
            return 0

        try:
            canonical_pf = FormatProfile(
                name="hdos_canonical",
                description="",
                physical_format=PhysicalFormat(
                    cylinders=HDOS_TRACKS,
                    heads=1,
                    rpm=300,
                    heads_inverted=False,
                    bytes_per_sector=HDOS_BYTES_PER_SECTOR,
                    track_formats=[
                        TrackFormat(
                            0,
                            39,
                            0,
                            0,
                            HDOS_SECTORS_PER_TRACK,
                            "FM",
                            250,
                            1,
                        )
                    ],
                ),
                filesystem_config=None,
            ).physical_format
            self.disk.set_geometry(canonical_pf)

            self._initialize()
            self._cached_validity_score = 100
        except (OSError, ValueError, struct.error) as e:
            self.logger.debug(f"HDOS validation failed: {e}")
            self._cached_validity_score = 0
        finally:
            if original_pf:
                self.disk.set_geometry(original_pf)
            if self._cached_validity_score == 0:
                self._init_completed = False
                self.label = None
                self._data_base_lba_cache = None

        return self._cached_validity_score

    @staticmethod
    def _geometry_plausible_for_hdos(pf: PhysicalFormat) -> bool:
        """
        Checks whether a disk geometry is one an HDOS controller can produce.

        HDOS disks always have uniform 256-byte sectors and a uniform
        sectors-per-track count in one of the H17/H37/H47 shapes (40 or 80
        tracks x 10 sectors, or 77 tracks x 26 sectors), single- or
        double-sided. A variable-zone geometry (e.g. a Commodore D64's
        21/19/18/17 sectors per track) is never HDOS.

        Args:
            pf: The physical format to check.

        Returns:
            True if the geometry is plausible for HDOS, False otherwise.
        """
        if pf.heads not in (1, 2):
            return False
        if not pf.track_formats:
            return pf.bytes_per_sector == HDOS_BYTES_PER_SECTOR
        spt_values = {tf.sectors_per_track for tf in pf.track_formats}
        bps_values = {tf.bytes_per_sector for tf in pf.track_formats}
        if len(spt_values) != 1 or bps_values != {HDOS_BYTES_PER_SECTOR}:
            return False
        return (pf.cylinders, next(iter(spt_values))) in HDOS_PLAUSIBLE_SHAPES

    def get_volume_label(self) -> Optional[str]:
        """
        Returns the HDOS volume label from the label record.

        Returns:
            The volume label (title) as a string, or None if not available.
        """
        if self.label and self.label.title:
            return self.label.title.strip()
        return None

    def list_directory(self, path: str) -> list[FileInfo]:
        """
        Lists all files in the root directory.

        Args:
            path: The directory path (must be "/").

        Returns:
            A list of FileInfo objects representing files.

        Raises:
            IOError: If the filesystem is not valid.
            NotImplementedError: If path is not root directory.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as HDOS.")
        if path != "/":
            raise NotImplementedError("HDOS does not support subdirectories.")
        self._initialize()
        return [
            FileInfo(
                name=e.get_filename(),
                size=self._calculate_file_size(e),
                is_dir=False,
                datetime=e.modification_date,
                attributes=e.attributes,
            )
            for e in self._dir_entries
        ]

    def read_file(self, path: str) -> bytes:
        """
        Reads the complete content of a specified file.

        Args:
            path: The full path to the file to read (e.g., "/MYFILE.TXT").

        Returns:
            The raw byte content of the file.

        Raises:
            IOError: If the filesystem is not valid or a read error occurs.
            FileNotFoundError: If the specified file does not exist.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as HDOS.")

        self._initialize()

        filename_upper = path.strip("/").upper()
        parts = filename_upper.split(".")
        name_part = parts[0][:8] if parts else ""
        ext_part = parts[1][:3] if len(parts) > 1 else ""
        search_filename = f"{name_part}.{ext_part}" if ext_part else name_part

        target_entry = None
        for e in self._dir_entries:
            entry_filename = e.get_filename().upper()
            if entry_filename == search_filename:
                target_entry = e
                break

        if not target_entry:
            raise FileNotFoundError(f"File not found: {path}")

        file_data = bytearray()
        current_group = target_entry.first_group

        sectors_per_group = (
            self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        )
        data_area_start_lba = self._data_base_lba()

        for _ in range(self.disk.physical_format.total_sectors):
            if current_group == 0:
                break
            if not (0 < current_group < len(self._grt)):
                raise OSError(
                    f"Corrupt file chain: group {current_group} is out of GRT bounds."
                )

            start_lba_of_group = data_area_start_lba + (
                (current_group - 1) * sectors_per_group
            )

            sectors_to_read = sectors_per_group
            if current_group == target_entry.last_group:
                # Clamp the untrusted last-sector index so a corrupt entry cannot
                # read past the group into other files' data (audit hdos_fs.py:1025).
                sectors_to_read = min(target_entry.last_sector_index, sectors_per_group)
                if target_entry.last_sector_index > sectors_per_group:
                    self.logger.warning(
                        f"Clamped last_sector_index {target_entry.last_sector_index} "
                        f"to {sectors_per_group} for file '{path}'."
                    )

            for i in range(sectors_to_read):
                try:
                    file_data.extend(self._read_lba(start_lba_of_group + i))
                except (OSError, ValueError) as e:
                    self.logger.error(
                        f"Failed to read LBA {start_lba_of_group + i} for "
                        f"file {path}: {e}"
                    )
                    raise OSError(f"Failed reading LBA {start_lba_of_group + i}") from e

            if current_group == target_entry.last_group:
                break
            current_group = self._grt[current_group]

        allocated_size = self._calculate_file_size(target_entry)
        file_data = file_data[:allocated_size]

        if len(file_data) > 0:
            last_non_null = len(file_data) - 1
            while last_non_null >= 0 and file_data[last_non_null] == 0:
                last_non_null -= 1

            if last_non_null >= 0 and last_non_null < len(file_data) - 1:
                non_null_portion = file_data[: last_non_null + 1]
                text_bytes = sum(
                    1
                    for b in non_null_portion
                    if b in range(32, 127) or b in (9, 10, 13)
                )
                text_ratio = (
                    text_bytes / len(non_null_portion)
                    if len(non_null_portion) > 0
                    else 0
                )

                if text_ratio > 0.9:
                    file_data = file_data[: last_non_null + 1]
                    self.logger.debug(
                        f"Trimmed trailing nulls from text file: {search_filename}"
                    )

        return bytes(file_data)

    def write_file(self, path: str, data: bytes) -> None:
        """
        Writes data to a new file on the disk.

        Deletes existing file if present.

        Args:
            path: The path of the file to write.
            data: The binary data to write to the file.

        Raises:
            IOError: If the filesystem is invalid or there's not enough space.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem not valid.")

        # Validate the filename before any destructive step (delete/allocation),
        # so an invalid name cannot leave the disk mid-modified (audit hdos_fs.py:1223).
        self._validate_filename(path)

        with contextlib.suppress(FileNotFoundError):
            self.delete(path)

        self._initialize()
        spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1

        num_sectors_needed = (
            len(data) + HDOS_BYTES_PER_SECTOR - 1
        ) // HDOS_BYTES_PER_SECTOR
        num_groups_needed = (
            (num_sectors_needed + spg - 1) // spg if num_sectors_needed > 0 else 0
        )

        if num_groups_needed == 0:
            self._create_empty_file_entry(path)
            self.disk.flush()
            self._dir_entries = None
            self._init_completed = False
            return

        allocated_groups = []
        visited = set()
        current_group = self._grt[0]
        for _ in range(num_groups_needed):
            if current_group == 0:
                raise OSError("Not enough free space on disk.")
            # Guard against a corrupt free chain (cycle or out-of-range group)
            # before mutating the GRT or writing any sector (audit hdos_fs.py:1108).
            if current_group in visited or not (
                1 <= current_group <= self._num_groups_on_disk
            ):
                raise OSError(
                    f"Corrupt GRT free chain detected (group {current_group})."
                )
            visited.add(current_group)
            allocated_groups.append(current_group)
            current_group = self._grt[current_group]

        self._grt[0] = current_group
        for i in range(len(allocated_groups) - 1):
            self._grt[allocated_groups[i]] = allocated_groups[i + 1]
        self._grt[allocated_groups[-1]] = 0

        padded_data = data.ljust(num_sectors_needed * HDOS_BYTES_PER_SECTOR, b"\x00")
        data_area_start_lba = self._data_base_lba()
        for i, group_num in enumerate(allocated_groups):
            start_lba = data_area_start_lba + (group_num - 1) * spg
            sector_offset = i * spg
            for j in range(spg):
                lba = start_lba + j
                sector_index = sector_offset + j
                if sector_index < num_sectors_needed:
                    start = sector_index * HDOS_BYTES_PER_SECTOR
                    end = start + HDOS_BYTES_PER_SECTOR
                    self._write_lba(lba, padded_data[start:end])

        self._create_and_write_dir_entry(
            path, allocated_groups, num_sectors_needed, spg
        )

        self._write_lba(self.label.grt_start_block, self._grt)
        self.disk.flush()

        self._dir_entries = None
        self._grt = None
        self._init_completed = False
        self._data_base_lba_cache = None
        self.logger.info(f"Successfully wrote file '{path}' ({len(data)} bytes).")

    def _validate_filename(self, path: str) -> None:
        """
        Validates an HDOS filename before any destructive write step.

        Over-length names/extensions are accepted (they are truncated to the 8.3
        on-disk fields), but an empty name or a non-ASCII name is rejected up
        front so it cannot crash mid-write after the old file was deleted, or
        create an invisible, undeletable directory entry (audit hdos_fs.py:1223).

        Args:
            path: The file path to validate (e.g. "/NAME.EXT").

        Raises:
            ValueError: If the name is empty or contains non-ASCII characters.
        """
        raw = path.strip("/")
        name_part = raw.split(".", 1)[0]
        if not name_part:
            raise ValueError(f"HDOS filename must have a non-empty name: '{raw}'")
        if not raw.isascii():
            raise ValueError(f"HDOS filename must be ASCII: '{raw}'")

    def _calculate_file_size(self, entry: HDOSDirectoryEntry) -> int:
        """
        Calculates the allocated size of a file based on its directory entry.

        HDOS doesn't store exact file sizes, only which groups are allocated
        and how many sectors in the last group contain data.

        Args:
            entry: The directory entry for the file.

        Returns:
            The calculated file size in bytes.
        """
        if not self._grt or not self.label:
            return 0

        if entry.first_group == 0:
            return 0

        group_count = 0
        current_group = entry.first_group

        for _ in range(len(self._grt)):
            if current_group == 0:
                break
            if not (0 < current_group < len(self._grt)):
                self.logger.warning(
                    f"File '{entry.get_filename()}' has corrupt chain at "
                    f"group {current_group}."
                )
                return 0

            group_count += 1
            if current_group == entry.last_group:
                break
            current_group = self._grt[current_group]

        if group_count == 0:
            return 0

        sectors_per_group = (
            self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        )

        if group_count > 1:
            full_groups_sectors = (group_count - 1) * sectors_per_group
        else:
            full_groups_sectors = 0

        # Clamp the untrusted last-sector index so a corrupt entry cannot inflate
        # the reported file size (audit hdos_fs.py:1025).
        last_group_sectors = min(entry.last_sector_index, sectors_per_group)

        total_sectors = full_groups_sectors + last_group_sectors
        return total_sectors * HDOS_BYTES_PER_SECTOR

    def _create_and_write_dir_entry(
        self,
        path: str,
        allocated_groups: list[int],
        num_sectors: int,
        cluster_factor: Optional[int] = None,
    ) -> None:
        """
        Finds a free slot and writes a new directory entry to the disk.

        Uses pre-HDOS 3.0 format based on real disk analysis.

        Args:
            path: The filename path.
            allocated_groups: List of group numbers allocated for the file.
            num_sectors: Total number of sectors used by the file.
            cluster_factor: Sectors per group for this file (defaults to label
                cluster factor).

        Raises:
            IOError: If no free directory space available.
        """
        filename_upper = path.strip("/").upper()
        name, ext = (filename_upper.split(".") + [""])[:2]
        name_bytes = name.ljust(8).encode("ascii")[:8]
        ext_bytes = ext.ljust(3).encode("ascii")[:3]

        if cluster_factor is None:
            cluster_factor = (
                self.label.cluster_factor if self.label.cluster_factor > 0 else 1
            )

        spg = cluster_factor
        lsi = (num_sectors - 1) % spg + 1 if num_sectors > 0 else 0

        now = datetime.datetime.now()

        year_offset = min(now.year - 1970, 63)
        if now.year - 1970 > 63:
            self.logger.warning(f"Year {now.year} exceeds HDOS limit, capping to 2033")

        date_packed = (year_offset << 9) | (now.month << 5) | now.day

        flags = 0
        if filename_upper in HDOS_SYSTEM_FILES:
            flags = FLAGS_SYSTEM_CORE
            self.logger.debug(f"Setting system file flags 0xF0 for {filename_upper}")
        elif filename_upper in HDOS_DIRECT_SYS:
            flags = FLAGS_DIRECT
            self.logger.debug(f"Setting DIRECT.SYS flags 0xE0 for {filename_upper}")

        entry_bytes = bytearray(HDOS_DIR_ENTRY_SIZE)
        entry_bytes[0:8] = name_bytes
        entry_bytes[8:11] = ext_bytes
        entry_bytes[11] = 0
        entry_bytes[12] = 0
        entry_bytes[13] = cluster_factor
        entry_bytes[14] = flags
        entry_bytes[15] = 0
        entry_bytes[16] = allocated_groups[0] if allocated_groups else 0
        entry_bytes[17] = allocated_groups[-1] if allocated_groups else 0
        entry_bytes[18] = lsi
        struct.pack_into("<H", entry_bytes, 19, date_packed)
        struct.pack_into("<H", entry_bytes, 21, date_packed)

        current_dir_lba = self.label.dir_start_block
        for _ in range(20):
            if current_dir_lba == 0:
                raise OSError("No free directory space.")

            sector1 = self._read_lba(current_dir_lba)
            sector2 = self._read_lba(current_dir_lba + 1)

            if len(sector1) != HDOS_BYTES_PER_SECTOR:
                raise OSError(
                    f"Directory sector 1 size mismatch: {len(sector1)} != "
                    f"{HDOS_BYTES_PER_SECTOR}"
                )
            if len(sector2) != HDOS_BYTES_PER_SECTOR:
                raise OSError(
                    f"Directory sector 2 size mismatch: {len(sector2)} != "
                    f"{HDOS_BYTES_PER_SECTOR}"
                )

            dir_data = bytearray(sector1 + sector2)

            for i in range(DIR_ENTRIES_PER_BLOCK):
                offset = i * HDOS_DIR_ENTRY_SIZE
                if dir_data[offset] in (0x00, 0xFF, 0xFE):
                    was_end_marker = dir_data[offset] == 0xFE
                    dir_data[offset : offset + HDOS_DIR_ENTRY_SIZE] = entry_bytes

                    # If we overwrote the end-of-directory marker, restore it
                    # at the next slot so the scanner doesn't see stale data.
                    if was_end_marker and i + 1 < DIR_ENTRIES_PER_BLOCK:
                        next_offset = (i + 1) * HDOS_DIR_ENTRY_SIZE
                        dir_data[next_offset] = 0xFE

                    self._write_lba(
                        current_dir_lba, bytes(dir_data[:HDOS_BYTES_PER_SECTOR])
                    )
                    self._write_lba(
                        current_dir_lba + 1,
                        bytes(
                            dir_data[HDOS_BYTES_PER_SECTOR : HDOS_BYTES_PER_SECTOR * 2]
                        ),
                    )
                    self.logger.info(
                        f"Wrote directory entry for {filename_upper} with "
                        f"flags 0x{flags:02X}"
                    )
                    return

            current_dir_lba = struct.unpack_from(
                "<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET
            )[0]

        raise OSError("Could not find a free directory entry slot.")

    def _create_empty_file_entry(self, path: str) -> None:
        """
        Creates a directory entry for a zero-byte file.

        Args:
            path: The file path.
        """
        spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        self._create_and_write_dir_entry(path, [], 0, spg)
        self.disk.flush()
        self.logger.info(f"Successfully wrote empty file '{path}'.")

    def _create_label_sector_bytes(self, label: HDOSLabelRecord) -> bytes:
        """
        Creates the 256-byte label sector from an HDOSLabelRecord.

        Args:
            label: The HDOSLabelRecord to serialize.

        Returns:
            The 256-byte raw label sector.
        """
        sector = bytearray(HDOS_BYTES_PER_SECTOR)
        struct.pack_into("<H", sector, 3, label.dir_start_block)
        struct.pack_into("<H", sector, 5, label.grt_start_block)
        sector[7] = label.cluster_factor
        sector[0] = (
            label.volume_number
            if hasattr(label, "volume_number") and label.volume_number is not None
            else 0
        )

        title_bytes = label.title.encode("ascii", "ignore")
        sector[17 : 17 + len(title_bytes)] = title_bytes
        return bytes(sector)

    def _data_base_lba(self) -> int:
        """
        Returns LBA where group 1 starts.

        Per HDOS spec, this is LBA 2.

        Returns:
            The LBA of the first data group (always 2).

        Raises:
            RuntimeError: If called before the label has been parsed.
        """
        if self._data_base_lba_cache is not None:
            return self._data_base_lba_cache

        if not self.label:
            raise RuntimeError("_data_base_lba called before label was parsed.")

        self._data_base_lba_cache = 2
        return 2

    def _initialize(self) -> None:
        """
        Loads and parses the core HDOS filesystem structures from the disk.

        This method is idempotent and caches its results.

        Raises:
            ValueError: If the parsed Label Record contains invalid data.
        """
        if self._init_completed:
            return

        label_data = self._read_lba(HDOS_LABEL_SECTOR_LBA)
        self.label = HDOSLabelRecord.from_bytes(label_data)
        if not self.label.is_valid(self.disk.physical_format.total_sectors):
            raise ValueError(
                f"Parsed Label Record contains invalid pointers: {self.label}"
            )

        self.logger.info(
            f"HDOS Label Record parsed: Title='{self.label.title}', "
            f"Vol={self.label.volume_number}, "
            f"DiskClusterFactor={self.label.cluster_factor}, "
            f"DirStartLBA={self.label.dir_start_block}, "
            f"GRTStartLBA={self.label.grt_start_block}"
        )

        self._grt = bytearray(self._read_lba(self.label.grt_start_block))

        try:
            self._rgt = bytearray(self._read_lba(HDOS_RGT_SECTOR_LBA))
            self.logger.debug(f"RGT loaded from LBA {HDOS_RGT_SECTOR_LBA}")
        except (OSError, ValueError) as e:
            self.logger.warning(
                f"Could not read RGT: {e}. Proceeding without lock-out map."
            )
            self._rgt = None

        data_base = self._data_base_lba()

        spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        total_data_sectors = self.disk.physical_format.total_sectors - data_base
        num_groups_on_disk = total_data_sectors // spg
        if len(self._grt) <= num_groups_on_disk:
            num_groups_on_disk = min(num_groups_on_disk, len(self._grt) - 1)
        self._num_groups_on_disk = num_groups_on_disk

        self._dir_entries = self._read_directory_chain()
        self._init_completed = True

    def _is_group_locked_out(self, group_num: int) -> bool:
        """
        Checks if a group is locked out in the RGT.

        Per HDOS spec:
        - RGT byte value 0x01 = usable
        - RGT byte value 0x00 or 0x80-0xFF (negative) = locked out

        Args:
            group_num: The group number to check (1-based).

        Returns:
            True if the group is marked as locked out, False otherwise.
        """
        if not self._rgt or group_num < 1 or group_num >= len(self._rgt):
            return False

        rgt_value = self._rgt[group_num]
        return rgt_value == 0x00 or rgt_value >= 0x80

    def _lba_to_ts(self, lba: int) -> tuple[int, int]:
        """
        Converts a Logical Block Address (LBA) to (track, sector).

        Args:
            lba: The logical block address.

        Returns:
            Tuple of (track, sector).
        """
        spt = self.disk.physical_format.get_sectors_per_track(0, 0)
        track = lba // spt
        sector = lba % spt
        return track, sector

    def _parse_date(self, date_bytes: bytes) -> datetime.datetime:
        """
        Parses a 2-byte HDOS date field.

        Year field is 6 bits, so maximum offset is 63 years from 1970.

        Args:
            date_bytes: 2-byte date value.

        Returns:
            Parsed datetime or default datetime on error.
        """
        word = struct.unpack_from("<H", date_bytes)[0]
        day = word & 0x1F
        month = (word >> 5) & 0x0F
        year_offset = (word >> 9) & 0x3F
        year = 1970 + year_offset
        try:
            if not (1 <= month <= 12 and 1 <= day <= 31):
                return HDOS_DEFAULT_DATETIME
            return datetime.datetime(year, month, day)
        except (ValueError, TypeError):
            return HDOS_DEFAULT_DATETIME

    def _parse_single_dir_entry(self, data: bytes) -> Optional[HDOSDirectoryEntry]:
        """
        Helper to parse a single 23-byte directory entry.

        Uses pre-HDOS 3.0 format based on real disk analysis.

        Args:
            data: 23-byte directory entry data.

        Returns:
            HDOSDirectoryEntry object or None if invalid.
        """
        if not data or len(data) < HDOS_DIR_ENTRY_SIZE or data[0] in (0x00, 0xFF, 0xFE):
            return None

        hex_dump = " ".join(f"{b:02X}" for b in data)
        self.logger.debug(f"Raw directory entry: {hex_dump}")

        name_bytes = data[0:8]
        ext_bytes = data[8:11]
        byte_11 = data[11]
        byte_12 = data[12]
        cluster_factor = data[13]
        flags_byte = data[14]
        byte_15 = data[15]
        first_group = data[16]
        last_group = data[17]
        last_sector_index = data[18]
        creation_date_bytes = data[19:21]
        mod_date_bytes = data[21:23]

        name = "".join(chr(b & 0x7F) for b in name_bytes).strip("\x00").strip()
        if not name:
            return None
        ext = "".join(chr(b & 0x7F) for b in ext_bytes).strip("\x00").strip()

        creation_date = self._parse_date(creation_date_bytes)
        mod_date = self._parse_date(mod_date_bytes)

        attributes = ""
        if flags_byte & 0x80:
            attributes += "S"
        if flags_byte & 0x40:
            attributes += "L"
        if flags_byte & 0x20:
            attributes += "W"
        if flags_byte & 0x10:
            attributes += "C"
        if not attributes:
            attributes = "-"

        self.logger.debug(
            f"  File: {name}.{ext if ext else ''}, Flags: 0x{flags_byte:02X} "
            f"({attributes})"
        )
        self.logger.debug(
            f"  Cluster factor: {cluster_factor}, Groups: {first_group}-"
            f"{last_group}, LSI: {last_sector_index}"
        )
        self.logger.debug(
            f"  Bytes 11,12,15: {byte_11},{byte_12},{byte_15} (expect 0,0,0)"
        )
        self.logger.debug(f"  Created: {creation_date}, Modified: {mod_date}")

        return HDOSDirectoryEntry(
            raw_name=name_bytes,
            name=name,
            ext=ext,
            cluster_factor=cluster_factor,
            first_group=first_group,
            last_group=last_group,
            last_sector_index=last_sector_index,
            creation_date=creation_date,
            modification_date=mod_date,
            attributes=attributes,
        )

    def _read_directory_chain(self) -> list[HDOSDirectoryEntry]:
        """
        Reads the directory, which is a linked-list of 512-byte blocks.

        Returns:
            A list of all valid HDOSDirectoryEntry objects found.
        """
        entries: list[HDOSDirectoryEntry] = []
        current_block_lba = self.label.dir_start_block
        visited: set[int] = set()

        for _ in range(20):
            if current_block_lba == 0:
                break

            # Break self-referential / cyclic next-block pointers from a crafted
            # image so entries are not duplicated (audit hdos_fs.py:1605).
            if current_block_lba in visited:
                self.logger.warning(
                    f"Cyclic directory next-block pointer at LBA "
                    f"{current_block_lba}; stopping."
                )
                break
            visited.add(current_block_lba)

            try:
                sector1 = self._read_lba(current_block_lba)
                sector2 = self._read_lba(current_block_lba + 1)
                dir_data = sector1 + sector2
            except (OSError, ValueError) as e:
                self.logger.error(
                    f"Could not read full 512-byte directory block at LBA "
                    f"{current_block_lba}: {e}"
                )
                break

            end_of_dir_found = False
            for i in range(DIR_ENTRIES_PER_BLOCK):
                offset = i * HDOS_DIR_ENTRY_SIZE
                entry_data = dir_data[offset : offset + HDOS_DIR_ENTRY_SIZE]

                first_byte = entry_data[0]
                if first_byte == 0xFE:
                    end_of_dir_found = True
                    break
                if first_byte in [0x00, 0xFF]:
                    continue

                entry = self._parse_single_dir_entry(entry_data)
                if entry:
                    entries.append(entry)

            if end_of_dir_found:
                break

            current_block_lba = struct.unpack_from(
                "<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET
            )[0]

        return entries

    def _read_lba(self, lba: int) -> bytes:
        """
        Reads a single sector from the disk using its LBA.

        Args:
            lba: Logical block address.

        Returns:
            Sector data as bytes.
        """
        c, h, s = self.disk.physical_format.lba_to_chs(lba)
        return self.disk.read_sector(c, h, s)

    def _write_lba(self, lba: int, data: bytes) -> None:
        """
        Writes a single sector to the disk using its LBA.

        Args:
            lba: Logical block address.
            data: Data to write (must be sector-sized).
        """
        c, h, s = self.disk.physical_format.lba_to_chs(lba)
        self.disk.write_sector(c, h, s, data)
