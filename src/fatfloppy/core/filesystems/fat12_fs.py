import contextlib
import copy
import datetime
import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger
from .fs_base import FileInfo, Filesystem


@dataclass
class FATVolumeInfo:
    """
    Represents the FAT Volume Information from the Boot Sector.

    This dataclass holds the BIOS Parameter Block (BPB) values that define
    the geometry and layout of the FAT filesystem on the disk.
    """

    logger = get_logger(__name__)
    oem_id: str = "MSDOS5.0"
    bytes_per_sector: int = 512
    sectors_per_cluster: int = 1
    reserved_sectors: int = 1
    num_fats: int = 2
    root_entries: int = 224
    total_sectors: int = 2880
    media_descriptor: int = 0xF0
    sectors_per_fat: int = 9
    sectors_per_track: int = 18
    num_heads: int = 2
    hidden_sectors: int = 0
    drive_number: int = 0
    volume_serial: int = 0
    volume_label: str = "NO NAME    "
    fs_type: str = "FAT12   "

    @classmethod
    def from_bytes(cls, data: bytes) -> "FATVolumeInfo":
        """
        Populates a FATVolumeInfo instance by parsing a boot sector.

        Args:
            data: A byte array (at least 128 bytes) from the boot sector.

        Returns:
            A new FATVolumeInfo instance.

        Raises:
            ValueError: If the provided data is too short or cannot be parsed.
        """
        if len(data) < 128:
            raise ValueError("Sector data too short")
        instance = cls()
        try:
            instance.oem_id = data[3:11].decode("cp437", errors="replace").strip()
            instance.bytes_per_sector = struct.unpack_from("<H", data, 0x00B)[0]
            instance.sectors_per_cluster = data[0x00D]
            instance.reserved_sectors = struct.unpack_from("<H", data, 0x00E)[0]
            instance.num_fats = data[0x010]
            instance.root_entries = struct.unpack_from("<H", data, 0x011)[0]
            total_sectors_16 = struct.unpack_from("<H", data, 0x013)[0]
            instance.media_descriptor = data[0x015]
            instance.sectors_per_fat = struct.unpack_from("<H", data, 0x016)[0]
            instance.sectors_per_track = struct.unpack_from("<H", data, 0x018)[0]
            instance.num_heads = struct.unpack_from("<H", data, 0x01A)[0]
            instance.hidden_sectors = struct.unpack_from("<I", data, 0x01C)[0]
            total_sectors_32 = struct.unpack_from("<I", data, 0x020)[0]
            instance.total_sectors = (
                total_sectors_32 if total_sectors_16 == 0 else total_sectors_16
            )
            instance.drive_number = data[0x024]
            if data[0x26] == 0x29:
                instance.volume_serial = struct.unpack_from("<I", data, 0x027)[0]
                instance.volume_label = data[0x02B:0x036].decode("cp437").strip()
                instance.fs_type = data[0x036:0x03E].decode("cp437").strip()
            else:
                instance.volume_label = ""
                instance.fs_type = ""
        except (struct.error, IndexError, UnicodeDecodeError) as e:
            cls.logger.error(f"BPB parsing error: {e}")
            raise ValueError("Failed to parse BPB.") from e
        return instance

    def is_valid(self) -> bool:
        """
        Performs basic validation checks on the BPB parameters.

        Returns:
            True if the parameters appear to be valid, False otherwise.
        """
        if self.bytes_per_sector not in [128, 256, 512, 1024, 2048, 4096]:
            return False
        if self.sectors_per_cluster not in [1, 2, 4, 8, 16, 32, 64, 128]:
            return False
        if (self.bytes_per_sector * self.sectors_per_cluster) > 65536:
            return False
        if self.reserved_sectors == 0:
            return False
        if self.num_fats not in [1, 2]:
            return False
        if self.sectors_per_fat == 0:
            return False
        if self.total_sectors == 0:
            return False
        if self.bytes_per_sector == 0:
            return False
        root_dir_bytes = self.root_entries * 32
        if root_dir_bytes % self.bytes_per_sector != 0:
            return False
        root_dir_sectors = root_dir_bytes // self.bytes_per_sector
        first_data_sector = (
            self.reserved_sectors
            + (self.num_fats * self.sectors_per_fat)
            + root_dir_sectors
        )
        return not first_data_sector >= self.total_sectors

    def to_bytes(self) -> bytes:
        """
        Serializes the FATVolumeInfo instance into a boot sector byte array.

        Returns:
            A byte array representing the boot sector.
        """
        boot_sector = bytearray(self.bytes_per_sector)
        boot_sector[0:3] = b"\xeb\xfe\x90"
        boot_sector[3:11] = self.oem_id.encode("cp437").ljust(8)
        struct.pack_into("<H", boot_sector, 0x00B, self.bytes_per_sector)
        struct.pack_into("<B", boot_sector, 0x00D, self.sectors_per_cluster)
        struct.pack_into("<H", boot_sector, 0x00E, self.reserved_sectors)
        struct.pack_into("<B", boot_sector, 0x010, self.num_fats)
        struct.pack_into("<H", boot_sector, 0x011, self.root_entries)
        if self.total_sectors < 65536:
            struct.pack_into("<H", boot_sector, 0x013, self.total_sectors)
            struct.pack_into("<I", boot_sector, 0x020, 0)
        else:
            struct.pack_into("<H", boot_sector, 0x013, 0)
            struct.pack_into("<I", boot_sector, 0x020, self.total_sectors)
        struct.pack_into("<B", boot_sector, 0x015, self.media_descriptor)
        struct.pack_into("<H", boot_sector, 0x016, self.sectors_per_fat)
        struct.pack_into("<H", boot_sector, 0x018, self.sectors_per_track)
        struct.pack_into("<H", boot_sector, 0x01A, self.num_heads)
        struct.pack_into("<I", boot_sector, 0x01C, self.hidden_sectors)
        # Extended BPB (0x29 signature, volume serial/label, FS type) and the
        # 0xAA55 boot signature were introduced with DOS 3.3+/4.0 for 512-byte
        # sectors.  Early FAT formats (8" floppies, 128/256-byte sectors)
        # predate these fields, so omit them for historical accuracy.
        # The read side (from_bytes) already handles this via the 0x29 check.
        if self.bytes_per_sector >= 512:
            struct.pack_into("<B", boot_sector, 0x024, self.drive_number)
            struct.pack_into("<B", boot_sector, 0x025, 0)
            struct.pack_into("<B", boot_sector, 0x026, 0x29)
            struct.pack_into("<I", boot_sector, 0x027, self.volume_serial)
            boot_sector[0x02B:0x036] = self.volume_label.encode("cp437").ljust(11)
            boot_sector[0x036:0x03E] = self.fs_type.encode("cp437").ljust(8)
            # The boot signature lives at the fixed offset 510 regardless of
            # sector size; also mirror it at sector-end for tools that look
            # there on non-512-byte sectors.
            struct.pack_into("<H", boot_sector, 510, 0xAA55)
            if self.bytes_per_sector != 512:
                struct.pack_into("<H", boot_sector, self.bytes_per_sector - 2, 0xAA55)
        return bytes(boot_sector)


FAT12_MAX_CLUSTERS = 4084
FAT12_EOC = 0xFFF
FAT12_EOC_MIN = 0xFF8
FAT12_BAD_CLUSTER = 0xFF7

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = ATTR_READ_ONLY | ATTR_HIDDEN | ATTR_SYSTEM | ATTR_VOLUME_ID

ENTRY_DELETED = 0xE5
ENTRY_UNUSED = 0x00

# Device names reserved by DOS; never valid as 8.3 file base names.
FAT_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
    }
)


