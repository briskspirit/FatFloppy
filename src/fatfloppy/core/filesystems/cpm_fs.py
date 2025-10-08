import contextlib
import datetime
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger
from .fs_base import FileInfo, Filesystem

CPM_SECTOR_SIZE = 128
CPM_DIRECTORY_ENTRIES_PER_SECTOR = CPM_SECTOR_SIZE // 32
CPM_EXTENT_SIZE = 16 * 1024
CPM_BLOCK_SIZE_DEFAULT = 1024
CPM_DEFAULT_DATETIME = datetime.datetime(1978, 1, 1)
CPM_ATTR_RO = 0x80
CPM_ATTR_SYS = 0x40
CPM_RECORDS_PER_EXTENT = 128
CPM_EOF_CHAR = 0x1A
CPM_DELETED_ENTRY_MARKER = 0xE5


@dataclass
class CPMDiskParameterBlock:
    """
    Represents the CP/M Disk Parameter Block (DPB).

    The DPB defines the logical and physical layout of the disk, including
    sector and block sizes, directory size, and reserved areas.

    Attributes:
        spt: Sectors Per Track (total logical 128-byte sectors on a track).
        bsh: Block SHift factor (log2(data allocation block size / 128)).
        blm: BLock Mask (2^BSH - 1).
        exm: EXtent Mask (0 for 16k extents, 1 for 32k, etc.).
        dsm: DiSk Max allocation block number (total blocks - 1).
        drm: DiRectory Max entry number (total directory entries - 1).
        al0: ALlocation bitmap byte 0.
        al1: ALlocation bitmap byte 1.
        cks: ChecKSUM vector size (0 means directory is not checksummed).
        off: OFFset, number of reserved tracks.
    """

    spt: int = 0
    bsh: int = 0
    blm: int = 0
    exm: int = 0
    dsm: int = 0
    drm: int = 0
    al0: int = 0
    al1: int = 0
    cks: int = 0
    off: int = 0

    @property
    def block_size(self) -> int:
        """
        Calculates the data allocation block size in bytes.

        Returns:
            The block size in bytes.
        """
        return CPM_SECTOR_SIZE * (2**self.bsh)

    @property
    def directory_blocks(self) -> int:
        """
        Calculates how many allocation blocks are used by the directory.

        Returns:
            The number of directory blocks.
        """
        if self.block_size == 0:
            return 0
        return ((self.drm + 1) * 32 + self.block_size - 1) // self.block_size

    @property
    def max_file_size(self) -> int:
        """
        Calculates the maximum theoretical file size for this disk geometry.

        Returns:
            The maximum file size in bytes.
        """
        num_pointers_per_extent = 16 if self.dsm <= 255 else 8
        return (self.exm + 1) * num_pointers_per_extent * self.block_size


@dataclass
class CPMDirectoryEntry:
    """
    Represents a 32-byte CP/M directory entry.

    Each entry describes a file's extent, which is a portion of a file's data.
    A single file can be composed of multiple extents (directory entries).

    Attributes:
        user: User number (0-15 for normal files, 0xE5 for deleted).
        name: The 8-character filename.
        ext: The 3-character file extension.
        ex: Extent number, low byte.
        s1: Reserved / System use.
        xh: Extent number, high byte (CP/M 3+) or S2 for CP/M 2.2.
        rc: Record Count (number of 128-byte records in this extent).
        blks: Allocation block pointers.
        attributes_raw: Raw attribute bits from the directory entry.
    """

    user: int = 0
    name: str = ""
    ext: str = ""
    ex: int = 0
    s1: int = 0
    xh: int = 0
    rc: int = 0
    blks: list[int] = field(default_factory=list)
    attributes_raw: dict[str, int] = field(default_factory=dict)

    def get_attributes(self) -> str:
        """
        Generates a human-readable string of the file's attributes.

        Returns:
            A string representing the file attributes (e.g., "U0-R-S").
        """
        attr_str_parts = [f"U{self.user}"]
        if self.attributes_raw.get("t1", 0) & 0x80:
            attr_str_parts.append("R")
        if self.attributes_raw.get("t2", 0) & 0x80:
            attr_str_parts.append("S")
        if self.attributes_raw.get("t3", 0) & 0x80:
            attr_str_parts.append("A")
        return "-".join(attr_str_parts)

    def get_filename(self) -> str:
        """
        Constructs the full 8.3 filename from the entry.

        Returns:
            The formatted filename string (e.g., "FILENAME.EXT").
        """
        name_clean = "".join(chr(ord(c) & 0x7F) for c in self.name).strip()
        ext_clean = "".join(chr(ord(c) & 0x7F) for c in self.ext).strip()
        return f"{name_clean}.{ext_clean}"

    def is_deleted(self) -> bool:
        """
        Checks if the directory entry is marked as deleted.

        Returns:
            True if deleted, False otherwise.
        """
        return self.user == CPM_DELETED_ENTRY_MARKER