class FATFilesystem(Filesystem):
    """
    Provides an interface to a FAT12 filesystem on a disk image.

    This class handles filesystem detection, metadata parsing (BPB), FAT
    manipulation, and file operations like reading, writing, deleting,
    and listing files and directories.
    """

    filesystem_type: ClassVar[str] = "FAT12"
    filesystem_aliases: ClassVar[list[str]] = ["FAT", "MSDOS"]
    validity_threshold: ClassVar[int] = 40
    VALIDITY_THRESHOLD = validity_threshold

    config_class: ClassVar[type] = FATVolumeInfo

    _invalid_83_chars_pattern = re.compile(r'[\\/:*?"<>|\s]')
    _reserved_names = FAT_RESERVED_NAMES

    def __init__(self, disk: Disk, config: Optional[FATVolumeInfo] = None):
        """
        Initializes the FATFilesystem instance.

        Initialization is deferred until the first operation or validity check
        to avoid raising errors on disks with other formats.

        Args:
            disk: The Disk object to operate on.
            config: Optional FAT Boot Sector / BPB for this filesystem.
        """
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self._init_completed = False
        self.boot_sector: Optional[FATVolumeInfo] = config
        self._cached_allocated_clusters: Optional[list[int]] = None
        self.fat_cache: Optional[bytearray] = None
        self.fat_dirty = False
        self._cached_validity_score: Optional[int] = None

        self._try_initialize()

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        """
        Returns all FAT12 format definitions provided by this plugin.

        Returns:
            Dictionary mapping format names to FormatProfile objects.
        """
        from .formats.fat12_formats import FAT12_FORMATS

        return FAT12_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        """
        Checks if two filesystem configurations match.

        Args:
            config1: First configuration to compare.
            config2: Second configuration to compare.

        Returns:
            True if configurations match, False otherwise.
        """
        if not isinstance(config1, FATVolumeInfo) or not isinstance(
            config2, FATVolumeInfo
        ):
            return False
        return (
            config1.total_sectors == config2.total_sectors
            and config1.bytes_per_sector == config2.bytes_per_sector
            and config1.sectors_per_cluster == config2.sectors_per_cluster
            and config1.sectors_per_fat == config2.sectors_per_fat
            and config1.root_entries == config2.root_entries
            and config1.num_heads == config2.num_heads
            and config1.media_descriptor == config2.media_descriptor
        )

    @staticmethod
    def create_config_from_params(
        format_info: dict[str, Any], physical_format: PhysicalFormat
    ) -> Optional[FATVolumeInfo]:
        """
        Creates a FATVolumeInfo config from parameters.

        Args:
            format_info: Dictionary containing FAT12 parameters.
            physical_format: The physical format of the disk.

        Returns:
            A configured FATVolumeInfo object, or None on error.
        """
        logger = get_logger("FATFilesystem")

        try:
            total_sectors = physical_format.total_sectors
            bytes_per_sector = physical_format.bytes_per_sector
            sectors_per_cluster = format_info.get("sectors_per_cluster", 1)
            if sectors_per_cluster == 0:
                sectors_per_cluster = 1
            reserved_sectors = format_info.get("reserved_sectors", 1)
            num_fats = format_info.get("num_fats", 2)
            root_entries = format_info.get(
                "root_entries", 224 if total_sectors > 1440 else 112
            )

            if bytes_per_sector == 0:
                logger.error("Bytes per sector cannot be zero")
                return None

            root_dir_sectors = (
                root_entries * 32 + bytes_per_sector - 1
            ) // bytes_per_sector
            available_for_fats_and_data = total_sectors - (
                reserved_sectors + root_dir_sectors
            )

            if available_for_fats_and_data < 0:
                logger.error("Not enough space for reserved and root directory sectors")
                return None

            sectors_per_fat = 1
            for _attempt in range(
                available_for_fats_and_data // (num_fats if num_fats > 0 else 1) + 1
            ):
                if num_fats == 0:
                    data_sectors = available_for_fats_and_data
                else:
                    data_sectors = total_sectors - (
                        reserved_sectors
                        + (num_fats * sectors_per_fat)
                        + root_dir_sectors
                    )

                if data_sectors < sectors_per_cluster:
                    break

                num_clusters_current_try = data_sectors // sectors_per_cluster
                if num_clusters_current_try <= 0:
                    break

                fat_bytes_needed = ((num_clusters_current_try + 2) * 3 + 1) // 2
                spf_needed = (
                    fat_bytes_needed + bytes_per_sector - 1
                ) // bytes_per_sector

                if spf_needed <= sectors_per_fat:
                    break
                sectors_per_fat = spf_needed
            else:
                logger.error("Could not determine consistent sectors_per_fat")
                return None

            if num_fats > 0:
                data_s = total_sectors - (
                    reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors
                )
            else:
                data_s = available_for_fats_and_data

            if data_s < sectors_per_cluster:
                logger.error(
                    f"Data sectors ({data_s}) < sectors_per_cluster "
                    f"({sectors_per_cluster})"
                )
                return None

            final_num_clusters = data_s // sectors_per_cluster
            if final_num_clusters <= 0:
                logger.error(f"Non-positive cluster count: {final_num_clusters}")
                return None

            if final_num_clusters > FAT12_MAX_CLUSTERS:
                logger.warning(
                    f"Cluster count ({final_num_clusters}) exceeds FAT12 limit"
                )

            return FATVolumeInfo(
                bytes_per_sector=bytes_per_sector,
                sectors_per_cluster=sectors_per_cluster,
                reserved_sectors=reserved_sectors,
                num_fats=num_fats,
                root_entries=root_entries,
                total_sectors=total_sectors,
                media_descriptor=format_info.get("media_descriptor", 0xF0),
                sectors_per_fat=sectors_per_fat,
                sectors_per_track=physical_format.track_formats[0].sectors_per_track,
                num_heads=physical_format.heads,
                hidden_sectors=format_info.get("hidden_sectors", 0),
                drive_number=format_info.get("drive_number", 0),
                volume_serial=format_info.get("volume_serial", 0),
            )
        except Exception as e:
            logger.error(f"Error creating FAT12 config: {e}", exc_info=True)
            return None

    def create_directory(self, path: str) -> None:
        """
        Creates a new directory.

        Args:
            path: The full path of the directory to create.

        Raises:
            IOError: If the filesystem is not valid, or if there is not enough
                space on the disk or in the parent directory.
            ValueError: If the directory name is not a valid 8.3 filename.
            FileExistsError: If a file with the same name already exists.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Creating directory: {path}")
        parent_path, dir_name = self._split_path(path)

        if not self._is_valid_83_filename(dir_name, allow_extension=False):
            raise ValueError(f"Invalid 8.3 directory name: '{dir_name}'")

        parent_dir_cluster = self._get_directory_cluster(parent_path)

        try:
            existing_entry, _ = self._find_entry_in_directory(
                parent_dir_cluster, dir_name
            )
            if existing_entry:
                if existing_entry.is_dir:
                    self.logger.warning(f"Directory '{path}' already exists.")
                    return
                raise FileExistsError(
                    f"A file with the name '{dir_name}' already exists in "
                    f"'{parent_path}'"
                )
        except FileNotFoundError:
            pass

        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            raise OSError("No free clusters available to create directory")
        self._set_fat_entry_cached(new_cluster, FAT12_EOC)
        cluster_offset = self._cluster_to_offset(new_cluster)

        try:
            self._write_bytes(cluster_offset, b"\x00" * self.allocation_unit_size)
            now = datetime.datetime.now()
            dot_entry = self._create_directory_entry_bytes(
                name=".", is_dir=True, starting_cluster=new_cluster, size=0, dt=now
            )
            dotdot_entry = self._create_directory_entry_bytes(
                name="..",
                is_dir=True,
                starting_cluster=parent_dir_cluster if parent_dir_cluster > 0 else 0,
                size=0,
                dt=now,
            )
            self._write_bytes(cluster_offset, dot_entry)
            self._write_bytes(cluster_offset + 32, dotdot_entry)
        except Exception as write_err:
            self.logger.error(
                f"Error writing '.'/'..' entries, freeing allocated cluster: "
                f"{write_err}"
            )
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise OSError("Failed to initialize new directory cluster") from write_err

        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            self.logger.error(
                f"No space in parent directory '{parent_path}' to create entry "
                f"for '{dir_name}'"
            )
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise OSError(f"No space in parent directory {parent_path}")

        dir_entry_bytes = self._create_directory_entry_bytes(
            name=dir_name, is_dir=True, starting_cluster=new_cluster, size=0, dt=now
        )
        try:
            dir_cluster_num, entry_offset_in_cluster = entry_location
            entry_disk_offset = self._get_offset_for_directory_entry(
                dir_cluster_num, entry_offset_in_cluster
            )
            self._write_bytes(entry_disk_offset, dir_entry_bytes)
        except Exception as parent_write_err:
            self.logger.error(
                f"Error writing parent directory entry, freeing allocated "
                f"cluster: {parent_write_err}"
            )
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise OSError(
                "Failed to write directory entry in parent"
            ) from parent_write_err

        self._commit_fat()
        self.disk.flush()
        self.logger.info(f"Successfully created directory '{path}'")

    def delete(self, path: str) -> None:
        """
        Deletes a file or an empty directory.

        Args:
            path: The full path of the item to delete.

        Raises:
            IOError: If the filesystem is not valid or a disk error occurs.
            ValueError: If attempting to delete the root directory.
            OSError: If attempting to delete a non-empty directory.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        if path == "/":
            raise ValueError("Cannot delete root directory")

        self.logger.debug(f"Deleting: {path}")
        parent_path, name = self._split_path(path)
        parent_dir_cluster = self._get_directory_cluster(parent_path)
        try:
            entry_to_delete, entry_location = self._find_entry_in_directory(
                parent_dir_cluster, name
            )
        except FileNotFoundError:
            self.logger.warning(f"Item '{path}' not found for deletion.")
            return

        if entry_to_delete.is_dir:
            try:
                dir_contents = self._list_directory_by_cluster(
                    entry_to_delete.starting_cluster
                )
                non_dot_entries = [e for e in dir_contents if e.name not in [".", ".."]]
                if non_dot_entries:
                    raise OSError(f"Directory not empty: {path}")
            except Exception as list_err:
                self.logger.error(
                    f"Could not verify if directory '{path}' is empty due to "
                    f"error: {list_err}"
                )
                raise OSError(
                    f"Could not verify directory contents before deleting: {path}"
                ) from list_err

        try:
            dir_cluster_num, entry_offset_in_cluster = entry_location
            entry_disk_offset = self._get_offset_for_directory_entry(
                dir_cluster_num, entry_offset_in_cluster
            )
            self._write_bytes(entry_disk_offset, bytes([ENTRY_DELETED]))
        except Exception as write_err:
            self.logger.error(f"Failed to mark entry deleted for '{path}': {write_err}")
            raise OSError(
                f"Failed to update directory entry for deletion: {path}"
            ) from write_err

        if entry_to_delete.starting_cluster >= 2:
            self.logger.debug(
                f"Freeing cluster chain starting at {entry_to_delete.starting_cluster}"
            )
            self._free_cluster_chain(entry_to_delete.starting_cluster)

        self._commit_fat()
        self.disk.flush()
        self._cached_allocated_clusters = None
        self.logger.info(f"Successfully deleted '{path}'")

    def delete_recursive(self, path: str, _visited: Optional[set[int]] = None) -> bool:
        """
        Recursively deletes a directory and all its contents.

        Args:
            path: The full path of the directory to delete.
            _visited: Internal set of directory start clusters already entered,
                used to break cyclic/self-referential directory structures from
                a crafted image (audit fat12_fs.py:579).

        Returns:
            True if successful, False otherwise.
        """
        if _visited is None:
            _visited = set()
        try:
            entry_info = self._find_path(path)
            if entry_info.is_dir:
                start = getattr(entry_info, "starting_cluster", 0)
                if start and start in _visited:
                    self.logger.error(
                        f"Cyclic directory detected at {path} "
                        f"(cluster {start}); aborting recursion."
                    )
                    return False
                if start:
                    _visited.add(start)
                contents = self.list_directory(path)
                for item in contents:
                    if item.name not in [".", ".."]:
                        item_path = (
                            f"{path}/{item.name}" if path != "/" else f"/{item.name}"
                        )
                        if not self.delete_recursive(item_path, _visited):
                            return False
                self.delete(path)
            else:
                self.delete(path)
            return True
        except Exception as e:
            self.logger.error(f"Error deleting {path}: {e}")
            return False

    def format_fs(
        self, profile: FormatProfile, volume_label: Optional[str] = None
    ) -> None:
        """
        Formats the disk with a FAT12 filesystem.

        This involves writing a new boot sector, initializing the FATs, and
        clearing the root directory area.

        Args:
            profile: The FormatProfile containing the target FATVolumeInfo.
            volume_label: An optional volume label to apply.

        Raises:
            TypeError: If the profile is not a FormatProfile object.
            ValueError: If the profile does not contain a valid FATVolumeInfo.
        """
        if not isinstance(profile, FormatProfile):
            raise TypeError("profile must be a FormatProfile object")
        if not profile.filesystem_config or not isinstance(
            profile.filesystem_config, FATVolumeInfo
        ):
            raise ValueError(
                "Invalid or missing FATVolumeInfo in FormatProfile for FAT formatting"
            )
        self.logger.info(f"Formatting disk with FAT12 profile: {profile.name}")

        boot_sector_config = copy.deepcopy(profile.filesystem_config)
        if volume_label:
            boot_sector_config.volume_label = volume_label.ljust(11)[:11]
        elif (
            not boot_sector_config.volume_label
            or not boot_sector_config.volume_label.strip()
        ):
            boot_sector_config.volume_label = "NO NAME".ljust(11)

        # Validate the config BEFORE writing anything, so an invalid profile does
        # not leave a half-formatted disk (audit fat12_fs.py:632).
        if not boot_sector_config.is_valid():
            raise ValueError(
                f"Invalid FAT configuration in profile '{profile.name}'; "
                "refusing to format."
            )

        boot_sector_bytes = boot_sector_config.to_bytes()
        self.disk.write_sector(0, 0, 0, boot_sector_bytes)
        self.logger.info("Wrote boot sector")

        fat_size_bytes = (
            boot_sector_config.sectors_per_fat * boot_sector_config.bytes_per_sector
        )
        fat_data = bytearray(fat_size_bytes)
        fat_data[0] = boot_sector_config.media_descriptor
        fat_data[1] = 0xFF
        fat_data[2] = 0xFF
        fat_start_lba = boot_sector_config.reserved_sectors
        for i in range(boot_sector_config.num_fats):
            fat_lba = fat_start_lba + (i * boot_sector_config.sectors_per_fat)
            c, h, s = self.disk.physical_format.lba_to_chs(fat_lba)
            self.disk.write_sectors(c, h, s, fat_data)
        self.logger.info(f"Wrote {boot_sector_config.num_fats} FATs")

        root_dir_bytes = boot_sector_config.root_entries * 32
        root_dir_sectors = (
            root_dir_bytes + boot_sector_config.bytes_per_sector - 1
        ) // boot_sector_config.bytes_per_sector
        root_dir_data = bytearray(
            root_dir_sectors * boot_sector_config.bytes_per_sector
        )
        root_dir_start_lba = fat_start_lba + (
            boot_sector_config.num_fats * boot_sector_config.sectors_per_fat
        )
        c, h, s = self.disk.physical_format.lba_to_chs(root_dir_start_lba)
        self.disk.write_sectors(c, h, s, root_dir_data)
        self.logger.info(f"Wrote root directory ({root_dir_sectors} sectors)")

        self.boot_sector = FATVolumeInfo.from_bytes(boot_sector_bytes)
        self.fat_cache = bytearray(fat_data)
        self.fat_dirty = False
        self._cached_allocated_clusters = []
        self._initialize_filesystem_parameters()

    def get_allocated_units(self) -> list[int]:
        """
        Returns a sorted list of all allocated cluster numbers.

        Returns:
            A list of integers representing the used cluster numbers.
        """
        if (
            self.get_validity_score() < self.validity_threshold
            or self.fat_cache is None
        ):
            self.logger.warning(
                "Cannot get allocated units: Filesystem invalid or FAT cache "
                "not loaded."
            )
            return []
        if self._cached_allocated_clusters is not None and not self.fat_dirty:
            return self._cached_allocated_clusters

        allocated_clusters = [
            cluster
            for cluster in range(2, self.num_clusters + 2)
            if self._read_fat_entry_cached(cluster, load_if_missing=False) != 0
        ]
        self._cached_allocated_clusters = allocated_clusters
        self.logger.debug(f"Retrieved {len(allocated_clusters)} allocated clusters")
        return allocated_clusters

    def get_display_info(self) -> dict[str, str]:
        """
        Returns a dictionary of key FAT filesystem parameters for display.

        Returns:
            A dictionary of filesystem properties.
        """
        if self.get_validity_score() < self.validity_threshold or not self.boot_sector:
            return {"Error": "FAT filesystem not valid or BPB missing"}

        bs = self.boot_sector
        return {
            "Filesystem Type": bs.fs_type.strip() if bs.fs_type else "FAT12",
            "Volume Label": (
                bs.volume_label.strip() if bs.volume_label else "(No Label)"
            ),
            "OEM ID": bs.oem_id,
            "Bytes per Sector": str(bs.bytes_per_sector),
            "Sectors per Cluster": str(bs.sectors_per_cluster),
            "Reserved Sectors": str(bs.reserved_sectors),
            "Number of FATs": str(bs.num_fats),
            "Root Directory Entries": str(bs.root_entries),
            "Total Sectors": str(bs.total_sectors),
            "Media Descriptor": f"0x{bs.media_descriptor:02X}",
            "Sectors per FAT": str(bs.sectors_per_fat),
            "Sectors per Track": str(bs.sectors_per_track),
            "Number of Heads": str(bs.num_heads),
            "Hidden Sectors": str(bs.hidden_sectors),
            "Volume Serial": (
                f"0x{bs.volume_serial:08X}" if bs.volume_serial else "N/A"
            ),
            "Drive Number": (f"0x{bs.drive_number:02X}" if bs.drive_number else "N/A"),
        }

    def get_disk_map_layout(self) -> dict[str, Any]:
        """
        Provides data for visualizing the disk layout.

        Returns:
            A dictionary containing legend information and callback functions
            to determine the type of each sector on the disk.
        """
        if self.get_validity_score() < self.validity_threshold or not self.boot_sector:
            return {}
        bs = self.boot_sector
        reserved = bs.reserved_sectors
        fat1_end = reserved + bs.sectors_per_fat
        fat2_end = fat1_end + bs.sectors_per_fat
        root_dir_sectors = (
            bs.root_entries * 32 + bs.bytes_per_sector - 1
        ) // bs.bytes_per_sector
        first_data_sector = (
            reserved + (bs.sectors_per_fat * bs.num_fats) + root_dir_sectors
        )

        def get_fat_sector_type(lba: int) -> str:
            if lba < reserved:
                return "boot"
            if lba < fat1_end:
                return "fat1"
            if bs.num_fats > 1 and lba < fat2_end:
                return "fat2"
            if lba < first_data_sector:
                return "root"

            relative_lba = lba - first_data_sector
            if bs.sectors_per_cluster == 0:
                return "data_free"
            cluster_index = relative_lba // bs.sectors_per_cluster
            fat_cluster_number = cluster_index + 2
            allocated_units = self.get_allocated_units()
            return "data_used" if fat_cluster_number in allocated_units else "data_free"

        legend_colors = {
            "Boot Sector": "#FF0000",
            "FAT1": "#00FF00",
            "FAT2": "#0000FF",
            "Root Directory": "#FFFF00",
            "Used Data Sector": "#FF00FF",
            "Free Data Sector": "#808080",
        }
        legend = [
            ("Boot Sector", legend_colors["Boot Sector"]),
            ("FAT1", legend_colors["FAT1"]),
        ]
        if bs.num_fats > 1:
            legend.append(("FAT2", legend_colors["FAT2"]))
        legend.extend(
            [
                ("Root Directory", legend_colors["Root Directory"]),
                ("Used Data Sector", legend_colors["Used Data Sector"]),
                ("Free Data Sector", legend_colors["Free Data Sector"]),
            ]
        )

        type_map = {
            "boot": legend_colors["Boot Sector"],
            "fat1": legend_colors["FAT1"],
            "fat2": legend_colors["FAT2"] if bs.num_fats > 1 else "#D3D3D3",
            "root": legend_colors["Root Directory"],
            "data_used": legend_colors["Used Data Sector"],
            "data_free": legend_colors["Free Data Sector"],
            "unknown": "#008B8B",
        }

        return {
            "legend": legend,
            "get_sector_type": get_fat_sector_type,
            "allocation_unit_size_sectors": bs.sectors_per_cluster,
            "first_data_sector": first_data_sector,
            "type_color_map": type_map,
        }

    def get_file_allocation_units(self, path: str) -> list[int]:
        """
        Gets the list of cluster numbers allocated to a specific file.

        Args:
            path: The full path to the file.

        Returns:
            A list of cluster numbers used by the file, in order.
            Returns an empty list if the file doesn't exist or has no clusters.

        Raises:
            IOError: If the filesystem is not valid.
            IsADirectoryError: If the path points to a directory.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as FAT.")

        path = self._normalize_path(path)
        self.logger.debug(f"Getting allocation units for file: {path}")

        try:
            file_entry_info = self._find_path(path)
        except FileNotFoundError:
            self.logger.warning(f"File not found: {path}")
            return []

        if not file_entry_info:
            return []

        if file_entry_info.is_dir:
            raise IsADirectoryError(f"Path is a directory, not a file: {path}")

        if file_entry_info.size == 0 or file_entry_info.starting_cluster < 2:
            return []

        cluster_chain = self._get_cluster_chain(file_entry_info.starting_cluster)

        if not cluster_chain:
            self.logger.warning(
                f"Could not get cluster chain for file {path} starting at "
                f"{file_entry_info.starting_cluster}"
            )
            return []

        self.logger.debug(f"File '{path}' uses clusters: {cluster_chain}")
        return cluster_chain

    def get_free_space(self) -> tuple[int, int]:
        """
        Calculates the free and total data space on the disk.

        Returns:
            A tuple containing (free_bytes, total_bytes).
        """
        if self.get_validity_score() < self.validity_threshold:
            return 0, 0
        total_data_bytes = self.num_clusters * self.allocation_unit_size
        allocated_count = len(self.get_allocated_units())
        free_clusters = max(self.num_clusters - allocated_count, 0)
        free_bytes = free_clusters * self.allocation_unit_size
        self.logger.debug(
            f"Free space: {free_bytes} bytes ({free_clusters} clusters), "
            f"Total data space: {total_data_bytes} bytes "
            f"({self.num_clusters} clusters)"
        )
        return free_bytes, total_data_bytes

    def get_specific_config(self) -> Optional[FATVolumeInfo]:
        """
        Returns the specific filesystem configuration object.

        Returns:
            The FATVolumeInfo object, or None if not initialized.
        """
        return self.boot_sector

    def get_validity_score(self) -> int:
        """
        Scores the likelihood that the disk contains a valid FAT filesystem.

        A score is calculated based on the boot sector signature, plausibility
        of BPB values, FAT media descriptor, and root directory entry validity.

        Returns:
            An integer score from 0 to 100.
        """
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        score = 0
        try:
            boot_sector_data = self.disk.read_sector(0, 0, 0)
            if not boot_sector_data:
                return 0

            if (
                len(boot_sector_data) >= 512
                and boot_sector_data[510:512] == b"\x55\xaa"
            ):
                score += 25
            if boot_sector_data[0] in (0xEB, 0xE9):
                score += 5

            parsed_bpb = None
            with contextlib.suppress(ValueError):
                parsed_bpb = FATVolumeInfo.from_bytes(boot_sector_data)

            if parsed_bpb and parsed_bpb.is_valid():
                score += 50
                # Respect a caller-supplied config; only derive the boot sector
                # from disk when none was provided (audit fat12_fs.py:2017).
                if self.boot_sector is None:
                    self.boot_sector = parsed_bpb
                self._try_initialize()
            elif self.boot_sector is None or not self.boot_sector.is_valid():
                # No valid BPB (None, or a garbage on-disk BPB): try to recognise
                # a pre-BPB FAT12 disk (DOS 1.x and early OEM formats) from the
                # disk geometry and the FAT structure itself.
                synthesized = self._try_synthesize_nobpb_bpb()
                if synthesized is not None:
                    self.boot_sector = synthesized
                    score += 50
                    self._init_completed = False
                    self._try_initialize()

            if self._init_completed and self.boot_sector:
                if (
                    self.fat_cache
                    and self.fat_cache[0] == self.boot_sector.media_descriptor
                ):
                    score += 40

                try:
                    root_data = self._read_bytes(
                        self.root_dir_start_offset, self.root_dir_bytes
                    )
                    parsed, valid = 0, 0
                    for i in range(0, len(root_data), 32):
                        entry = root_data[i : i + 32]
                        if len(entry) < 32 or entry[0] == ENTRY_UNUSED:
                            break
                        if entry[0] == ENTRY_DELETED:
                            continue
                        parsed += 1
                        if self._parse_single_directory_entry(entry):
                            valid += 1
                    if parsed > 0:
                        score += int((valid / parsed) * 30)
                except Exception as e:
                    self.logger.debug(
                        f"Could not analyze root directory for scoring: {e}"
                    )

        except Exception as e:
            self.logger.debug(f"Could not read boot sector for scoring: {e}")
            return 0

        final_score = min(score, 100)
        self.logger.info(f"FAT validation score: {final_score}")
        self._cached_validity_score = final_score
        return final_score

    def _read_lba(self, lba: int) -> bytes:
        """Reads one logical block (sector) by LBA using the disk geometry."""
        c, h, s = self.disk.physical_format.lba_to_chs(lba)
        return self.disk.read_sector(c, h, s)

    def _try_synthesize_nobpb_bpb(self) -> Optional["FATVolumeInfo"]:
        """
        Recognises a FAT12 disk that has no BIOS Parameter Block.

        Early DOS (1.x) and some OEM disks carry no BPB - the format was implied
        by the FAT media-descriptor byte and a fixed layout. Using the disk's
        known geometry, this synthesises a candidate BPB and accepts it only if
        the on-disk structure is unmistakably FAT12: a valid media byte, two
        byte-identical FAT copies, and a root directory of only valid 8.3 entries.
        That gate makes false positives on CP/M, HDOS or random data effectively
        impossible, and it runs only when the on-disk BPB is invalid, so genuine
        BPB disks are never affected.

        Returns:
            A synthesised FATVolumeInfo, or None if this is not a no-BPB FAT12 disk.
        """
        pf = self.disk.physical_format
        if not pf or pf.has_variable_bps:
            return None
        bps = pf.bytes_per_sector
        if bps not in (128, 256, 512, 1024):
            return None
        try:
            total_sectors = pf.total_sectors
            spt = pf.get_sectors_per_track(0, 0)
            heads = pf.heads
        except (ValueError, AttributeError):
            return None
        if total_sectors < 4:
            return None

        num_fats = 2
        # Reserved-sector candidates: 1 (standard DOS 1.x) and two whole tracks.
        # 86-DOS / SCP reserve the first two tracks for the bootstrap, so the FAT
        # begins at track 2 (e.g. LBA 52 on an 8" 26-sector disk). The structure
        # verification below gates every candidate, so probing a second offset
        # cannot create a false positive.
        reserved_candidates = [1]
        if spt and (2 * spt) not in reserved_candidates:
            reserved_candidates.append(2 * spt)

        for reserved in reserved_candidates:
            try:
                fat_start = self._read_lba(reserved)
            except Exception:
                continue
            if len(fat_start) < 3:
                continue
            media = fat_start[0]
            # On a FAT12 disk the first three FAT bytes are [media, 0xFF, 0xFF]
            # (the reserved 12-bit entries 0 and 1).
            if (
                not (0xF0 <= media <= 0xFF)
                or fat_start[1] != 0xFF
                or fat_start[2] != 0xFF
            ):
                continue

            for spc in (1, 2, 4, 8, 16):
                for root_entries in (
                    64,
                    112,
                    224,
                    512,
                    16,
                    32,
                    48,
                    96,
                    128,
                    192,
                    240,
                    256,
                ):
                    if (root_entries * 32) % bps != 0:
                        continue
                    spf = self._solve_sectors_per_fat(
                        total_sectors, reserved, num_fats, root_entries, spc, bps
                    )
                    if spf is None:
                        continue
                    candidate = FATVolumeInfo()
                    candidate.oem_id = "FATFLNBP"
                    candidate.bytes_per_sector = bps
                    candidate.sectors_per_cluster = spc
                    candidate.reserved_sectors = reserved
                    candidate.num_fats = num_fats
                    candidate.root_entries = root_entries
                    candidate.total_sectors = total_sectors
                    candidate.media_descriptor = media
                    candidate.sectors_per_fat = spf
                    candidate.sectors_per_track = spt
                    candidate.num_heads = heads
                    candidate.volume_label = ""
                    candidate.fs_type = "FAT12"
                    if not candidate.is_valid():
                        continue
                    if self._verify_nobpb_structure(candidate):
                        self.logger.info(
                            f"Recognised no-BPB FAT12: media=0x{media:02x} "
                            f"reserved={reserved} spc={spc} spf={spf} "
                            f"root={root_entries} total={total_sectors}"
                        )
                        return candidate
        return None

    @staticmethod
    def _solve_sectors_per_fat(
        total_sectors: int,
        reserved: int,
        num_fats: int,
        root_entries: int,
        spc: int,
        bps: int,
    ) -> Optional[int]:
        """
        Solves the 12-bit FAT size for a no-BPB layout from the geometry.

        Iterates the self-referential FAT-size equation to a fixed point. Returns
        the sectors-per-FAT, or None if no consistent 12-bit solution exists.
        """
        root_sectors = (root_entries * 32 + bps - 1) // bps
        spf = 1
        for _ in range(16):
            data_sectors = total_sectors - reserved - num_fats * spf - root_sectors
            if data_sectors <= 0:
                return None
            num_clusters = data_sectors // spc
            n = num_clusters + 2
            fat_bytes = (n * 3 + 1) // 2  # ceil(n * 1.5) for 12-bit entries
            needed = (fat_bytes + bps - 1) // bps
            if needed == spf:
                # FAT12 only addresses fewer than 4085 clusters.
                return spf if num_clusters < 4085 else None
            if needed < spf:
                return None
            spf = needed
        return None

    def _verify_nobpb_structure(self, bpb: "FATVolumeInfo") -> bool:
        """
        Confirms the on-disk structure matches a no-BPB FAT12 candidate.

        Requires the two FAT copies to be byte-identical and the root directory
        to contain only well-formed 8.3 entries (or be empty). This is the gate
        that prevents false positives.
        """
        reserved = bpb.reserved_sectors
        spf = bpb.sectors_per_fat
        bps = bpb.bytes_per_sector

        # The geometry must actually cover the claimed volume: the last sector
        # must be addressable. This rejects an over-large geometry (e.g. a 1.44M
        # generic geometry applied to a 160K image) whose FAT/root sectors happen
        # to alias the real ones (audit/no-BPB false-positive guard).
        try:
            last = self._read_lba(bpb.total_sectors - 1)
            if len(last) < bps:
                return False
        except Exception:
            return False

        try:
            fat1 = bytearray()
            fat2 = bytearray()
            for i in range(spf):
                fat1 += self._read_lba(reserved + i)
                fat2 += self._read_lba(reserved + spf + i)
        except Exception:
            return False
        if bytes(fat1) != bytes(fat2):
            return False

        root_start_lba = reserved + bpb.num_fats * spf
        root_sectors = (bpb.root_entries * 32 + bps - 1) // bps
        try:
            root = bytearray()
            for i in range(root_sectors):
                root += self._read_lba(root_start_lba + i)
        except Exception:
            return False

        cluster_bytes = bpb.sectors_per_cluster * bps
        files_checked = 0
        files_consistent = 0
        for i in range(0, len(root), 32):
            entry = root[i : i + 32]
            if len(entry) < 32 or entry[0] == ENTRY_UNUSED:
                break
            if entry[0] == ENTRY_DELETED:
                continue
            if not self._is_plausible_dir_entry(entry):
                return False
            attr = entry[11]
            if attr & ATTR_LONG_NAME == ATTR_LONG_NAME:
                continue
            if attr & (ATTR_VOLUME_ID | ATTR_DIRECTORY):
                continue
            size = int.from_bytes(entry[28:32], "little")
            if size == 0 or cluster_bytes == 0:
                continue
            start = int.from_bytes(entry[26:28], "little")
            expected = (size + cluster_bytes - 1) // cluster_bytes
            files_checked += 1
            if self._fat_chain_length(bytes(fat1), start, bpb) == expected:
                files_consistent += 1

        # FAT-chain consistency gate. A wrong sector order (e.g. an interleaved
        # disk read in physical order, or a wrong reserved offset) can still
        # present a valid-looking directory, because 32-byte entries are
        # skew-invariant. But the FAT chains such a reading produces will not
        # match the entries' file sizes. Requiring the chains to match rejects a
        # mis-read layout - so the correct profile/geometry is used instead -
        # while a genuinely sequential no-BPB disk passes unchanged.
        return files_checked == 0 or files_consistent >= files_checked

    @staticmethod
    def _fat_chain_length(fat: bytes, start: int, bpb: "FATVolumeInfo") -> int:
        """
        Returns the FAT12 cluster-chain length from `start`, or -1 if broken.

        A broken chain (a free/out-of-range link before an end-of-chain marker)
        returns -1 so it never matches an expected length.
        """
        max_clusters = bpb.total_sectors // max(1, bpb.sectors_per_cluster) + 2
        count = 0
        cluster = start
        while 2 <= cluster < 0xFF0 and count <= max_clusters:
            count += 1
            offset = (cluster * 3) // 2
            if offset + 1 >= len(fat):
                return -1
            value = fat[offset] | (fat[offset + 1] << 8)
            cluster = (value & 0x0FFF) if cluster % 2 == 0 else (value >> 4)
        return count if cluster >= FAT12_EOC_MIN else -1

    @staticmethod
    def _is_plausible_dir_entry(entry: bytes) -> bool:
        """Checks a 32-byte directory entry looks like a real 8.3 record."""
        attr = entry[11]
        if attr & ATTR_LONG_NAME == ATTR_LONG_NAME:
            return True
        if attr & 0xC0:  # undefined high attribute bits -> garbage
            return False
        # 8.3 name bytes must be printable (early DOS used uppercase ASCII);
        # 0x05 is the legal kanji-escape first byte.
        return all(b == 0x05 or (0x20 <= b < 0x7F) for b in entry[0:11])

    def get_volume_label(self) -> Optional[str]:
        """
        Returns the FAT volume label from the boot sector.

        Returns:
            The volume label as a string, or None if not available.
        """
        if self.boot_sector and self.boot_sector.volume_label:
            return self.boot_sector.volume_label.strip()
        return None

    def list_directory(self, path: str = "/") -> list[FileInfo]:
        """
        Lists all files and subdirectories in a given directory.

        Args:
            path: The directory path to list. Defaults to the root directory.

        Returns:
            A list of FileInfo objects representing the directory contents.

        Raises:
            IOError: If the filesystem is not valid.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Listing directory: {path}")

        if path == "/":
            results = self._list_directory_by_cluster(0)
        else:
            dir_entry_info = self._find_path(path)
            if not dir_entry_info or not dir_entry_info.is_dir:
                self.logger.warning(f"Path not found or not a directory: {path}")
                return []
            results = self._list_directory_by_cluster(dir_entry_info.starting_cluster)

        return [
            entry
            for entry in results
            if entry.name not in [".", ".."] and entry.attributes != "VOL"
        ]

    def read_file(self, path: str) -> bytes:
        """
        Reads the complete content of a specified file.

        Args:
            path: The path of the file to read.

        Returns:
            The binary content of the file.

        Raises:
            IOError: If the filesystem is not valid.
            FileNotFoundError: If the file cannot be found.
            IsADirectoryError: If the path points to a directory.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Reading file: {path}")

        file_entry_info = self._find_path(path)
        if not file_entry_info:
            raise FileNotFoundError(f"File not found: {path}")
        if file_entry_info.is_dir:
            raise IsADirectoryError(f"Path is a directory: {path}")
        if file_entry_info.size == 0 or file_entry_info.starting_cluster < 2:
            return b""

        cluster_chain = self._get_cluster_chain(file_entry_info.starting_cluster)
        if not cluster_chain:
            self.logger.warning(
                f"Could not get cluster chain for file {path} starting at "
                f"{file_entry_info.starting_cluster}"
            )
            return b""

        file_data = self._read_cluster_chain_data(cluster_chain)
        return file_data[: file_entry_info.size]

    def suggest_import_name(
        self, host_name: str, existing_names: Iterable[str], is_dir: bool = False
    ) -> str:
        """
        Derives a valid, unique 8.3 name, avoiding DOS reserved device names.

        Extends the base 8.3 policy: if the sanitized base name collides with a
        reserved device name (CON, PRN, ...), a '_' is appended so the result
        always passes _is_valid_83_filename().

        Args:
            host_name: The filename from the host filesystem.
            existing_names: Names already present in the target directory.
            is_dir: True when importing a directory entry (no extension).

        Returns:
            A valid, unique on-disk 8.3 name.
        """
        name = super().suggest_import_name(host_name, existing_names, is_dir)
        base, dot, ext = name.partition(".")
        if base in FAT_RESERVED_NAMES:
            base = (base + "_")[:8]
            name = f"{base}.{ext}" if dot else base
            if name.upper() in {n.upper() for n in existing_names}:
                name = super().suggest_import_name(name, existing_names, is_dir)
        return name

    def write_file(self, path: str, data: bytes) -> None:
        """
        Writes data to a file on the FAT filesystem.

        If the file exists, it will be deleted and overwritten.

        Args:
            path: The path of the file to write.
            data: The binary data to write to the file.

        Raises:
            IOError: If the filesystem is invalid, there's not enough space,
                or the directory is full.
            ValueError: If the filename is not a valid 8.3 format.
            IsADirectoryError: If a directory exists at the target path.
        """
        if self.get_validity_score() < self.validity_threshold:
            raise OSError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Writing file: {path}, size: {len(data)} bytes")

        parent_path, file_name = self._split_path(path)
        if not self._is_valid_83_filename(file_name):
            raise ValueError(f"Invalid 8.3 filename: '{file_name}'")

        parent_dir_cluster = self._get_directory_cluster(parent_path)

        num_clusters_needed = (
            (len(data) + self.allocation_unit_size - 1) // self.allocation_unit_size
            if data
            else 0
        )

        existing_entry = None
        try:
            existing_entry, _ = self._find_entry_in_directory(
                parent_dir_cluster, file_name
            )
            if existing_entry and existing_entry.is_dir:
                raise IsADirectoryError(
                    f"Cannot overwrite directory with a file: {path}"
                )
        except FileNotFoundError:
            existing_entry = None

        # Verify the new data fits BEFORE destroying the old file, counting the
        # clusters the old file would release. This guarantees a failed overwrite
        # can never lose the original (audit fat12_fs.py:1082).
        existing_cluster_count = 0
        if (
            existing_entry
            and not existing_entry.is_dir
            and existing_entry.starting_cluster >= 2
        ):
            existing_cluster_count = len(
                self._get_cluster_chain(existing_entry.starting_cluster)
            )
        free_bytes, _ = self.get_free_space()
        free_clusters = free_bytes // self.allocation_unit_size
        if num_clusters_needed > free_clusters + existing_cluster_count:
            raise OSError("Not enough free space to write file")

        if existing_entry and not existing_entry.is_dir:
            self.logger.debug(f"File '{file_name}' exists, deleting before overwrite.")
            self.delete(path)

        start_cluster = 0
        if num_clusters_needed > 0:
            clusters = self._allocate_cluster_chain(num_clusters_needed)
            if not clusters:
                raise OSError("Not enough free space to write file")
            start_cluster = clusters[0]
            # Roll back the allocation if the data write fails, so a partial
            # write does not leave lost clusters behind (audit fat12_fs.py:1097).
            try:
                self._write_cluster_chain_data(clusters, data)
            except Exception:
                self._free_cluster_chain(start_cluster)
                self._commit_fat()
                raise

        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            if start_cluster > 0:
                self._free_cluster_chain(start_cluster)
                self._commit_fat()
            raise OSError(f"No space in directory {parent_path}")

        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(
            dir_cluster_num, entry_offset_in_cluster
        )
        entry_bytes = self._create_directory_entry_bytes(
            name=file_name,
            is_dir=False,
            starting_cluster=start_cluster,
            size=len(data),
            dt=datetime.datetime.now(),
        )
        # Roll back the allocation if the directory-entry write fails, so the
        # clusters are not leaked as lost (audit fat12_fs.py:1117).
        try:
            self._write_bytes(entry_disk_offset, entry_bytes)
        except Exception:
            if start_cluster > 0:
                self._free_cluster_chain(start_cluster)
                self._commit_fat()
            raise

        self._commit_fat()
        self.disk.flush()

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[list[int]]:
        """
        Allocates a chain of free clusters in the FAT.

        Args:
            num_clusters: Number of clusters to allocate.

        Returns:
            List of allocated cluster numbers, or None on failure.

        Raises:
            ValueError: If filesystem not initialized.
            IOError: If not enough free clusters available.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if num_clusters <= 0:
            return []
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Failed to load FAT cache for allocating cluster chain.")
            return None

        allocated_clusters = []
        last_allocated = None
        try:
            for _ in range(num_clusters):
                free_cluster = self._find_free_cluster()
                if free_cluster is None:
                    raise OSError(
                        f"Could not allocate {num_clusters} clusters, only "
                        f"found {len(allocated_clusters)}."
                    )
                self._set_fat_entry_cached(free_cluster, FAT12_EOC)
                allocated_clusters.append(free_cluster)
                if last_allocated is not None:
                    self._set_fat_entry_cached(last_allocated, free_cluster)
                last_allocated = free_cluster

            self.logger.debug(
                f"Successfully allocated cluster chain: {allocated_clusters}"
            )
            return allocated_clusters
        except Exception as e:
            self.logger.error(f"Error during cluster allocation: {e}. Rolling back.")
            if allocated_clusters:
                self._free_cluster_chain(allocated_clusters[0])
            return None

    def _cluster_to_offset(self, cluster: int) -> int:
        """
        Converts a cluster number to its byte offset from the start of the disk.

        Args:
            cluster: Cluster number to convert.

        Returns:
            Byte offset on disk.

        Raises:
            ValueError: If filesystem not initialized or cluster invalid.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if cluster < 2:
            raise ValueError(
                f"Invalid cluster number for offset calculation: {cluster}"
            )
        return self.data_area_start_offset + ((cluster - 2) * self.allocation_unit_size)

    def _commit_fat(self) -> None:
        """Writes the dirty FAT cache to all FAT copies on the disk."""
        self._write_fat_sectors()

    def _create_directory_entry_bytes(
        self,
        name: str,
        is_dir: bool,
        starting_cluster: int,
        size: int,
        dt: datetime.datetime,
    ) -> bytes:
        """
        Creates a 32-byte directory entry from file metadata.

        Args:
            name: Filename (8.3 format or special '.' '..')
            is_dir: True if this is a directory entry.
            starting_cluster: Starting cluster number.
            size: File size in bytes.
            dt: Datetime for the entry.

        Returns:
            32-byte directory entry.
        """
        entry = bytearray(32)
        if name in [".", ".."]:
            fname_bytes = name.ljust(11).encode("cp437")
        else:
            fname_bytes = self._format_83_filename(name)
        entry[0:11] = fname_bytes

        entry[11] = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE
        time_val = (
            ((dt.hour & 0x1F) << 11)
            | ((dt.minute & 0x3F) << 5)
            | ((dt.second // 2) & 0x1F)
        )
        date_val = (
            (((dt.year - 1980) & 0x7F) << 9)
            | ((dt.month & 0x0F) << 5)
            | (dt.day & 0x1F)
        )
        struct.pack_into("<H", entry, 22, time_val)
        struct.pack_into("<H", entry, 24, date_val)
        struct.pack_into("<H", entry, 26, starting_cluster & 0xFFFF)
        struct.pack_into("<I", entry, 28, size if not is_dir else 0)
        return bytes(entry)

    def _find_entry_in_directory(
        self, dir_cluster: int, name_to_find: str
    ) -> tuple[FileInfo, tuple[int, int]]:
        """
        Finds a directory entry by name within a given directory cluster.

        Args:
            dir_cluster: Cluster number of the directory (0 for root).
            name_to_find: Name to search for.

        Returns:
            A tuple containing the FileInfo object and its location
            (cluster_num, offset_in_cluster).

        Raises:
            ValueError: If filesystem not initialized.
            FileNotFoundError: If entry not found.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        name_upper = name_to_find.upper()

        def find_in_data(
            data: bytes, cluster_num: int
        ) -> Optional[tuple[FileInfo, tuple[int, int]]]:
            for i in range(0, len(data), 32):
                entry_data = data[i : i + 32]
                if len(entry_data) < 32 or entry_data[0] == ENTRY_UNUSED:
                    break
                if entry_data[0] == ENTRY_DELETED:
                    continue
                entry = self._parse_single_directory_entry(entry_data)
                if entry and entry.name.upper() == name_upper:
                    return entry, (cluster_num, i)
            return None

        if dir_cluster == 0:
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            result = find_in_data(dir_data, 0)
            if result:
                return result
        elif dir_cluster >= 2:
            cluster_chain = self._get_cluster_chain(dir_cluster)
            for c in cluster_chain:
                cluster_data = self._read_bytes(
                    self._cluster_to_offset(c), self.allocation_unit_size
                )
                result = find_in_data(cluster_data, c)
                if result:
                    return result

        raise FileNotFoundError(
            f"Entry '{name_to_find}' not found in directory cluster {dir_cluster}"
        )

    def _find_free_cluster(self) -> Optional[int]:
        """
        Finds the first available (zero) cluster in the FAT.

        Returns:
            Cluster number, or None if no free clusters.

        Raises:
            ValueError: If filesystem not initialized.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Failed to load FAT cache for finding free cluster.")
            return None
        for cluster in range(2, self.num_clusters + 2):
            if self._read_fat_entry_cached(cluster, load_if_missing=False) == 0:
                return cluster
        self.logger.warning("No free clusters found on the disk.")
        return None

    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[tuple[int, int]]:
        """
        Finds a free slot in a directory for a new entry.

        Args:
            dir_cluster: Directory cluster number (0 for root).

        Returns:
            A tuple of (cluster_number, offset_in_cluster) or None if full.

        Raises:
            ValueError: If filesystem not initialized or invalid cluster.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")

        def find_in_data(data: bytes, cluster_num: int) -> Optional[tuple[int, int]]:
            for i in range(0, len(data), 32):
                if data[i] in (ENTRY_UNUSED, ENTRY_DELETED):
                    return cluster_num, i
            return None

        if dir_cluster == 0:
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            found = find_in_data(dir_data, 0)
            if found:
                return found
            self.logger.warning("Root directory is full.")
            return None
        elif dir_cluster >= 2:
            cluster_chain = self._get_cluster_chain(dir_cluster)
            if not cluster_chain:
                self.logger.error(
                    f"Cannot find free entry: Invalid cluster chain for "
                    f"directory {dir_cluster}"
                )
                return None
            for c in cluster_chain:
                cluster_data = self._read_bytes(
                    self._cluster_to_offset(c), self.allocation_unit_size
                )
                found = find_in_data(cluster_data, c)
                if found:
                    return found
            last_cluster = cluster_chain[-1]
            new_cluster = self._find_free_cluster()
            if new_cluster is None:
                self.logger.warning(
                    f"Subdirectory in cluster {dir_cluster} is full and no "
                    "free clusters to extend."
                )
                return None
            self._set_fat_entry_cached(last_cluster, new_cluster)
            self._set_fat_entry_cached(new_cluster, FAT12_EOC)
            self._write_bytes(
                self._cluster_to_offset(new_cluster), bytes(self.allocation_unit_size)
            )
            return new_cluster, 0

        raise ValueError(f"Invalid directory cluster specified: {dir_cluster}")

    def _find_path(self, path: str) -> Optional[FileInfo]:
        """
        Traverses a path to find the final file or directory.

        Args:
            path: Path to find.

        Returns:
            FileInfo for the target, or None.

        Raises:
            ValueError: If filesystem not initialized.
            FileNotFoundError: If path component not found.
            NotADirectoryError: If path component is not a directory.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        path = self._normalize_path(path)
        if path == "/":
            return FileInfo(
                name="/",
                size=0,
                is_dir=True,
                datetime=datetime.datetime(1980, 1, 1),
                attributes="D",
                starting_cluster=0,
            )

        parts = path.strip("/").split("/")
        current_cluster = 0
        current_entry_info: Optional[FileInfo] = None

        for part_name in parts:
            found_entry: Optional[FileInfo] = None
            entries_in_current = self._list_directory_by_cluster(current_cluster)
            for entry in entries_in_current:
                if entry.name.upper() == part_name.upper():
                    found_entry = entry
                    break
            if found_entry:
                if not found_entry.is_dir and part_name != parts[-1]:
                    raise NotADirectoryError(
                        f"Path component '{part_name}' is not a directory"
                    )
                current_entry_info = found_entry
                current_cluster = found_entry.starting_cluster
            else:
                raise FileNotFoundError(
                    f"Path component '{part_name}' not found in '{path}'"
                )

        return current_entry_info

    def _format_83_filename(self, name: str) -> bytes:
        """
        Formats a string into an 8.3 filename byte representation.

        Args:
            name: Filename to format.

        Returns:
            11-byte formatted filename.
        """
        parts = name.upper().split(".", 1)
        base_name = parts[0]
        extension = parts[1] if len(parts) > 1 else ""
        formatted_name = base_name.ljust(8)[:8].encode("cp437", errors="replace")
        formatted_ext = extension.ljust(3)[:3].encode("cp437", errors="replace")
        return formatted_name + formatted_ext

    def _free_cluster_chain(self, start_cluster: int) -> None:
        """
        Frees an entire cluster chain in the FAT by setting entries to 0.

        Args:
            start_cluster: First cluster in the chain.

        Raises:
            ValueError: If filesystem not initialized.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if start_cluster < 2:
            self.logger.warning(
                f"Invalid start cluster for freeing chain: {start_cluster}"
            )
            return
        if self.fat_cache is None and not self._load_fat_cache():
            return

        current = start_cluster
        freed_count = 0
        for _ in range(self.num_clusters + 2):
            if not (2 <= current < self.num_clusters + 2):
                self.logger.error(
                    f"Invalid cluster {current} in chain from {start_cluster}."
                )
                break
            next_cluster = self._read_fat_entry_cached(current, load_if_missing=False)
            self._set_fat_entry_cached(current, 0)
            freed_count += 1
            if FAT12_EOC_MIN <= next_cluster <= FAT12_EOC or next_cluster == 0:
                break
            current = next_cluster
        self.logger.debug(
            f"Freed {freed_count} clusters in chain starting at {start_cluster}"
        )

    def _get_cluster_chain(self, start_cluster: int) -> list[int]:
        """
        Reads the FAT to trace and return a list of clusters in a chain.

        Args:
            start_cluster: First cluster in the chain.

        Returns:
            List of cluster numbers in the chain.

        Raises:
            ValueError: If filesystem not initialized.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if not (2 <= start_cluster < self.num_clusters + 2):
            self.logger.warning(
                f"Invalid start cluster for chain retrieval: {start_cluster}"
            )
            return []
        if self.fat_cache is None and not self._load_fat_cache():
            return []

        chain = []
        current = start_cluster
        for _ in range(self.num_clusters + 2):
            if current in chain:
                self.logger.error(
                    f"Loop detected in cluster chain at cluster {current}."
                )
                break
            chain.append(current)
            next_cluster = self._read_fat_entry_cached(current)
            if FAT12_EOC_MIN <= next_cluster <= FAT12_EOC:
                break
            if next_cluster in {0, FAT12_BAD_CLUSTER}:
                self.logger.error(
                    f"Chain terminated unexpectedly at cluster {current} with "
                    f"value {next_cluster:03X}."
                )
                break
            # Reject unaddressable cluster numbers (reserved cluster 1, or values
            # past the disk) before they enter the chain, so consumers never call
            # _cluster_to_offset on an invalid cluster (audit fat12_fs.py:1516).
            if not (2 <= next_cluster < self.num_clusters + 2):
                self.logger.error(
                    f"Invalid next cluster {next_cluster:03X} in chain from "
                    f"{start_cluster}; truncating chain."
                )
                break
            current = next_cluster

        self.logger.debug(f"Retrieved cluster chain for {start_cluster}: {chain}")
        return chain

    def _get_directory_cluster(self, dir_path: str) -> int:
        """
        Returns the starting cluster number for a given directory path.

        Args:
            dir_path: Directory path.

        Returns:
            Cluster number (0 for root).

        Raises:
            ValueError: If filesystem not initialized.
            FileNotFoundError: If directory not found.
            NotADirectoryError: If path is not a directory.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        dir_path = self._normalize_path(dir_path)
        if dir_path == "/":
            return 0

        dir_entry_info = self._find_path(dir_path)
        if not dir_entry_info:
            raise FileNotFoundError(f"Directory not found: {dir_path}")
        if not dir_entry_info.is_dir:
            raise NotADirectoryError(f"Not a directory: {dir_path}")
        return dir_entry_info.starting_cluster

    def _get_offset_for_directory_entry(
        self, dir_cluster_num: int, entry_offset_in_cluster: int
    ) -> int:
        """
        Calculates the absolute disk offset for a directory entry.

        Args:
            dir_cluster_num: Cluster containing the entry (0 for root).
            entry_offset_in_cluster: Offset within the cluster.

        Returns:
            Absolute byte offset on disk.

        Raises:
            ValueError: If filesystem not initialized or invalid cluster.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if dir_cluster_num == 0:
            return self.root_dir_start_offset + entry_offset_in_cluster
        if dir_cluster_num >= 2:
            return self._cluster_to_offset(dir_cluster_num) + entry_offset_in_cluster
        raise ValueError(
            f"Invalid cluster number for directory entry: {dir_cluster_num}"
        )

    def _initialize_filesystem_parameters(self) -> None:
        """
        Calculates and sets key filesystem offsets and sizes from the BPB.

        Raises:
            ValueError: If boot sector invalid or contains zero values.
        """
        if not self.boot_sector or not self.boot_sector.is_valid():
            raise ValueError(
                "Cannot initialize FAT parameters: Invalid Boot Sector / BPB"
            )
        bpb = self.boot_sector
        if bpb.bytes_per_sector == 0 or bpb.sectors_per_cluster == 0:
            raise ValueError(
                "BPB contains zero value for sector size or sectors per cluster."
            )

        self.allocation_unit_size = bpb.sectors_per_cluster * bpb.bytes_per_sector
        self.fat_start_offset = bpb.reserved_sectors * bpb.bytes_per_sector
        self.fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start_offset = self.fat_start_offset + (
            bpb.num_fats * self.fat_size_bytes
        )
        self.root_dir_bytes = bpb.root_entries * 32
        self.data_area_start_offset = self.root_dir_start_offset + self.root_dir_bytes
        first_data_sector = (
            self.data_area_start_offset + bpb.bytes_per_sector - 1
        ) // bpb.bytes_per_sector
        total_data_sectors = max(0, bpb.total_sectors - first_data_sector)
        self.num_clusters = total_data_sectors // bpb.sectors_per_cluster
        self._init_completed = True
        self.logger.debug("FAT filesystem parameters initialized.")

    def _is_valid_83_filename(
        self, name: str, allow_dots: bool = True, allow_extension: bool = True
    ) -> bool:
        """
        Checks if a name conforms to the 8.3 filename standard.

        Args:
            name: Filename to validate.
            allow_dots: Whether to allow '.' and '..' as valid names.
            allow_extension: Whether to allow file extensions.

        Returns:
            True if valid 8.3 filename, False otherwise.
        """
        if not name or (name.endswith(".") and name not in [".", ".."]):
            return False
        if name in [".", ".."]:
            return allow_dots
        if self._invalid_83_chars_pattern.search(name) or any(
            0 < ord(c) < 32 for c in name
        ):
            return False

        parts = name.split(".", 1)
        base = parts[0]
        ext = parts[1] if len(parts) > 1 else ""

        if not base or len(base) > 8:
            return False
        if len(ext) > 3:
            return False
        if ext and not allow_extension:
            return False
        if base.upper() in self._reserved_names:
            return False
        if base.strip() != base or ext.strip() != ext:
            return False
        return not (base.startswith(".") or ext and ext.startswith("."))

    def _list_directory_by_cluster(self, cluster: int) -> list[FileInfo]:
        """
        Reads and parses all directory entries for a given cluster chain.

        Args:
            cluster: Starting cluster (0 for root directory).

        Returns:
            List of FileInfo objects.

        Raises:
            ValueError: If filesystem not initialized.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if cluster == 0:
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            return self._parse_directory_data(dir_data)
        if cluster >= 2:
            cluster_chain = self._get_cluster_chain(cluster)
            dir_data = self._read_cluster_chain_data(cluster_chain)
            return self._parse_directory_data(dir_data)
        return []

    def _load_boot_sector(self) -> None:
        """Reads sector 0 and attempts to parse it as a FAT boot sector."""
        # Respect an already-set config (caller-supplied, or a synthesized no-BPB
        # boot sector) instead of clobbering it with the on-disk bytes, which may
        # be garbage on a pre-BPB disk (audit fat12_fs.py:2017).
        if self.boot_sector is not None and self.boot_sector.is_valid():
            return
        try:
            boot_sector_data = self.disk.read_sector(0, 0, 0)
            if not boot_sector_data:
                self.logger.error("Boot sector read returned empty data.")
                self.boot_sector = None
                return
            self.boot_sector = FATVolumeInfo.from_bytes(boot_sector_data)
            self.logger.debug("Loaded and parsed boot sector from disk.")
        except Exception as e:
            self.logger.error(f"Error reading or parsing boot sector: {e}")
            self.boot_sector = None

    def _load_fat_cache(self) -> bool:
        """
        Loads the first FAT from the disk into the memory cache.

        Returns:
            True if successful, False otherwise.
        """
        if self.fat_cache is not None:
            return True
        if not self._init_completed:
            return False
        try:
            self.logger.debug(
                f"Loading FAT cache from offset {self.fat_start_offset}, "
                f"size {self.fat_size_bytes} bytes."
            )
            self.fat_cache = bytearray(
                self._read_bytes(self.fat_start_offset, self.fat_size_bytes)
            )
            self.fat_dirty = False
            return True
        except OSError as e:
            self.logger.error(f"IOError loading FAT cache: {e}")
            self.fat_cache = None
            return False

    def _normalize_path(self, path: str) -> str:
        """
        Normalizes a path string to use forward slashes and removes duplicates.

        Args:
            path: Path to normalize.

        Returns:
            Normalized path string.
        """
        path = path.replace("\\", "/").strip()
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        path = re.sub("/+", "/", path)
        return path if path else "/"

    def _parse_directory_data(self, dir_data: bytes) -> list[FileInfo]:
        """
        Parses a block of raw directory data into a list of FileInfo objects.

        Args:
            dir_data: Raw directory entry data.

        Returns:
            List of FileInfo objects.
        """
        entries = []
        for i in range(0, len(dir_data), 32):
            entry_data = dir_data[i : i + 32]
            if len(entry_data) < 32:
                break
            if entry_data[0] == ENTRY_UNUSED:
                break
            if entry_data[0] == ENTRY_DELETED:
                continue
            entry = self._parse_single_directory_entry(entry_data)
            if entry:
                entries.append(entry)
        return entries

    def _parse_fat_datetime(self, date_val: int, time_val: int) -> datetime.datetime:
        """
        Converts FAT's packed date and time format into a datetime object.

        Args:
            date_val: Packed date value.
            time_val: Packed time value.

        Returns:
            datetime object.
        """
        try:
            year = 1980 + ((date_val >> 9) & 0x7F)
            month = (date_val >> 5) & 0x0F
            day = date_val & 0x1F
            hour = (time_val >> 11) & 0x1F
            mins = (time_val >> 5) & 0x3F
            secs = (time_val & 0x1F) * 2
            return datetime.datetime(year, month, day, hour, mins, secs)
        except ValueError:
            return datetime.datetime(1980, 1, 1)

    def _parse_single_directory_entry(self, entry_data: bytes) -> Optional[FileInfo]:
        """
        Parses a single 32-byte directory entry.

        Args:
            entry_data: 32-byte directory entry.

        Returns:
            FileInfo object or None if entry is invalid or should be skipped.
        """
        try:
            attributes = entry_data[11]
            if attributes & ATTR_LONG_NAME == ATTR_LONG_NAME:
                return None
            if attributes & ATTR_VOLUME_ID:
                return FileInfo(
                    name=entry_data[0:11].decode("cp437").strip(),
                    size=0,
                    is_dir=False,
                    datetime=datetime.datetime.min,
                    attributes="VOL",
                    starting_cluster=0,
                )

            raw_name = entry_data[0:8]
            if raw_name[0] == 0x05:
                raw_name = b"\xe5" + raw_name[1:]
            base_name = raw_name.decode("cp437").rstrip()
            extension = entry_data[8:11].decode("cp437").rstrip()
            full_name = f"{base_name}.{extension}" if extension else base_name

            if full_name not in [".", ".."] and not self._is_valid_83_filename(
                full_name, allow_dots=False
            ):
                self.logger.warning(
                    f"Skipping directory entry with invalid 8.3 name: {full_name!r}"
                )
                return None

            is_dir = bool(attributes & ATTR_DIRECTORY)
            size = struct.unpack("<I", entry_data[28:32])[0]
            cluster = struct.unpack("<H", entry_data[26:28])[0]
            dt = self._parse_fat_datetime(
                struct.unpack("<H", entry_data[24:26])[0],
                struct.unpack("<H", entry_data[22:24])[0],
            )

            attr_str = (
                "".join(
                    [
                        "R" if attributes & ATTR_READ_ONLY else "",
                        "H" if attributes & ATTR_HIDDEN else "",
                        "S" if attributes & ATTR_SYSTEM else "",
                        "D" if is_dir else "",
                        "A" if attributes & ATTR_ARCHIVE else "",
                    ]
                )
                or "-"
            )

            return FileInfo(
                name=full_name,
                size=size if not is_dir else 0,
                is_dir=is_dir,
                datetime=dt,
                attributes=attr_str,
                starting_cluster=cluster,
            )
        except (UnicodeDecodeError, struct.error, IndexError) as e:
            self.logger.error(
                f"Failed to parse directory entry: {e}, data: {entry_data!r}"
            )
            return None

    def _read_bytes(self, offset: int, length: int) -> bytes:
        """
        Reads a specific number of bytes from an absolute disk offset.

        Args:
            offset: Byte offset on disk.
            length: Number of bytes to read.

        Returns:
            Requested bytes.

        Raises:
            ValueError: If filesystem not initialized.
            IOError: If read fails.
        """
        if not self._init_completed or not self.boot_sector:
            raise ValueError("Filesystem not initialized for reading.")
        if length <= 0:
            return b""
        bps = self.boot_sector.bytes_per_sector
        start_lba = offset // bps
        end_lba = (offset + length - 1) // bps
        num_sectors = end_lba - start_lba + 1

        try:
            c, h, s = self.disk.physical_format.lba_to_chs(start_lba)
            all_data = self.disk.read_sectors(c, h, s, num_sectors)
            start_offset_in_data = offset % bps
            end_offset_in_data = start_offset_in_data + length
            if len(all_data) < end_offset_in_data:
                raise OSError(
                    f"Short read: expected {end_offset_in_data} bytes in "
                    f"buffer, got {len(all_data)}"
                )
            return all_data[start_offset_in_data:end_offset_in_data]
        except (OSError, ValueError) as e:
            raise OSError(
                f"Failed to read {length} bytes at offset {offset}: {e}"
            ) from e

    def _read_cluster_chain_data(self, cluster_chain: list[int]) -> bytes:
        """
        Reads the full data content of a file from its cluster chain.

        Args:
            cluster_chain: List of cluster numbers.

        Returns:
            Combined data from all clusters.

        Raises:
            ValueError: If filesystem not initialized.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if not cluster_chain:
            return b""
        result = bytearray()
        for cluster in cluster_chain:
            offset = self._cluster_to_offset(cluster)
            result.extend(self._read_bytes(offset, self.allocation_unit_size))
        return bytes(result)

    def _read_fat_entry_cached(self, cluster: int, load_if_missing: bool = True) -> int:
        """
        Reads a 12-bit FAT entry from the cache.

        Args:
            cluster: Cluster number to read.
            load_if_missing: Whether to load FAT cache if not present.

        Returns:
            FAT entry value.

        Raises:
            ValueError: If filesystem not initialized or FAT cache unavailable.
            IOError: If FAT cache loading fails.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None:
            if load_if_missing and not self._load_fat_cache():
                raise OSError("Failed to load FAT cache for reading entry")
            elif self.fat_cache is None:
                raise ValueError("FAT cache unavailable")
        if not (0 <= cluster < self.num_clusters + 2):
            return FAT12_BAD_CLUSTER

        byte_offset = (cluster * 3) // 2
        if byte_offset + 1 >= len(self.fat_cache):
            return FAT12_BAD_CLUSTER

        value = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
        return (value & 0x0FFF) if (cluster % 2 == 0) else (value >> 4)

    def _set_fat_entry_cached(self, cluster: int, value: int) -> None:
        """
        Writes a 12-bit FAT entry to the cache.

        Args:
            cluster: Cluster number to write.
            value: Value to write.

        Raises:
            ValueError: If filesystem not initialized or cluster invalid.
            IOError: If FAT cache loading fails.
            IndexError: If FAT offset out of bounds.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and not self._load_fat_cache():
            raise OSError("Failed to load FAT cache for writing entry")
        if not (2 <= cluster < self.num_clusters + 2):
            raise ValueError(f"Invalid cluster number: {cluster}")

        byte_offset = (cluster * 3) // 2
        if byte_offset + 1 >= len(self.fat_cache):
            raise IndexError("FAT offset out of bounds")

        current_word = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
        if cluster % 2 == 0:
            new_word = (current_word & 0xF000) | (value & 0x0FFF)
        else:
            new_word = (current_word & 0x000F) | ((value & 0x0FFF) << 4)

        if new_word != current_word:
            struct.pack_into("<H", self.fat_cache, byte_offset, new_word)
            self.fat_dirty = True
            self._cached_allocated_clusters = None

    def _split_path(self, path: str) -> tuple[str, str]:
        """
        Splits a path into its parent directory path and the final component.

        Args:
            path: Path to split.

        Returns:
            Tuple of (parent_path, filename).
        """
        path = self._normalize_path(path)
        if path == "/":
            return "/", ""
        last_slash = path.rfind("/")
        if last_slash == 0:
            return "/", path[1:]
        return path[:last_slash], path[last_slash + 1 :]

    def _try_initialize(self) -> None:
        """
        Attempts to load the boot sector and initialize filesystem parameters.

        This method suppresses errors to allow for safe instantiation on
        non-FAT disks.
        """
        try:
            self._load_boot_sector()

            if self.boot_sector and self.boot_sector.is_valid():
                self._initialize_filesystem_parameters()
                self._load_fat_cache()
                self._init_completed = True
        except (OSError, ValueError) as e:
            self.logger.debug(f"FAT Initialization failed during initial check: {e}")
            self._init_completed = False

    def _write_bytes(self, offset: int, data: bytes) -> None:
        """
        Writes a byte string to an absolute disk offset.

        Handles sector alignment automatically.

        Args:
            offset: Byte offset on disk.
            data: Data to write.

        Raises:
            ValueError: If filesystem not initialized.
        """
        if not data:
            return
        if not self._init_completed or not self.boot_sector:
            raise ValueError("Filesystem not initialized for writing.")
        bps = self.boot_sector.bytes_per_sector
        start_lba = offset // bps
        offset_in_first_sector = offset % bps

        data_to_write = data
        current_lba = start_lba

        if offset_in_first_sector != 0:
            c, h, s = self.disk.physical_format.lba_to_chs(start_lba)
            sector_data = bytearray(self.disk.read_sector(c, h, s))
            bytes_in_sector = min(len(data), bps - offset_in_first_sector)
            sector_data[
                offset_in_first_sector : offset_in_first_sector + bytes_in_sector
            ] = data[:bytes_in_sector]
            self.disk.write_sector(c, h, s, bytes(sector_data))
            data_to_write = data[bytes_in_sector:]
            current_lba += 1

        num_full_sectors = len(data_to_write) // bps
        if num_full_sectors > 0:
            full_sectors_data = data_to_write[: num_full_sectors * bps]
            c, h, s = self.disk.physical_format.lba_to_chs(current_lba)
            self.disk.write_sectors(c, h, s, full_sectors_data)
            data_to_write = data_to_write[len(full_sectors_data) :]
            current_lba += num_full_sectors

        if data_to_write:
            c, h, s = self.disk.physical_format.lba_to_chs(current_lba)
            sector_data = bytearray(self.disk.read_sector(c, h, s))
            sector_data[: len(data_to_write)] = data_to_write
            self.disk.write_sector(c, h, s, bytes(sector_data))

    def _write_cluster_chain_data(self, cluster_chain: list[int], data: bytes) -> None:
        """
        Writes file data across a chain of clusters.

        Args:
            cluster_chain: List of cluster numbers to write to.
            data: Data to write.

        Raises:
            ValueError: If filesystem not initialized or cluster chain empty
                with data present.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if not cluster_chain and data:
            raise ValueError("Cluster chain is empty but data is present")

        data_pos = 0
        for cluster in cluster_chain:
            offset = self._cluster_to_offset(cluster)
            chunk_size = min(len(data) - data_pos, self.allocation_unit_size)
            chunk = data[data_pos : data_pos + chunk_size]
            self._write_bytes(offset, chunk)
            data_pos += chunk_size
            if data_pos >= len(data):
                break

    def _write_fat_sectors(self) -> None:
        """
        Writes the cached FAT to all FAT copies on the disk.

        Raises:
            IOError: If FAT write fails.
        """
        if not self._init_completed or not self.boot_sector:
            self.logger.error("Cannot write FAT sectors: Filesystem not initialized.")
            return
        if self.fat_cache is None or not self.fat_dirty:
            return
        try:
            self.logger.info(
                f"Writing {self.boot_sector.num_fats} copies of FAT cache..."
            )
            for i in range(self.boot_sector.num_fats):
                offset = self.fat_start_offset + (i * self.fat_size_bytes)
                self._write_bytes(offset, self.fat_cache)
            self.fat_dirty = False
            self.logger.info("Successfully wrote FAT sectors.")
        except OSError as e:
            raise OSError("Failed to write FAT cache to disk") from e