class CPMFilesystem(Filesystem):
    """
    Provides an interface to a CP/M filesystem on a disk image.

    This class handles filesystem detection, metadata parsing (DPB), and
    file operations like reading, writing, deleting, and listing files.
    """

    filesystem_type: ClassVar[str] = "CPM"
    filesystem_aliases: ClassVar[list[str]] = ["CP/M"]
    validity_threshold: ClassVar[int] = 50
    config_class: ClassVar[type] = CPMDiskParameterBlock
    VALIDITY_THRESHOLD = validity_threshold

    def __init__(self, disk: Disk, config: Optional[CPMDiskParameterBlock] = None):
        """
        Initializes the CPMFilesystem instance.

        Args:
            disk: The Disk object to operate on.
            config: Optional CP/M Disk Parameter Block for this filesystem.
        """
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self.dpb: Optional[CPMDiskParameterBlock] = config
        self._init_completed = False
        self._cached_directory: Optional[list[CPMDirectoryEntry]] = None
        self._cached_allocation_map: Optional[set[int]] = None
        self._cached_validity_score: Optional[int] = None

        if self.disk and self.disk.physical_format:
            if self.dpb:
                try:
                    self._initialize_parameters()
                    self._init_completed = True
                    self.logger.info(
                        "CP/M Filesystem initialized successfully with provided DPB."
                    )
                except ValueError as e:
                    self.logger.error(
                        f"CP/M Initialization failed with provided DPB: {e}"
                    )
            else:
                if self._try_derive_dpb():
                    try:
                        self._initialize_parameters()
                        self._init_completed = True
                        self.logger.info(
                            "CP/M Filesystem initialized with derived DPB based on physical format."
                        )
                    except ValueError as e:
                        self.logger.error(
                            f"CP/M Initialization failed with derived DPB: {e}"
                        )
                else:
                    self.logger.warning(
                        "CP/M Filesystem initialized without a DPB. "
                        "get_validity_score() will be required to confirm format."
                    )
        else:
            self.logger.warning(
                "CP/M Filesystem initialized without disk or physical format."
            )

    @property
    def allocation_unit_size(self) -> int:
        """
        Returns the size of a single allocation block in bytes.

        Returns:
            The allocation unit size in bytes.
        """
        if self.dpb:
            return self.dpb.block_size
        return 0

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        """
        Returns all CP/M format definitions provided by this plugin.

        Returns:
            Dictionary mapping format names to FormatProfile objects.
        """
        from .formats.cpm_formats import CPM_FORMATS

        return CPM_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        """
        Compares two CP/M configuration objects for equality.

        Args:
            config1: The first configuration object.
            config2: The second configuration object.

        Returns:
            True if the configurations match, False otherwise.
        """
        if not isinstance(config1, CPMDiskParameterBlock) or not isinstance(
            config2, CPMDiskParameterBlock
        ):
            return False
        return (
            config1.spt == config2.spt
            and config1.bsh == config2.bsh
            and config1.dsm == config2.dsm
            and config1.off == config2.off
        )

    @staticmethod
    def create_config_from_params(
        format_info: dict[str, Any], physical_format: PhysicalFormat
    ) -> Optional[CPMDiskParameterBlock]:
        """
        Creates a CPMDiskParameterBlock config from parameters.

        Args:
            format_info: Dictionary containing CP/M parameters.
            physical_format: The physical format of the disk.

        Returns:
            A configured CPMDiskParameterBlock object, or None on error.
        """
        logger = get_logger("CPMFilesystem")

        try:
            bsh = format_info.get("bsh", 3)
            return CPMDiskParameterBlock(
                spt=format_info.get(
                    "spt",
                    physical_format.track_formats[0].sectors_per_track
                    * (physical_format.bytes_per_sector // 128),
                ),
                bsh=bsh,
                blm=format_info.get("blm", (2**bsh) - 1),
                exm=format_info.get("exm", 0),
                dsm=format_info.get(
                    "dsm",
                    (
                        physical_format.total_sectors
                        * (physical_format.bytes_per_sector // 128)
                    )
                    // (2**bsh)
                    - 10,
                ),
                drm=format_info.get("drm", 63),
                al0=format_info.get("al0", 0xC0),
                al1=format_info.get("al1", 0x00),
                cks=format_info.get("cks", 0),
                off=format_info.get("off", 2),
            )
        except Exception as e:
            logger.error(f"Error creating CP/M config: {e}", exc_info=True)
            return None

    def check(self) -> bool:
        """
        Performs a basic consistency check on the filesystem.

        Returns:
            True if the filesystem appears consistent, False otherwise.
        """
        self.logger.warning("Filesystem check not implemented for this type.")
        return True

    def create_directory(self, _path: str) -> None:
        """
        CP/M does not support hierarchical directories.

        Args:
            path: The directory path, unused.

        Raises:
            NotImplementedError: Always raised as CP/M does not support directories.
        """
        self.logger.warning(
            "CP/M 2.2 does not support traditional directory creation via this method."
        )
        raise NotImplementedError(
            "CP/M create_directory not applicable in the standard sense."
        )

    def delete(self, path: str) -> None:
        """
        Deletes a file by marking its directory entries as unused (0xE5).

        Args:
            path: The path to the file to delete (e.g., "U0:FILENAME.EXT").

        Raises:
            IOError: If the filesystem is not considered valid.
            FileNotFoundError: If the specified file does not exist.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem not valid")

        user, parsed_filename = self._parse_cpm_path(path)

        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()

        indices_to_delete = []
        for i, entry in enumerate(self._cached_directory):
            if (
                not entry.is_deleted()
                and entry.user == user
                and entry.get_filename().upper() == parsed_filename.upper()
            ):
                indices_to_delete.append(i)

        if not indices_to_delete:
            raise FileNotFoundError(f"File '{path}' not found.")

        sectors_to_modify = defaultdict(bytearray)
        for index in indices_to_delete:
            cpm_track, log_sec, offset = self._map_dir_entry_index_to_location(index)
            key = (cpm_track, log_sec)

            if key not in sectors_to_modify:
                sectors_to_modify[key] = bytearray(
                    self._read_logical_sector(cpm_track, log_sec)
                )

            sectors_to_modify[key][offset] = CPM_DELETED_ENTRY_MARKER

        for (cpm_track, log_sec), data in sectors_to_modify.items():
            self._write_logical_sector(cpm_track, log_sec, bytes(data))

        self.logger.info(
            f"Deleted file '{path}' by marking {len(indices_to_delete)} directory entries."
        )
        self._cached_directory = None
        self._cached_allocation_map = None
        self.disk.flush()

    def delete_recursive(self, path: str) -> bool:
        """
        Deletes a file (CP/M has no directories, so this is an alias for delete).

        Args:
            path: The path to the file to delete.

        Returns:
            True if deletion was successful, False otherwise.
        """
        try:
            self.delete(path)
            return True
        except Exception:
            return False

    def format_fs(
        self,
        profile: FormatProfile,
        volume_label: Optional[str] = None,  # noqa: ARG002
    ) -> None:
        """
        Formats the disk with a CP/M filesystem layout.

        This involves clearing the system tracks and initializing the directory area
        with 0xE5 bytes.

        Args:
            profile: The FormatProfile containing the target DPB.
            volume_label: Not used for CP/M (parameter kept for interface compatibility).

        Raises:
            ValueError: If the profile does not contain a valid CPMDiskParameterBlock.
            IOError: If writing to the disk fails.
        """
        if not isinstance(profile.filesystem_config, CPMDiskParameterBlock):
            raise ValueError(
                "FormatProfile for CP/M must contain a CPMDiskParameterBlock."
            )

        original_dpb = self.dpb
        self.dpb = profile.filesystem_config
        try:
            self._initialize_parameters()
        except ValueError as e:
            self.dpb = original_dpb
            raise ValueError(
                f"Failed to re-initialize parameters with new DPB for format: {e}"
            ) from e

        self.logger.info(f"Formatting disk with CP/M profile: {profile.name}")

        num_reserved_cpm_tracks = self.dpb.off
        self.logger.info(f"Clearing {num_reserved_cpm_tracks} reserved CP/M tracks...")
        for i in range(num_reserved_cpm_tracks):
            phys_cyl, phys_head = self._cpm_track_to_chs_coords(i)
            spt = self.disk.physical_format.get_sectors_per_track(phys_cyl, phys_head)
            bps = self.disk.physical_format.get_bytes_per_sector(phys_cyl, phys_head)
            fill_data = bytes([CPM_DELETED_ENTRY_MARKER] * bps)
            for s in range(1, spt + 1):
                try:
                    self.disk.write_sector(phys_cyl, phys_head, s, fill_data)
                except Exception as e:
                    self.logger.error(
                        f"Error writing to reserved track {i} "
                        f"(C:{phys_cyl} H:{phys_head} S:{s}): {e}"
                    )
                    raise OSError("Failed to clear system tracks during format") from e

        dir_logical_128b_sectors_count = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        self.logger.info(
            f"Clearing {dir_logical_128b_sectors_count} logical 128-byte sectors for directory..."
        )
        blank_sector_128b = bytes([CPM_DELETED_ENTRY_MARKER] * CPM_SECTOR_SIZE)
        current_cpm_track_idx = self.dpb.off
        current_logical_128b_sec_on_cpm_track_idx = 0

        for _ in range(dir_logical_128b_sectors_count):
            try:
                self._write_logical_sector(
                    current_cpm_track_idx,
                    current_logical_128b_sec_on_cpm_track_idx,
                    blank_sector_128b,
                )
            except Exception as e:
                self.logger.error(
                    f"Error writing to directory area CP/M_T:{current_cpm_track_idx} "
                    f"Log.S:{current_logical_128b_sec_on_cpm_track_idx}: {e}"
                )
                raise OSError("Failed to clear directory area during format") from e

            logical_spt_for_dir_track = self._get_logical_spt(current_cpm_track_idx)
            current_logical_128b_sec_on_cpm_track_idx += 1
            if (
                logical_spt_for_dir_track > 0
                and current_logical_128b_sec_on_cpm_track_idx
                >= logical_spt_for_dir_track
            ):
                current_logical_128b_sec_on_cpm_track_idx = 0
                current_cpm_track_idx += 1

        self.logger.info("Data area not explicitly cleared (standard for CP/M format).")
        self._cached_directory = []
        self._cached_allocation_map = set(range(self.dpb.directory_blocks))
        self.disk.flush()
        self.logger.info(
            "CP/M formatting complete (system tracks and directory cleared)."
        )

    def get_allocated_units(self) -> list[int]:
        """
        Returns a sorted list of all allocated block numbers.

        Returns:
            A list of integers representing the used block numbers.
        """
        if self.get_validity_score() < self.validity_threshold or not self.dpb:
            return []
        if self._cached_allocation_map is None:
            self._load_allocation_map()

        return (
            sorted(self._cached_allocation_map) if self._cached_allocation_map else []
        )

    def get_disk_map_layout(self) -> dict[str, Any]:
        """
        Provides data for visualizing the disk layout.

        Returns:
            A dictionary containing layout information for disk visualization.
        """
        if (
            not self.dpb
            or not self.disk
            or not self.disk.physical_format
            or not self._init_completed
        ):
            return {}

        allocated_data_blocks = self.get_allocated_units()

        reserved_logical_sectors = 0
        for t in range(self.dpb.off):
            reserved_logical_sectors += self._get_logical_spt(t)

        dir_logical_sectors = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        first_data_sector = reserved_logical_sectors + dir_logical_sectors

        def get_cpm_sector_type_lba(phys_lba: int) -> str:
            """
            Determines sector type based on LBA address.

            For variable sector size disks, we manually walk through tracks to find
            which logical sector this LBA represents.
            """
            try:
                logical_sector_count = 0

                for cpm_track in range(
                    self.disk.physical_format.cylinders
                    * self.disk.physical_format.heads
                ):
                    if self.disk.physical_format.heads > 1:
                        phys_cyl = cpm_track // self.disk.physical_format.heads
                        phys_head = cpm_track % self.disk.physical_format.heads
                    else:
                        phys_cyl = cpm_track
                        phys_head = 0

                    try:
                        self.disk.physical_format.get_track_format(phys_cyl, phys_head)
                    except ValueError:
                        continue

                    logical_spt = self._get_logical_spt(cpm_track)
                    track_start_lba = logical_sector_count
                    track_end_lba = logical_sector_count + logical_spt

                    if track_start_lba <= phys_lba < track_end_lba:
                        if cpm_track < self.dpb.off:
                            return "system"

                        if phys_lba < reserved_logical_sectors + dir_logical_sectors:
                            return "directory"

                        data_logical_sector_offset = phys_lba - (
                            reserved_logical_sectors + dir_logical_sectors
                        )

                        if self.dpb.block_size == 0:
                            return "data_free"

                        logical_sectors_per_block = (
                            self.dpb.block_size // CPM_SECTOR_SIZE
                        )
                        if logical_sectors_per_block == 0:
                            return "data_free"

                        cpm_alloc_block_num = (
                            data_logical_sector_offset // logical_sectors_per_block
                        )

                        return (
                            "data_used"
                            if cpm_alloc_block_num in allocated_data_blocks
                            else "data_free"
                        )

                    logical_sector_count += logical_spt

                return "unknown"

            except Exception as e:
                self.logger.error(
                    f"Error determining sector type for LBA {phys_lba}: {e}"
                )
                return "unknown"

        legend_colors = {
            "System Tracks": "#A0A0A0",
            "Directory": "#FFFF00",
            "Used Data Block": "#FF00FF",
            "Free Data Block": "#808080",
        }
        legend = [
            ("System Tracks", legend_colors["System Tracks"]),
            ("Directory", legend_colors["Directory"]),
            ("Used Data Block", legend_colors["Used Data Block"]),
            ("Free Data Block", legend_colors["Free Data Block"]),
        ]
        type_map = {
            "system": legend_colors["System Tracks"],
            "directory": legend_colors["Directory"],
            "data_used": legend_colors["Used Data Block"],
            "data_free": legend_colors["Free Data Block"],
            "unknown": "#008B8B",
        }

        data_track_format = self.disk.physical_format.get_track_format(self.dpb.off, 0)
        alloc_unit_phys_sectors = (
            self.dpb.block_size // data_track_format.bytes_per_sector
            if data_track_format.bytes_per_sector > 0
            else 1
        )

        return {
            "legend": legend,
            "get_sector_type": get_cpm_sector_type_lba,
            "allocation_unit_size_sectors": alloc_unit_phys_sectors,
            "first_data_sector": first_data_sector,
            "type_color_map": type_map,
        }

    def get_display_info(self) -> dict[str, str]:
        """
        Returns a dictionary of key CP/M filesystem parameters for display.

        Returns:
            A dictionary of filesystem properties.
        """
        if not self.dpb:
            return {"Error": "CP/M DPB not available."}
        return {
            "Filesystem Type": "CP/M",
            "Sectors Per Track (DPB SPT - logical 128b)": str(self.dpb.spt),
            "Block Shift (BSH)": str(self.dpb.bsh),
            "Block Mask (BLM)": hex(self.dpb.blm),
            "Extent Mask (EXM)": hex(self.dpb.exm),
            "Max Alloc Block (DSM)": str(self.dpb.dsm),
            "Max Dir Entries (DRM+1)": str(self.dpb.drm + 1),
            "Dir Alloc Bytes (AL0,AL1)": f"{hex(self.dpb.al0)}, {hex(self.dpb.al1)}",
            "Checksum Vector Size (CKS)": str(self.dpb.cks),
            "Reserved Tracks (OFF)": str(self.dpb.off),
            "Calculated Block Size": f"{self.dpb.block_size} bytes",
            "Calculated Directory Blocks": str(self.dpb.directory_blocks),
        }

    def get_file_allocation_units(self, path: str) -> list[int]:
        """
        Gets the list of allocation block numbers used by a specific file.

        Args:
            path: The path to the file (e.g., "FILENAME.EXT" or "U0:FILENAME.EXT").

        Returns:
            A list of allocation block numbers used by the file, in order of extents.
            Returns an empty list if the file doesn't exist or has no allocated blocks.

        Raises:
            IOError: If the filesystem is not valid.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as CP/M.")

        user, parsed_filename = self._parse_cpm_path(path)

        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()

        file_extents = []
        for entry in self._cached_directory:
            if (
                not entry.is_deleted()
                and entry.user == user
                and entry.get_filename().upper() == parsed_filename.upper()
            ):
                file_extents.append(entry)

        if not file_extents:
            self.logger.warning(f"File '{path}' not found in directory.")
            return []

        file_extents.sort(key=lambda e: (e.ex | (e.xh << 8)))

        all_blocks = []
        for extent in file_extents:
            for block_num in extent.blks:
                if 0 < block_num <= self.dpb.dsm:
                    all_blocks.append(block_num)

        self.logger.debug(f"File '{path}' uses {len(all_blocks)} blocks: {all_blocks}")
        return all_blocks

    def get_free_space(self) -> tuple[int, int]:
        """
        Calculates the free and total data space on the disk.

        Returns:
            A tuple containing (free_bytes, total_bytes).
        """
        if self.get_validity_score() < self.validity_threshold or not self.dpb:
            return 0, 0

        total_alloc_blocks_on_disk = self.dpb.dsm + 1
        total_data_bytes_possible = total_alloc_blocks_on_disk * self.dpb.block_size
        allocated_block_count = len(self.get_allocated_units())
        free_blocks = total_alloc_blocks_on_disk - allocated_block_count
        free_bytes = free_blocks * self.dpb.block_size

        return free_bytes, total_data_bytes_possible

    def get_specific_config(self) -> Optional[CPMDiskParameterBlock]:
        """
        Returns the specific filesystem configuration object.

        Returns:
            The CPMDiskParameterBlock object, or None if not initialized.
        """
        return self.dpb

    def get_validity_score(self) -> int:
        """
        Scores the likelihood that the disk contains a valid CP/M filesystem.

        Returns:
            An integer score from 0 (not CP/M) to 100 (definitely CP/M).
        """
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        score = 0
        if not self.disk or not self.disk.physical_format or not self.dpb:
            self.logger.debug(
                "Score: 0 (No disk, physical format, or DPB for validation.)"
            )
            return 0

        try:
            self._initialize_parameters()
            score += 5

            dir_logical_sectors = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
            current_cpm_track = self.dpb.off
            logical_sector_idx = 0

            all_dir_bytes = bytearray()

            for _ in range(dir_logical_sectors):
                try:
                    sector_data = self._read_logical_sector(
                        current_cpm_track, logical_sector_idx
                    )
                    all_dir_bytes.extend(sector_data)

                    logical_spt = self._get_logical_spt(current_cpm_track)
                    if logical_spt > 0:
                        logical_sector_idx += 1
                        if logical_sector_idx >= logical_spt:
                            logical_sector_idx = 0
                            current_cpm_track += 1
                    else:
                        break
                except Exception as e:
                    self.logger.debug(f"Score: 0 (Cannot read directory sector: {e})")
                    self._cached_validity_score = 0
                    return 0

            if all(b == CPM_DELETED_ENTRY_MARKER for b in all_dir_bytes):
                score += 75
                final_score = min(100, score)
                self.logger.info(
                    f"CP/M validation score (empty formatted): {final_score}"
                )
                self._cached_validity_score = final_score
                return final_score

            total_entries = len(all_dir_bytes) // 32
            plausible_entries = 0
            active_entries_count = 0
            valid_filenames_count = 0

            for i in range(total_entries):
                entry_bytes = all_dir_bytes[i * 32 : (i + 1) * 32]
                user_num = entry_bytes[0]

                if user_num == CPM_DELETED_ENTRY_MARKER:
                    plausible_entries += 1
                    continue

                if 0 <= user_num <= 15:
                    active_entries_count += 1
                    raw_name_and_ext = entry_bytes[1:12]

                    name_hex = " ".join(f"{b:02X}" for b in entry_bytes[1:9])
                    ext_hex = " ".join(f"{b:02X}" for b in entry_bytes[9:12])
                    self.logger.debug(
                        f"Entry {i} (U{user_num}): name=[{name_hex}] ext=[{ext_hex}]"
                    )

                    significant_bytes = [
                        b for b in raw_name_and_ext if b != 0 and b != 0x20
                    ]

                    if not significant_bytes:
                        self.logger.debug(
                            f"Score: 0 (Entry {i}: active user {user_num} with null filename - "
                            "wrong interleave)"
                        )
                        self._cached_validity_score = 0
                        return 0

                    for b in significant_bytes:
                        if b == 0x7F or b == 0xFF:
                            self.logger.debug(
                                f"Score: 0 (Entry {i}: DEL/0xFF char 0x{b:02X}, user={user_num})"
                            )
                            self._cached_validity_score = 0
                            return 0

                        masked = b & 0x7F

                        if b >= 0x80:
                            if not (
                                (ord("A") <= masked <= ord("Z"))
                                or (ord("0") <= masked <= ord("9"))
                                or masked == ord("-")
                                or masked == ord("_")
                            ):
                                self.logger.debug(
                                    f"Score: 0 (Entry {i}: invalid high-bit char 0x{b:02X} "
                                    f"(masked={chr(masked) if 32 <= masked <= 126 else '?'}), "
                                    f"user={user_num})"
                                )
                                self._cached_validity_score = 0
                                return 0
                        else:
                            if not (0x21 <= b <= 0x7E):
                                self.logger.debug(
                                    f"Score: 0 (Entry {i}: invalid char 0x{b:02X}, "
                                    f"user={user_num})"
                                )
                                self._cached_validity_score = 0
                                return 0

                    plausible_entries += 1
                    valid_filenames_count += 1
                    self.logger.debug(f"Entry {i}: VALID")

            if plausible_entries < 1:
                self.logger.debug(
                    f"Score: 0 (Found {plausible_entries} plausible entries)"
                )
                self._cached_validity_score = 0
                return 0

            score += 40

            if valid_filenames_count > 0:
                filename_bonus = min(20, valid_filenames_count * 2)
                score += filename_bonus
                self.logger.debug(
                    f"Found {valid_filenames_count} valid filenames, bonus={filename_bonus}"
                )

            if active_entries_count > 0:
                valid_blocks = 0
                total_blocks = 0

                for i in range(total_entries):
                    entry_bytes = all_dir_bytes[i * 32 : (i + 1) * 32]
                    user_num = entry_bytes[0]

                    if user_num != CPM_DELETED_ENTRY_MARKER and 0 <= user_num <= 15:
                        if self.dpb.dsm > 255:
                            for j in range(8):
                                ptr_bytes = entry_bytes[16 + j * 2 : 16 + j * 2 + 2]
                                if len(ptr_bytes) == 2:
                                    block = struct.unpack("<H", ptr_bytes)[0]
                                    if block == 0:
                                        continue
                                    total_blocks += 1
                                    if 0 < block <= self.dpb.dsm:
                                        valid_blocks += 1
                        else:
                            for j in range(16):
                                block = entry_bytes[16 + j]
                                if block == 0:
                                    continue
                                total_blocks += 1
                                if 0 < block <= self.dpb.dsm:
                                    valid_blocks += 1

                if total_blocks > 0:
                    block_ratio = valid_blocks / total_blocks
                    if block_ratio < 0.8:
                        self.logger.debug(
                            f"Score: 0 (Too many invalid blocks: {valid_blocks}/{total_blocks})"
                        )
                        self._cached_validity_score = 0
                        return 0
                    score += int(30 * block_ratio)

            final_score = min(100, int(score))
            self.logger.info(
                f"CP/M validation score: {final_score} ({valid_filenames_count} valid files)"
            )
            self._cached_validity_score = final_score
            return final_score

        except Exception as e:
            self.logger.debug(f"get_validity_score check failed with exception: {e}")
            self._cached_validity_score = 0
            return 0

    def get_volume_label(self) -> Optional[str]:
        """
        CP/M does not have a standard volume label concept.

        Returns:
            Always returns None for CP/M filesystems.
        """
        return None

    def list_directory(self, path: str) -> list[FileInfo]:
        """
        Lists all files in the root directory.

        Args:
            path: Must be "/" as CP/M does not support subdirectories.

        Returns:
            A list of FileInfo objects representing the files.

        Raises:
            IOError: If the filesystem is not valid.
            NotImplementedError: If a path other than "/" is provided.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as CP/M.")
        if path != "/":
            raise NotImplementedError("Subdirectories not supported in CP/M")

        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()

        file_groups = defaultdict(list)
        for entry in self._cached_directory:
            if not entry.is_deleted() and 0 <= entry.user <= 15:
                key = (entry.user, entry.get_filename())
                file_groups[key].append(entry)

        files = []
        for key, group in file_groups.items():
            _user, full_name = key
            group.sort(key=lambda e: (e.ex | (e.xh << 8)))

            total_rc = sum(e.rc for e in group)
            size = total_rc * CPM_SECTOR_SIZE
            attr = group[0].get_attributes() if group else "-"

            files.append(
                FileInfo(
                    name=full_name,
                    size=size,
                    is_dir=False,
                    datetime=CPM_DEFAULT_DATETIME,
                    attributes=attr,
                    starting_cluster=0,
                    extra_data=group,
                )
            )
        return files

    def read_file(self, path: str) -> bytes:
        """
        Reads the complete content of a specified file.

        This method will search for the file across all user areas unless a
        specific user is provided (e.g., "U5:MYFILE.TXT"). If multiple files
        with the same name exist under different users, it reads the one from
        the lowest user number.

        Args:
            path: The path of the file to read.

        Returns:
            The binary content of the file.

        Raises:
            IOError: If the filesystem is not valid.
            FileNotFoundError: If the file cannot be found.
            ValueError: If the filename format is invalid.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as CP/M.")

        path = path.lstrip("/")
        specified_user: Optional[int] = None
        filename: str

        if ":" in path:
            try:
                user_part, file_part = path.split(":", 1)
                if user_part.upper().startswith("U") and user_part[1:].isdigit():
                    specified_user = int(user_part[1:])
                    filename = file_part
                else:
                    filename = path
            except ValueError:
                filename = path
        else:
            filename = path

        if "." not in filename:
            raise ValueError(f"Invalid filename format: {filename}")

        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()

        all_file_groups = self._group_extents(self._cached_directory)
        matching_groups = []
        search_filename_key = filename.upper()
        for (user, file_key), group in all_file_groups.items():
            if file_key == search_filename_key and (
                specified_user is None or user == specified_user
            ):
                matching_groups.append(group)

        if not matching_groups:
            raise FileNotFoundError(f"File {path} not found")
        if len(matching_groups) > 1:
            self.logger.warning(
                f"File '{filename}' exists for multiple users; reading from lowest user number."
            )
            matching_groups.sort(key=lambda g: g[0].user)

        group = matching_groups[0]
        data = bytearray()
        _name_part, ext_part = filename.split(".", 1)
        text_exts = ["ASM", "PRN", "BAS", "TXT", "DOC", "HEX"]
        is_text = ext_part.upper() in text_exts

        for entry in group:
            extent_data = bytearray()
            for block in entry.blks:
                if block != 0:
                    block_data = self._read_block(block)
                    extent_data.extend(block_data)
            used_data = extent_data[: entry.rc * CPM_SECTOR_SIZE]
            if is_text:
                used_data = bytes(b & 0x7F for b in used_data)
            data.extend(used_data)

        return bytes(data).rstrip(bytes([CPM_EOF_CHAR]))

    def write_file(self, path: str, data: bytes) -> None:
        """
        Writes data to a file on the CP/M filesystem.

        This function will first delete the file if it already exists, then find
        free directory entries and data blocks to store the new content.

        Args:
            path: The path of the file to write (e.g., "U0:NEWFILE.TXT").
            data: The binary data to write to the file.

        Raises:
            IOError: If the filesystem is invalid, there's not enough space,
                     or the directory is full.
            ValueError: If the DPB is not set.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem not valid")

        user, parsed_filename = self._parse_cpm_path(path)
        base_name, ext_name = (parsed_filename.split(".", 1) + [""])[:2]

        with contextlib.suppress(FileNotFoundError):
            self.delete(path)

        if not self.dpb:
            raise ValueError("DPB not set.")
        block_size = self.dpb.block_size
        if block_size == 0:
            raise OSError("Block size is zero, cannot write file.")

        num_records_total = (
            (len(data) + CPM_SECTOR_SIZE - 1) // CPM_SECTOR_SIZE if data else 0
        )
        num_dir_entries_needed = (
            (num_records_total + CPM_RECORDS_PER_EXTENT - 1) // CPM_RECORDS_PER_EXTENT
            if data
            else 0
        )
        num_blocks_needed = (len(data) + block_size - 1) // block_size if data else 0

        self.logger.info(
            f"Writing '{path}': {len(data)} bytes, needs {num_blocks_needed} "
            f"blocks, {num_dir_entries_needed} dir entries."
        )

        self._cached_directory = self._read_directory_entries()
        self._load_allocation_map()

        free_blocks = sorted(set(range(self.dpb.dsm + 1)) - self._cached_allocation_map)
        if len(free_blocks) < num_blocks_needed:
            raise OSError(
                f"Not enough free space. Required: {num_blocks_needed}, "
                f"Available: {len(free_blocks)}."
            )
        blocks_to_use = free_blocks[:num_blocks_needed]

        free_dir_slots = [
            i for i, e in enumerate(self._cached_directory) if e.is_deleted()
        ]
        if len(free_dir_slots) < num_dir_entries_needed:
            raise OSError(
                f"Directory is full. Required: {num_dir_entries_needed}, "
                f"Available: {len(free_dir_slots)}."
            )
        dir_slots_to_use = free_dir_slots[:num_dir_entries_needed]

        records_rem = num_records_total
        blocks_consumed = 0
        sectors_to_modify = defaultdict(bytearray)

        for i in range(num_dir_entries_needed):
            rc = min(records_rem, CPM_RECORDS_PER_EXTENT)
            records_rem -= rc

            bytes_in_this_extent = rc * CPM_SECTOR_SIZE
            blocks_for_this_extent = (
                bytes_in_this_extent + block_size - 1
            ) // block_size
            extent_blocks = blocks_to_use[
                blocks_consumed : blocks_consumed + blocks_for_this_extent
            ]
            blocks_consumed += blocks_for_this_extent

            new_entry = CPMDirectoryEntry(
                user=user,
                name=base_name,
                ext=ext_name,
                ex=i,
                s1=0,
                xh=0,
                rc=rc,
                blks=extent_blocks,
                attributes_raw={},
            )
            entry_bytes = self._format_entry_to_bytes(new_entry)

            cpm_track, log_sec, offset = self._map_dir_entry_index_to_location(
                dir_slots_to_use[i]
            )
            key = (cpm_track, log_sec)

            if key not in sectors_to_modify:
                sectors_to_modify[key] = bytearray(
                    self._read_logical_sector(cpm_track, log_sec)
                )
            sectors_to_modify[key][offset : offset + 32] = entry_bytes

        for (cpm_track, log_sec), mod_data in sectors_to_modify.items():
            self._write_logical_sector(cpm_track, log_sec, bytes(mod_data))

        data_to_write = bytearray(data)
        if len(data_to_write) > 0 and len(data_to_write) % CPM_SECTOR_SIZE != 0:
            padding_needed = CPM_SECTOR_SIZE - (len(data_to_write) % CPM_SECTOR_SIZE)
            data_to_write.extend([CPM_EOF_CHAR] * padding_needed)

        for i, block_num in enumerate(blocks_to_use):
            chunk = data_to_write[i * block_size : (i + 1) * block_size]
            self._write_block(block_num, bytes(chunk))

        self.logger.info(f"Successfully wrote file '{path}'.")
        self._cached_directory = None
        self._cached_allocation_map = None
        self.disk.flush()

    def _block_to_track_sector(self, block_num: int) -> tuple[int, int]:
        """
        Converts a data block number to its starting logical track and sector.

        Args:
            block_num: The block number to convert.

        Returns:
            A tuple of (cpm_track, logical_sector_on_track).

        Raises:
            ValueError: If the DPB is not set or block_size is zero.
        """
        if not self.dpb:
            raise ValueError("DPB not set.")
        if self.dpb.block_size == 0:
            raise ValueError("DPB.block_size is zero.")

        logical_128byte_sectors_per_alloc_block = self.dpb.block_size // CPM_SECTOR_SIZE
        start_logical_128byte_sector_for_block_global = (
            block_num * logical_128byte_sectors_per_alloc_block
        )
        current_track = self.dpb.off
        sectors_remaining = start_logical_128byte_sector_for_block_global

        while True:
            spt_for_current_track = self._get_logical_spt(current_track)
            if spt_for_current_track == 0:
                raise ValueError(
                    f"Logical SPT for track {current_track} is zero, cannot map block."
                )
            if sectors_remaining < spt_for_current_track:
                return current_track, sectors_remaining
            sectors_remaining -= spt_for_current_track
            current_track += 1

    def _cpm_track_to_chs_coords(self, cpm_track: int) -> tuple[int, int]:
        """
        Converts a linear CP/M track number to physical CHS coordinates.

        Args:
            cpm_track: The CP/M track number.

        Returns:
            A tuple of (cylinder, head).

        Raises:
            ValueError: If physical format is not available.
        """
        if not self.disk.physical_format:
            raise ValueError("Physical format not available.")
        heads = self.disk.physical_format.heads
        cylinder = cpm_track // heads
        head = cpm_track % heads
        return cylinder, head

    def _format_entry_to_bytes(self, entry: CPMDirectoryEntry) -> bytes:
        """
        Serializes a CPMDirectoryEntry object into a 32-byte array.

        Args:
            entry: The directory entry to serialize.

        Returns:
            A 32-byte representation of the entry.

        Raises:
            ValueError: If the DPB is not available.
        """
        if not self.dpb:
            raise ValueError("DPB not available.")

        entry_bytes = bytearray(32)
        entry_bytes[0] = entry.user

        name_padded = entry.name.upper().ljust(8, " ")
        ext_padded = entry.ext.upper().ljust(3, " ")

        entry_bytes[1:9] = name_padded.encode("ascii")
        entry_bytes[9:12] = ext_padded.encode("ascii")

        if "t1" in entry.attributes_raw:
            entry_bytes[9] |= entry.attributes_raw.get("t1", 0) & 0x80
        if "t2" in entry.attributes_raw:
            entry_bytes[10] |= entry.attributes_raw.get("t2", 0) & 0x80
        if "t3" in entry.attributes_raw:
            entry_bytes[11] |= entry.attributes_raw.get("t3", 0) & 0x80

        entry_bytes[12] = entry.ex
        entry_bytes[13] = entry.s1
        entry_bytes[14] = entry.xh
        entry_bytes[15] = entry.rc

        if self.dpb.dsm > 255:
            for i, block_num in enumerate(entry.blks):
                if i < 8:
                    struct.pack_into("<H", entry_bytes, 16 + i * 2, block_num)
        else:
            for i, block_num in enumerate(entry.blks):
                if i < 16:
                    entry_bytes[16 + i] = block_num

        return bytes(entry_bytes)

    def _get_logical_spt(self, cpm_track_num: int) -> int:
        """
        Calculates the number of logical 128-byte sectors for a CP/M track.

        Args:
            cpm_track_num: The CP/M track number.

        Returns:
            The number of logical 128-byte sectors on this track.

        Raises:
            ValueError: If disk, physical_format, or DPB is not available.
        """
        if not self.disk or not self.disk.physical_format or not self.dpb:
            raise ValueError(
                "Disk, physical_format, or DPB not available for SPT calculation."
            )

        phys_cyl, phys_head = self._cpm_track_to_chs_coords(cpm_track_num)

        if phys_cyl >= self.disk.physical_format.cylinders:
            self.logger.warning(
                f"Track number {cpm_track_num} (phys cyl {phys_cyl}) exceeds max physical "
                f"cylinder {self.disk.physical_format.cylinders - 1}. Falling back to DPB.spt"
            )
            return self.dpb.spt

        phys_spt = self.disk.physical_format.get_sectors_per_track(phys_cyl, phys_head)
        phys_bps = self.disk.physical_format.get_bytes_per_sector(phys_cyl, phys_head)

        if phys_bps < CPM_SECTOR_SIZE:
            self.logger.error(
                f"Track {cpm_track_num}: physical BPS ({phys_bps}) < "
                f"logical BPS ({CPM_SECTOR_SIZE}). Not supported."
            )
            return 0

        return phys_spt * (phys_bps // CPM_SECTOR_SIZE)

    def _group_extents(
        self, raw_entries: list[CPMDirectoryEntry]
    ) -> dict[tuple[int, str], list[CPMDirectoryEntry]]:
        """
        Groups raw directory entries by file, creating a per-file extent list.

        Args:
            raw_entries: The list of raw directory entries.

        Returns:
            A dictionary mapping (user, filename) to list of extents.
        """
        files = defaultdict(list)
        for entry in raw_entries:
            if entry.is_deleted():
                continue
            key = (entry.user, entry.get_filename().upper())
            files[key].append(entry)
        for key in files:
            files[key].sort(key=lambda e: (e.ex | (e.xh << 8)))
        return files

    def _initialize_parameters(self) -> None:
        """
        Initializes filesystem parameters and prerequisite data.

        Raises:
            ValueError: If the DPB is not set.
        """
        if not self.dpb:
            raise ValueError("DPB not set for initialization.")

    def _load_allocation_map(self) -> None:
        """
        Builds a set of all used block numbers by scanning the directory.
        """
        if self.get_validity_score() < self.validity_threshold or not self.dpb:
            return

        self.logger.debug(
            "Building CP/M allocation map by scanning directory entries..."
        )
        entries = self._read_directory_entries()
        used_blocks = set()

        for block in range(self.dpb.directory_blocks):
            used_blocks.add(block)

        self.logger.debug(
            f"Directory uses {self.dpb.directory_blocks} blocks: "
            f"{sorted([b for b in used_blocks if b < self.dpb.directory_blocks])}"
        )

        for entry in entries:
            if entry.is_deleted():
                continue
            for block_num in entry.blks:
                if 0 < block_num <= self.dpb.dsm:
                    used_blocks.add(block_num)

        self._cached_allocation_map = used_blocks
        self.logger.debug(f"Built allocation map with {len(used_blocks)} used blocks.")

    def _map_dir_entry_index_to_location(self, index: int) -> tuple[int, int, int]:
        """
        Maps a flat directory entry index to its on-disk location.

        Args:
            index: The directory entry index.

        Returns:
            A tuple of (cpm_track, logical_sector_on_track, offset_in_sector).

        Raises:
            ValueError: If the DPB is not set.
            IOError: If the SPT for a track is zero.
        """
        if not self.dpb:
            raise ValueError("DPB not set.")

        logical_dir_sector_idx = index // CPM_DIRECTORY_ENTRIES_PER_SECTOR
        offset_in_sector = (index % CPM_DIRECTORY_ENTRIES_PER_SECTOR) * 32
        current_cpm_track = self.dpb.off
        logical_sectors_left = logical_dir_sector_idx

        while True:
            spt = self._get_logical_spt(current_cpm_track)
            if spt == 0:
                raise OSError(
                    f"Cannot map directory entry: SPT for CP/M track {current_cpm_track} is zero."
                )
            if logical_sectors_left < spt:
                return current_cpm_track, logical_sectors_left, offset_in_sector
            logical_sectors_left -= spt
            current_cpm_track += 1

    def _map_logical_to_physical_sector(
        self, cpm_track: int, logical_sector_on_track: int
    ) -> tuple[int, int, int, int]:
        """
        Maps a logical sector address to its physical disk location.

        Args:
            cpm_track: The CP/M track number.
            logical_sector_on_track: The logical sector number on the track.

        Returns:
            A tuple of (phys_cyl, phys_head, phys_sector_id, offset_in_phys).

        Raises:
            ValueError: If the physical index exceeds the translation table length.
        """
        phys_cyl, phys_head = self._cpm_track_to_chs_coords(cpm_track)
        tf = self.disk.physical_format.get_track_format(phys_cyl, phys_head)

        phys_bps = tf.bytes_per_sector
        log_per_phys = phys_bps // CPM_SECTOR_SIZE
        phys_index = logical_sector_on_track // log_per_phys
        offset_in_phys = (logical_sector_on_track % log_per_phys) * CPM_SECTOR_SIZE

        order = tf.sector_translation_table
        if phys_index >= len(order):
            raise ValueError(
                f"Physical index {phys_index} exceeds order length {len(order)}."
            )
        phys_sector_id = order[phys_index]

        return phys_cyl, phys_head, phys_sector_id, offset_in_phys

    def _parse_cpm_path(self, path: str) -> tuple[int, str]:
        """
        Parses a CP/M path into a user number and an 8.3 filename.

        Args:
            path: The CP/M path to parse (e.g., "U0:FILENAME.EXT").

        Returns:
            A tuple of (user_number, filename).

        Raises:
            ValueError: If the filename is empty.
        """
        path_to_parse = path.upper().lstrip("/")
        user = 0
        filename_part = path_to_parse

        if path_to_parse.startswith("U") and ":" in path_to_parse:
            parts = path_to_parse.split(":", 1)
            user_str = parts[0][1:]
            if user_str.isdigit():
                try:
                    user = int(user_str)
                    filename_part = parts[1]
                except ValueError:
                    pass

        name_parts = filename_part.split(".", 1)
        base = name_parts[0][:8]
        ext = name_parts[1][:3] if len(name_parts) > 1 else ""
        parsed_filename = f"{base}.{ext}" if ext else base

        if not parsed_filename.strip():
            raise ValueError(f"Empty filename derived from path '{path}'")
        if len(parsed_filename) > 12:
            self.logger.warning(
                f"Parsed filename '{parsed_filename}' from '{path}' is longer than typical 8.3."
            )

        return user, parsed_filename.strip()

    def _parse_directory_entry(self, entry_bytes: bytes) -> Optional[CPMDirectoryEntry]:
        """
        Parses a 32-byte chunk into a CPMDirectoryEntry object.

        Args:
            entry_bytes: The 32-byte directory entry data.

        Returns:
            A CPMDirectoryEntry object, or None if parsing fails.

        Raises:
            ValueError: If the DPB is not available.
        """
        if len(entry_bytes) < 32:
            return None
        if not self.dpb:
            raise ValueError("DPB not available for parsing directory entry.")

        user = entry_bytes[0]
        name_bytes = bytes(b & 0x7F for b in entry_bytes[1:9])
        ext_bytes = bytes(b & 0x7F for b in entry_bytes[9:12])
        raw_attrs = {"t1": entry_bytes[9], "t2": entry_bytes[10], "t3": entry_bytes[11]}
        name = name_bytes.decode("ascii", errors="replace").strip()
        ext = ext_bytes.decode("ascii", errors="replace").strip()
        ex, s1, xh_s2, rc = (
            entry_bytes[12],
            entry_bytes[13],
            entry_bytes[14],
            entry_bytes[15],
        )

        block_pointers = []
        if self.dpb.dsm > 255:
            for j in range(8):
                ptr_bytes = entry_bytes[16 + j * 2 : 16 + j * 2 + 2]
                if len(ptr_bytes) == 2:
                    block_pointers.append(struct.unpack("<H", ptr_bytes)[0])
        else:
            block_pointers.extend(entry_bytes[16:32])

        return CPMDirectoryEntry(
            user, name, ext, ex, s1, xh_s2, rc, block_pointers, raw_attrs
        )

    def _read_block(self, block_num: int) -> bytes:
        """
        Reads one full data allocation block from the disk.

        Args:
            block_num: The block number to read.

        Returns:
            The block data as bytes.

        Raises:
            ValueError: If the DPB is not set.
        """
        if block_num == 0:
            return b""
        if not self.dpb:
            raise ValueError("DPB not set.")

        logical_sectors_per_block = self.dpb.block_size // CPM_SECTOR_SIZE
        data = bytearray()
        current_cpm_track, logical_sector_on_track = self._block_to_track_sector(
            block_num
        )

        for _ in range(logical_sectors_per_block):
            try:
                data.extend(
                    self._read_logical_sector(
                        current_cpm_track, logical_sector_on_track
                    )
                )
            except ValueError:
                self.logger.warning(
                    f"Read for block {block_num} went past valid disk sectors. "
                    "Padding with nulls."
                )
                data.extend(bytes(CPM_SECTOR_SIZE))

            logical_sector_on_track += 1
            spt = self._get_logical_spt(current_cpm_track)
            if spt > 0 and logical_sector_on_track >= spt:
                logical_sector_on_track = 0
                current_cpm_track += 1
        return bytes(data)

    def _read_directory_entries(self) -> list[CPMDirectoryEntry]:
        """
        Reads all CP/M directory entries from the disk.

        Returns:
            A list of CPMDirectoryEntry objects.

        Raises:
            ValueError: If the DPB, disk, or physical format is not available.
        """
        if not self.dpb or not self.disk or not self.disk.physical_format:
            raise ValueError("DPB, disk, or physical format not available.")

        entries = []
        dir_logical_sectors = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        current_cpm_track = self.dpb.off
        logical_sector_idx = 0

        for _ in range(dir_logical_sectors):
            if len(entries) > self.dpb.drm:
                break
            try:
                sector_data = self._read_logical_sector(
                    current_cpm_track, logical_sector_idx
                )
                for i in range(CPM_DIRECTORY_ENTRIES_PER_SECTOR):
                    if len(entries) > self.dpb.drm:
                        break
                    offset = i * 32
                    entry_data = sector_data[offset : offset + 32]
                    if len(entry_data) != 32:
                        continue
                    entry = self._parse_directory_entry(entry_data)
                    if entry:
                        entries.append(entry)
            except Exception as e:
                self.logger.error(
                    f"Failed to read directory sector at CP/M track {current_cpm_track}, "
                    f"logical sector {logical_sector_idx}: {e}"
                )
                break

            logical_spt = self._get_logical_spt(current_cpm_track)
            if logical_spt > 0:
                logical_sector_idx += 1
                if logical_sector_idx >= logical_spt:
                    logical_sector_idx = 0
                    current_cpm_track += 1
            else:
                break
        return entries

    def _read_logical_sector(
        self, cpm_track: int, logical_sector_on_track: int
    ) -> bytes:
        """
        Reads a single 128-byte logical sector from the disk.

        Args:
            cpm_track: The CP/M track number.
            logical_sector_on_track: The logical sector number on the track.

        Returns:
            The 128-byte sector data.

        Raises:
            ValueError: If disk or physical format is not available, or if the
                       logical sector exceeds the SPT for the track.
        """
        if not self.disk or not self.disk.physical_format:
            raise ValueError("Disk or physical format not available.")
        if logical_sector_on_track >= self._get_logical_spt(cpm_track):
            raise ValueError(
                f"Logical sector {logical_sector_on_track} exceeds SPT for CP/M track {cpm_track}."
            )

        phys_cyl, phys_head, phys_sector_id, offset_in_phys = (
            self._map_logical_to_physical_sector(cpm_track, logical_sector_on_track)
        )

        phys_sector_data = self.disk.read_sector(phys_cyl, phys_head, phys_sector_id)
        return phys_sector_data[offset_in_phys : offset_in_phys + CPM_SECTOR_SIZE]

    def _try_derive_dpb(self) -> bool:
        """
        Tries to derive a DPB for common 8-inch disk formats.

        Returns:
            True if a DPB was successfully derived, False otherwise.
        """
        pf = self.disk.physical_format
        if pf.cylinders != 77 or pf.heads != 1 or pf.rpm != 360:
            return False
        if len(pf.track_formats) == 1:
            tf = pf.track_formats[0]
            if (
                tf.encoding == "FM"
                and tf.rate in (250, 300, 500)
                and tf.sectors_per_track == 26
                and tf.bytes_per_sector == 128
            ):
                self.dpb = CPMDiskParameterBlock(
                    spt=26,
                    bsh=3,
                    blm=7,
                    exm=0,
                    dsm=242,
                    drm=63,
                    al0=0xC0,
                    al1=0x00,
                    cks=0,
                    off=2,
                )
                return True
        elif len(pf.track_formats) == 2:
            tf0, tf1 = pf.track_formats[0], pf.track_formats[1]
            if (
                tf0.track_start == 0
                and tf0.track_end == 0
                and tf0.encoding == "FM"
                and tf0.sectors_per_track == 26
                and tf0.bytes_per_sector == 128
                and tf1.track_start == 1
                and tf1.track_end == 76
                and tf1.encoding == "MFM"
                and tf1.sectors_per_track == 26
                and tf1.bytes_per_sector == 256
            ):
                self.dpb = CPMDiskParameterBlock(
                    spt=52,
                    bsh=4,
                    blm=15,
                    exm=1,
                    dsm=242,
                    drm=63,
                    al0=0xC0,
                    al1=0x00,
                    cks=0,
                    off=2,
                )
                return True
        return False

    def _write_block(self, block_num: int, data: bytes) -> None:
        """
        Writes data to a single data allocation block.

        Args:
            block_num: The block number to write to.
            data: The data to write.

        Raises:
            ValueError: If the DPB is not available or the block number is invalid.
        """
        if not self.dpb:
            raise ValueError("DPB not available for write_block.")
        if block_num <= 0 or block_num > self.dpb.dsm:
            raise ValueError(f"Invalid block number {block_num} for writing.")

        expected_size = self.dpb.block_size
        if len(data) > expected_size:
            data = data[:expected_size]
        elif len(data) < expected_size:
            data = data.ljust(expected_size, b"\x00")

        logical_sectors_per_block = expected_size // CPM_SECTOR_SIZE
        start_cpm_track, start_logical_sector_on_track = self._block_to_track_sector(
            block_num
        )
        current_cpm_track = start_cpm_track
        current_logical_sector = start_logical_sector_on_track

        for i in range(logical_sectors_per_block):
            sector_data = data[i * CPM_SECTOR_SIZE : (i + 1) * CPM_SECTOR_SIZE]
            self._write_logical_sector(
                current_cpm_track, current_logical_sector, sector_data
            )
            current_logical_sector += 1
            spt = self._get_logical_spt(current_cpm_track)
            if spt > 0 and current_logical_sector >= spt:
                current_logical_sector = 0
                current_cpm_track += 1

    def _write_logical_sector(
        self, cpm_track: int, logical_sector_on_track: int, data: bytes
    ) -> None:
        """
        Writes a single 128-byte logical sector to the disk.

        Args:
            cpm_track: The CP/M track number.
            logical_sector_on_track: The logical sector number on the track.
            data: The 128-byte data to write.

        Raises:
            ValueError: If the DPB, disk, or physical format is not available,
                       or if the data size is incorrect, or if the logical sector
                       exceeds the SPT for the track.
            IOError: If reading the physical sector returns 0 bytes.
        """
        if not self.dpb or not self.disk or not self.disk.physical_format:
            raise ValueError("DPB, disk, or physical format not available.")
        if len(data) != CPM_SECTOR_SIZE:
            raise ValueError(
                f"Data size {len(data)} != CPM_SECTOR_SIZE {CPM_SECTOR_SIZE}."
            )
        if logical_sector_on_track >= self._get_logical_spt(cpm_track):
            raise ValueError(
                f"Logical sector {logical_sector_on_track} exceeds SPT for CP/M track {cpm_track}."
            )

        phys_cyl, phys_head, phys_sector_id, offset_in_phys = (
            self._map_logical_to_physical_sector(cpm_track, logical_sector_on_track)
        )

        phys_sector_data = self.disk.read_sector(phys_cyl, phys_head, phys_sector_id)
        if len(phys_sector_data) == 0:
            raise OSError(
                f"Read 0 bytes from physical sector C:{phys_cyl} H:{phys_head} S:{phys_sector_id}"
            )

        phys_sector_data = bytearray(phys_sector_data)
        phys_sector_data[offset_in_phys : offset_in_phys + CPM_SECTOR_SIZE] = data
        self.disk.write_sector(
            phys_cyl, phys_head, phys_sector_id, bytes(phys_sector_data)
        )
