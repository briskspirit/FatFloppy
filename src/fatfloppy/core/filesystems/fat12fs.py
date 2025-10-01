# src/fatfloppy/core/filesystems/fat12fs.py
"""
This module provides the filesystem implementation for the FAT12 (File Allocation
Table) system, commonly used on floppy disks. It handles the interpretation of
disk structures like the Boot Sector (BPB), parsing the FAT, and managing file
I/O operations such as reading, writing, creating, and deleting files and
directories.

Classes:
    FATVolumeInfo: A dataclass representing the FAT Boot Sector / BPB.
    FATFilesystem: The main class that implements the FAT12 filesystem logic.
"""
import re
import struct
import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any, ClassVar

from .fs_base import Filesystem, FileInfo
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger
from ..disk import Disk
from ..drivers.base_driver import DiskIODriver
from ..physical_format import PhysicalFormat


@dataclass
class FATVolumeInfo:
    """
    Represents the FAT Volume Information from the Boot Sector.

    This dataclass holds the BIOS Parameter Block (BPB) values that define the
    geometry and layout of the FAT filesystem on the disk.
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
    def from_bytes(cls, data: bytes) -> 'FATVolumeInfo':
        """
        Populates a FATVolumeInfo instance by parsing a boot sector byte array.

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
            instance.oem_id = data[3:11].decode('cp437', errors='replace').strip()
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
            instance.total_sectors = total_sectors_32 if total_sectors_16 == 0 else total_sectors_16
            instance.drive_number = data[0x024]
            # Check for Extended Boot Record signature
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
            return False  # Avoid division by zero
        root_dir_bytes = self.root_entries * 32
        if root_dir_bytes % self.bytes_per_sector != 0:
            return False
        root_dir_sectors = root_dir_bytes // self.bytes_per_sector
        first_data_sector = self.reserved_sectors + (self.num_fats * self.sectors_per_fat) + root_dir_sectors
        if first_data_sector >= self.total_sectors:
            return False
        return True

    def to_bytes(self) -> bytes:
        """
        Serializes the FATVolumeInfo instance into a boot sector byte array.

        Returns:
            A byte array representing the boot sector.
        """
        boot_sector = bytearray(self.bytes_per_sector)
        boot_sector[0:3] = b'\xEB\xFE\x90'  # JMP instruction
        boot_sector[3:11] = self.oem_id.encode('cp437').ljust(8)
        struct.pack_into('<H', boot_sector, 0x00B, self.bytes_per_sector)
        struct.pack_into('<B', boot_sector, 0x00D, self.sectors_per_cluster)
        struct.pack_into('<H', boot_sector, 0x00E, self.reserved_sectors)
        struct.pack_into('<B', boot_sector, 0x010, self.num_fats)
        struct.pack_into('<H', boot_sector, 0x011, self.root_entries)
        if self.total_sectors < 65536:
            struct.pack_into('<H', boot_sector, 0x013, self.total_sectors)
            struct.pack_into('<I', boot_sector, 0x020, 0)
        else:
            struct.pack_into('<H', boot_sector, 0x013, 0)
            struct.pack_into('<I', boot_sector, 0x020, self.total_sectors)
        struct.pack_into('<B', boot_sector, 0x015, self.media_descriptor)
        struct.pack_into('<H', boot_sector, 0x016, self.sectors_per_fat)
        struct.pack_into('<H', boot_sector, 0x018, self.sectors_per_track)
        struct.pack_into('<H', boot_sector, 0x01A, self.num_heads)
        struct.pack_into('<I', boot_sector, 0x01C, self.hidden_sectors)
        struct.pack_into('<B', boot_sector, 0x024, self.drive_number)
        struct.pack_into('<B', boot_sector, 0x025, 0)  # Reserved
        struct.pack_into('<B', boot_sector, 0x026, 0x29)  # Extended Boot Sig
        struct.pack_into('<I', boot_sector, 0x027, self.volume_serial)
        boot_sector[0x02B:0x036] = self.volume_label.encode('cp437').ljust(11)
        boot_sector[0x036:0x03E] = self.fs_type.encode('cp437').ljust(8)
        struct.pack_into('<H', boot_sector, self.bytes_per_sector - 2, 0xAA55)
        return bytes(boot_sector)


# --- Constants ---
logger = get_logger("FATFilesystem")

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


class FATFilesystem(Filesystem):
    """
    Provides an interface to a FAT12 filesystem on a disk image.

    This class handles filesystem detection, metadata parsing (BPB), FAT
    manipulation, and file operations like reading, writing, deleting,
    and listing files and directories.
    """
    # Plugin metadata
    filesystem_type: ClassVar[str] = "FAT12"
    filesystem_aliases: ClassVar[List[str]] = ["FAT", "MSDOS"]
    validity_threshold: ClassVar[int] = 40  # Score above which the filesystem is considered usable
    VALIDITY_THRESHOLD = validity_threshold  # For backward compatibility with tests

    def __init__(self, disk: Disk):
        """
        Initializes the FATFilesystem instance.

        Initialization is deferred until the first operation or validity check
        to avoid raising errors on disks with other formats.

        Args:
            disk: The Disk object to operate on.
        """
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self._init_completed = False
        self.boot_sector: Optional[FATVolumeInfo] = None
        self._cached_allocated_clusters: Optional[List[int]] = None
        self.fat_cache: Optional[bytearray] = None
        self.fat_dirty = False
        self._cached_validity_score: Optional[int] = None

        self._try_initialize()

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        if not isinstance(config1, FATVolumeInfo) or not isinstance(config2, FATVolumeInfo):
            return False
        # For FAT, matching total sectors is a strong indicator.
        return config1.total_sectors == config2.total_sectors

    @staticmethod
    def create_config_from_params(format_info: Dict[str, Any],
                                   physical_format: PhysicalFormat) -> Optional[FATVolumeInfo]:
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
            root_entries = format_info.get("root_entries", 224 if total_sectors > 1440 else 112)

            if bytes_per_sector == 0:
                logger.error("Bytes per sector cannot be zero")
                return None

            root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
            available_for_fats_and_data = total_sectors - (reserved_sectors + root_dir_sectors)

            if available_for_fats_and_data < 0:
                logger.error("Not enough space for reserved and root directory sectors")
                return None

            sectors_per_fat = 1
            for _attempt in range(available_for_fats_and_data // (num_fats if num_fats > 0 else 1) + 1):
                if num_fats == 0:
                    data_sectors = available_for_fats_and_data
                else:
                    data_sectors = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)

                if data_sectors < sectors_per_cluster:
                    break

                num_clusters_current_try = data_sectors // sectors_per_cluster
                if num_clusters_current_try <= 0:
                    break

                fat_bytes_needed = ((num_clusters_current_try + 2) * 3 + 1) // 2
                spf_needed = (fat_bytes_needed + bytes_per_sector - 1) // bytes_per_sector

                if spf_needed <= sectors_per_fat:
                    break
                sectors_per_fat = spf_needed
            else:
                logger.error("Could not determine consistent sectors_per_fat")
                return None

            if num_fats > 0:
                data_s = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)
            else:
                data_s = available_for_fats_and_data

            if data_s < sectors_per_cluster:
                logger.error(f"Data sectors ({data_s}) < sectors_per_cluster ({sectors_per_cluster})")
                return None

            final_num_clusters = data_s // sectors_per_cluster
            if final_num_clusters <= 0:
                logger.error(f"Non-positive cluster count: {final_num_clusters}")
                return None

            if final_num_clusters > FAT12_MAX_CLUSTERS:
                logger.warning(f"Cluster count ({final_num_clusters}) exceeds FAT12 limit")

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

    # ##################################################################
    # #                        PUBLIC API METHODS                      ##
    # ##################################################################

    def get_volume_label(self) -> Optional[str]:
        """
        Returns the FAT volume label from the boot sector.

        Returns:
            The volume label as a string, or None if not available.
        """
        if self.boot_sector and self.boot_sector.volume_label:
            return self.boot_sector.volume_label.strip()
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
            raise IOError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Creating directory: {path}")
        parent_path, dir_name = self._split_path(path)

        if not self._is_valid_83_filename(dir_name, allow_extension=False):
            raise ValueError(f"Invalid 8.3 directory name: '{dir_name}'")

        parent_dir_cluster = self._get_directory_cluster(parent_path)

        # Check if an entry with the same name already exists
        try:
            existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, dir_name)
            if existing_entry:
                if existing_entry.is_dir:
                    self.logger.warning(f"Directory '{path}' already exists.")
                    return
                raise FileExistsError(f"A file with the name '{dir_name}' already exists in '{parent_path}'")
        except FileNotFoundError:
            pass  # Name is available, proceed.

        # Allocate a new cluster for the directory's data
        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            raise IOError("No free clusters available to create directory")
        self._set_fat_entry_cached(new_cluster, FAT12_EOC)
        cluster_offset = self._cluster_to_offset(new_cluster)

        try:
            # Initialize the new cluster with '.' and '..' entries
            self._write_bytes(cluster_offset, b"\x00" * self.allocation_unit_size)
            now = datetime.datetime.now()
            dot_entry = self._create_directory_entry_bytes(
                name=".", is_dir=True, starting_cluster=new_cluster, size=0, dt=now)
            dotdot_entry = self._create_directory_entry_bytes(
                name="..", is_dir=True,
                starting_cluster=parent_dir_cluster if parent_dir_cluster > 0 else 0,
                size=0, dt=now)
            self._write_bytes(cluster_offset, dot_entry)
            self._write_bytes(cluster_offset + 32, dotdot_entry)
        except Exception as write_err:
            self.logger.error(f"Error writing '.'/'..' entries, freeing allocated cluster: {write_err}")
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise IOError("Failed to initialize new directory cluster") from write_err

        # Find a free slot in the parent directory to create the new entry
        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            self.logger.error(f"No space in parent directory '{parent_path}' to create entry for '{dir_name}'")
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise IOError(f"No space in parent directory {parent_path}")

        # Write the new directory entry in the parent directory
        dir_entry_bytes = self._create_directory_entry_bytes(
            name=dir_name, is_dir=True, starting_cluster=new_cluster, size=0, dt=now)
        try:
            dir_cluster_num, entry_offset_in_cluster = entry_location
            entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)
            self._write_bytes(entry_disk_offset, dir_entry_bytes)
        except Exception as parent_write_err:
            self.logger.error(f"Error writing parent directory entry, freeing allocated cluster: {parent_write_err}")
            self._set_fat_entry_cached(new_cluster, 0)
            raise IOError("Failed to write directory entry in parent") from parent_write_err

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
            raise IOError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        if path == "/":
            raise ValueError("Cannot delete root directory")

        self.logger.debug(f"Deleting: {path}")
        parent_path, name = self._split_path(path)
        parent_dir_cluster = self._get_directory_cluster(parent_path)
        try:
            entry_to_delete, entry_location = self._find_entry_in_directory(parent_dir_cluster, name)
        except FileNotFoundError:
            self.logger.warning(f"Item '{path}' not found for deletion.")
            return

        # If it's a directory, ensure it's empty
        if entry_to_delete.is_dir:
            try:
                dir_contents = self._list_directory_by_cluster(entry_to_delete.starting_cluster)
                non_dot_entries = [e for e in dir_contents if e.name not in [".", ".."]]
                if non_dot_entries:
                    raise OSError(f"Directory not empty: {path}")
            except Exception as list_err:
                self.logger.error(f"Could not verify if directory '{path}' is empty due to error: {list_err}")
                raise OSError(f"Could not verify directory contents before deleting: {path}") from list_err

        # Mark the directory entry as deleted
        try:
            dir_cluster_num, entry_offset_in_cluster = entry_location
            entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)
            self._write_bytes(entry_disk_offset, bytes([ENTRY_DELETED]))
        except Exception as write_err:
            self.logger.error(f"Failed to mark entry deleted for '{path}': {write_err}")
            raise IOError(f"Failed to update directory entry for deletion: {path}") from write_err

        # Free the associated cluster chain
        if entry_to_delete.starting_cluster >= 2:
            self.logger.debug(f"Freeing cluster chain starting at {entry_to_delete.starting_cluster}")
            self._free_cluster_chain(entry_to_delete.starting_cluster)

        self._commit_fat()
        self.disk.flush()
        self._cached_allocated_clusters = None  # Invalidate cache
        self.logger.info(f"Successfully deleted '{path}'")

    def delete_recursive(self, path: str) -> bool:
        """
        Recursively deletes a directory and all its contents.

        Args:
            path: The full path of the directory to delete.

        Returns:
            True if successful, False otherwise.
        """
        try:
            entry_info = self._find_path(path)
            if entry_info.is_dir:
                contents = self.list_directory(path)
                for item in contents:
                    if item.name not in [".", ".."]:
                        item_path = f"{path}/{item.name}" if path != "/" else f"/{item.name}"
                        if not self.delete_recursive(item_path):
                            return False
                self.delete(path)
            else:
                self.delete(path)
            return True
        except Exception as e:
            self.logger.error(f"Error deleting {path}: {e}")
            return False

    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None:
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
        if not profile.filesystem_config or not isinstance(profile.filesystem_config, FATVolumeInfo):
            raise ValueError("Invalid or missing FATVolumeInfo in FormatProfile for FAT formatting")
        self.logger.info(f"Formatting disk with FAT12 profile: {profile.name}")

        boot_sector_config = profile.filesystem_config
        if volume_label:
            boot_sector_config.volume_label = volume_label.ljust(11)[:11]
        elif not boot_sector_config.volume_label or not boot_sector_config.volume_label.strip():
            boot_sector_config.volume_label = "NO NAME".ljust(11)

        # Write Boot Sector
        boot_sector_bytes = boot_sector_config.to_bytes()
        self.disk.write_sector(0, 0, 1, boot_sector_bytes)
        self.logger.info("Wrote boot sector")

        # Initialize and Write FATs
        fat_size_bytes = boot_sector_config.sectors_per_fat * boot_sector_config.bytes_per_sector
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

        # Clear Root Directory
        root_dir_bytes = boot_sector_config.root_entries * 32
        root_dir_sectors = (root_dir_bytes + boot_sector_config.bytes_per_sector - 1) // boot_sector_config.bytes_per_sector
        root_dir_data = bytearray(root_dir_sectors * boot_sector_config.bytes_per_sector)
        root_dir_start_lba = fat_start_lba + (boot_sector_config.num_fats * boot_sector_config.sectors_per_fat)
        c, h, s = self.disk.physical_format.lba_to_chs(root_dir_start_lba)
        self.disk.write_sectors(c, h, s, root_dir_data)
        self.logger.info(f"Wrote root directory ({root_dir_sectors} sectors)")

        # Re-initialize the filesystem state
        self.boot_sector = FATVolumeInfo.from_bytes(boot_sector_bytes)
        self.fat_cache = bytearray(fat_data)
        self.fat_dirty = False
        self._cached_allocated_clusters = []
        self._initialize_filesystem_parameters()

    def get_allocated_units(self) -> List[int]:
        """
        Returns a sorted list of all allocated cluster numbers.

        Returns:
            A list of integers representing the used cluster numbers.
        """
        if self.get_validity_score() < self.validity_threshold or self.fat_cache is None:
            self.logger.warning("Cannot get allocated units: Filesystem invalid or FAT cache not loaded.")
            return []
        if self._cached_allocated_clusters is not None and not self.fat_dirty:
            return self._cached_allocated_clusters

        allocated_clusters = [
            cluster for cluster in range(2, self.num_clusters + 2)
            if self._read_fat_entry_cached(cluster, load_if_missing=False) != 0
        ]
        self._cached_allocated_clusters = allocated_clusters
        self.logger.debug(f"Retrieved {len(allocated_clusters)} allocated clusters")
        return allocated_clusters

    def get_display_info(self) -> Dict[str, str]:
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
            "Volume Label": bs.volume_label.strip() if bs.volume_label else "(No Label)",
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
            "Volume Serial": f"0x{bs.volume_serial:08X}" if bs.volume_serial else "N/A",
            "Drive Number": f"0x{bs.drive_number:02X}" if bs.drive_number else "N/A"
        }

    def get_disk_map_layout(self) -> Dict[str, Any]:
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
        root_dir_sectors = (bs.root_entries * 32 + bs.bytes_per_sector - 1) // bs.bytes_per_sector
        first_data_sector = reserved + (bs.sectors_per_fat * bs.num_fats) + root_dir_sectors

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
            "Boot Sector": "#FF0000", "FAT1": "#00FF00", "FAT2": "#0000FF",
            "Root Directory": "#FFFF00", "Used Data Sector": "#FF00FF",
            "Free Data Sector": "#808080",
        }
        legend = [("Boot Sector", legend_colors["Boot Sector"]), ("FAT1", legend_colors["FAT1"])]
        if bs.num_fats > 1:
            legend.append(("FAT2", legend_colors["FAT2"]))
        legend.extend([
            ("Root Directory", legend_colors["Root Directory"]),
            ("Used Data Sector", legend_colors["Used Data Sector"]),
            ("Free Data Sector", legend_colors["Free Data Sector"])
        ])

        type_map = {
            "boot": legend_colors["Boot Sector"], "fat1": legend_colors["FAT1"],
            "fat2": legend_colors["FAT2"] if bs.num_fats > 1 else "#D3D3D3",
            "root": legend_colors["Root Directory"], "data_used": legend_colors["Used Data Sector"],
            "data_free": legend_colors["Free Data Sector"], "unknown": "#008B8B"
        }

        return {
            'legend': legend,
            'get_sector_type': get_fat_sector_type,
            'allocation_unit_size_sectors': bs.sectors_per_cluster,
            'first_data_sector': first_data_sector,
            'type_color_map': type_map
        }

    def get_free_space(self) -> Tuple[int, int]:
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
            f"Total data space: {total_data_bytes} bytes ({self.num_clusters} clusters)")
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

        A score is calculated based on the boot sector signature, plausibility of
        BPB values, FAT media descriptor, and root directory entry validity.

        Returns:
            An integer score from 0 to 100.
        """
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        score = 0
        try:
            boot_sector_data = self.disk.read_sector(0, 0, 1)
            if not boot_sector_data:
                return 0

            if len(boot_sector_data) >= 512 and boot_sector_data[510:512] == b'\x55\xAA':
                score += 25
            if boot_sector_data[0] in (0xEB, 0xE9):  # JMP instruction
                score += 5

            # Attempt to parse BPB from disk
            parsed_bpb = None
            try:
                parsed_bpb = FATVolumeInfo.from_bytes(boot_sector_data)
            except ValueError:
                pass  # Ignore parse errors for scoring

            if parsed_bpb and parsed_bpb.is_valid():
                score += 50
                self.boot_sector = parsed_bpb
                self._try_initialize()
            else:
                # Fallback: Check for a profile-associated BPB
                if (self.disk and self.disk.physical_format and
                        hasattr(self.disk.physical_format, '_associated_filesystem_config')):
                    fs_config = getattr(self.disk.physical_format, '_associated_filesystem_config')
                    if isinstance(fs_config, FATVolumeInfo):
                        self.logger.debug("No valid BPB on disk, validating against profile BPB.")
                        score += 10
                        self.boot_sector = fs_config
                        self._try_initialize()

            # Further checks if initialization was successful
            if self._init_completed and self.boot_sector:
                # Check FAT media descriptor
                if self.fat_cache and self.fat_cache[0] == self.boot_sector.media_descriptor:
                    score += 40

                # Analyze root directory for filename validity
                try:
                    root_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
                    parsed, valid = 0, 0
                    for i in range(0, len(root_data), 32):
                        entry = root_data[i:i + 32]
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
                    self.logger.debug(f"Could not analyze root directory for scoring: {e}")

        except Exception as e:
            self.logger.debug(f"Could not read boot sector for scoring: {e}")
            return 0

        final_score = min(score, 100)
        self.logger.info(f"FAT validation score: {final_score}")
        self._cached_validity_score = final_score
        return final_score

    def list_directory(self, path: str = "/") -> List[FileInfo]:
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
            raise IOError("Filesystem is not valid or not recognized as FAT.")
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

        # Filter out '.', '..', Volume ID, and LFN entries from the final listing
        return [entry for entry in results if entry.name not in [".", ".."] and entry.attributes != "VOL"]

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
            raise IOError("Filesystem is not valid or not recognized as FAT.")
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
                f"Could not get cluster chain for file {path} starting at {file_entry_info.starting_cluster}")
            return b""

        file_data = self._read_cluster_chain_data(cluster_chain)
        return file_data[:file_entry_info.size]

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
            raise IOError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Writing file: {path}, size: {len(data)} bytes")

        parent_path, file_name = self._split_path(path)
        if not self._is_valid_83_filename(file_name):
            raise ValueError(f"Invalid 8.3 filename: '{file_name}'")

        parent_dir_cluster = self._get_directory_cluster(parent_path)

        # If file exists, delete it first
        try:
            existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, file_name)
            if existing_entry:
                if existing_entry.is_dir:
                    raise IsADirectoryError(f"Cannot overwrite directory with a file: {path}")
                self.logger.debug(f"File '{file_name}' exists, deleting before overwrite.")
                self.delete(path)
        except FileNotFoundError:
            pass  # File does not exist, which is fine.

        # Allocate clusters for the new file data
        num_clusters_needed = (len(data) + self.allocation_unit_size - 1) // self.allocation_unit_size if data else 0
        start_cluster = 0
        if num_clusters_needed > 0:
            clusters = self._allocate_cluster_chain(num_clusters_needed)
            if not clusters:
                raise IOError("Not enough free space to write file")
            start_cluster = clusters[0]
            self._write_cluster_chain_data(clusters, data)

        # Create a new directory entry for the file
        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            if start_cluster > 0:
                self._free_cluster_chain(start_cluster)  # Rollback cluster allocation
                self._commit_fat()
            raise IOError(f"No space in directory {parent_path}")

        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)
        entry_bytes = self._create_directory_entry_bytes(
            name=file_name, is_dir=False, starting_cluster=start_cluster,
            size=len(data), dt=datetime.datetime.now()
        )
        self._write_bytes(entry_disk_offset, entry_bytes)

        self._commit_fat()
        self.disk.flush()

    # ##################################################################
    # #                      PRIVATE HELPER METHODS                    ##
    # ##################################################################

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[List[int]]:
        """Allocates a chain of free clusters in the FAT."""
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
                    raise IOError(f"Could not allocate {num_clusters} clusters, only found {len(allocated_clusters)}.")
                # Mark as EOC immediately, will be overwritten if not the last
                self._set_fat_entry_cached(free_cluster, FAT12_EOC)
                allocated_clusters.append(free_cluster)
                if last_allocated is not None:
                    # Link the previous cluster to the new one
                    self._set_fat_entry_cached(last_allocated, free_cluster)
                last_allocated = free_cluster

            self.logger.debug(f"Successfully allocated cluster chain: {allocated_clusters}")
            return allocated_clusters
        except Exception as e:
            self.logger.error(f"Error during cluster allocation: {e}. Rolling back.")
            # Rollback: Free any clusters that were allocated
            if allocated_clusters:
                self._free_cluster_chain(allocated_clusters[0])
            return None

    def _check_and_adjust_geometry(self, driver: DiskIODriver, explicit_format_set: bool) -> None:
        """
        DEPRECATED/UNUSED: Adjusts disk geometry based on driver or boot sector.
        Note: This logic is complex and its usage has been removed. Retained
              for historical purposes but is not actively called.
        """
        bs = self.boot_sector
        # Check against driver's physical format if available
        if hasattr(driver, 'physical_format') and driver.physical_format:
            actual_spt = driver.physical_format.track_formats[0].sectors_per_track
            if actual_spt != self.disk.physical_format.track_formats[0].sectors_per_track:
                self.logger.debug(f"Adjusting geometry based on driver physical format (SPT: {actual_spt})")
                # (Complex geometry adjustment logic omitted for clarity)
        # Fallback to boot sector geometry if no explicit format was set
        elif (not explicit_format_set and bs and
              bs.sectors_per_track > 0 and bs.num_heads > 0):
            if (self.disk.physical_format.track_formats[0].sectors_per_track != bs.sectors_per_track or
                    self.disk.physical_format.heads != bs.num_heads):
                self.logger.debug(f"Adjusting geometry based on boot sector (H: {bs.num_heads}, SPT: {bs.sectors_per_track})")
                # (Complex geometry adjustment logic omitted for clarity)

    def _cluster_to_offset(self, cluster: int) -> int:
        """Converts a cluster number to its byte offset from the start of the disk."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if cluster < 2:
            raise ValueError(f"Invalid cluster number for offset calculation: {cluster}")
        return self.data_area_start_offset + (cluster - 2) * self.allocation_unit_size

    def _commit_fat(self) -> None:
        """Writes the dirty FAT cache to all FAT copies on the disk."""
        self._write_fat_sectors()

    def _create_directory_entry_bytes(
            self, name: str, is_dir: bool, starting_cluster: int,
            size: int, dt: datetime.datetime) -> bytes:
        """Creates a 32-byte directory entry from file metadata."""
        entry = bytearray(32)
        # Handle special '.' and '..' entries
        if name in [".", ".."]:
            fname_bytes = name.ljust(11).encode("cp437")
        else:
            fname_bytes = self._format_83_filename(name)
        entry[0:11] = fname_bytes

        entry[11] = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE
        # FAT date/time format
        time_val = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
        date_val = (((dt.year - 1980) & 0x7F) << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)
        struct.pack_into("<H", entry, 22, time_val)
        struct.pack_into("<H", entry, 24, date_val)
        struct.pack_into("<H", entry, 26, starting_cluster & 0xFFFF)
        struct.pack_into("<I", entry, 28, size if not is_dir else 0)
        return bytes(entry)

    def _find_entry_in_directory(
            self, dir_cluster: int, name_to_find: str) -> Tuple[FileInfo, Tuple[int, int]]:
        """
        Finds a directory entry by name within a given directory cluster.

        Returns:
            A tuple containing the FileInfo object and its location
            (cluster_num, offset_in_cluster).
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        name_upper = name_to_find.upper()

        def find_in_data(data: bytes, cluster_num: int) -> Optional[Tuple[FileInfo, Tuple[int, int]]]:
            for i in range(0, len(data), 32):
                entry_data = data[i:i + 32]
                if len(entry_data) < 32 or entry_data[0] == ENTRY_UNUSED:
                    break
                if entry_data[0] == ENTRY_DELETED:
                    continue
                entry = self._parse_single_directory_entry(entry_data)
                if entry and entry.name.upper() == name_upper:
                    return entry, (cluster_num, i)
            return None

        if dir_cluster == 0:  # Root directory
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            result = find_in_data(dir_data, 0)
            if result:
                return result
        elif dir_cluster >= 2:  # Subdirectory
            cluster_chain = self._get_cluster_chain(dir_cluster)
            for c in cluster_chain:
                cluster_data = self._read_bytes(self._cluster_to_offset(c), self.allocation_unit_size)
                result = find_in_data(cluster_data, c)
                if result:
                    return result

        raise FileNotFoundError(f"Entry '{name_to_find}' not found in directory cluster {dir_cluster}")

    def _find_free_cluster(self) -> Optional[int]:
        """Finds the first available (zero) cluster in the FAT."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Failed to load FAT cache for finding free cluster.")
            return None
        # Cluster numbers start at 2
        for cluster in range(2, self.num_clusters + 2):
            if self._read_fat_entry_cached(cluster, load_if_missing=False) == 0:
                return cluster
        self.logger.warning("No free clusters found on the disk.")
        return None

    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[Tuple[int, int]]:
        """
        Finds a free slot in a directory for a new entry.

        Returns:
            A tuple of (cluster_number, offset_in_cluster) or None if full.
        """
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")

        def find_in_data(data: bytes, cluster_num: int) -> Optional[Tuple[int, int]]:
            for i in range(0, len(data), 32):
                if data[i] in (ENTRY_UNUSED, ENTRY_DELETED):
                    return cluster_num, i
            return None

        if dir_cluster == 0:  # Root directory (fixed size)
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            found = find_in_data(dir_data, 0)
            if found:
                return found
            self.logger.warning("Root directory is full.")
            return None
        elif dir_cluster >= 2:  # Subdirectory (can be extended)
            cluster_chain = self._get_cluster_chain(dir_cluster)
            if not cluster_chain:
                self.logger.error(f"Cannot find free entry: Invalid cluster chain for directory {dir_cluster}")
                return None
            # Search existing clusters first
            for c in cluster_chain:
                cluster_data = self._read_bytes(self._cluster_to_offset(c), self.allocation_unit_size)
                found = find_in_data(cluster_data, c)
                if found:
                    return found
            # If no free entry, extend the directory by one cluster
            last_cluster = cluster_chain[-1]
            new_cluster = self._find_free_cluster()
            if new_cluster is None:
                self.logger.warning(f"Subdirectory in cluster {dir_cluster} is full and no free clusters to extend.")
                return None
            self._set_fat_entry_cached(last_cluster, new_cluster)
            self._set_fat_entry_cached(new_cluster, FAT12_EOC)
            self._write_bytes(self._cluster_to_offset(new_cluster), bytes(self.allocation_unit_size))
            return new_cluster, 0  # First entry in the new cluster

        raise ValueError(f"Invalid directory cluster specified: {dir_cluster}")

    def _find_path(self, path: str) -> Optional[FileInfo]:
        """Traverses a path to find the final file or directory."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        path = self._normalize_path(path)
        if path == "/":
            return FileInfo(name="/", size=0, is_dir=True, datetime=datetime.datetime(1980, 1, 1),
                            attributes="D", starting_cluster=0)

        parts = path.strip("/").split("/")
        current_cluster = 0  # Start from root
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
                    raise NotADirectoryError(f"Path component '{part_name}' is not a directory")
                current_entry_info = found_entry
                current_cluster = found_entry.starting_cluster
            else:
                raise FileNotFoundError(f"Path component '{part_name}' not found in '{path}'")

        return current_entry_info

    def _format_83_filename(self, name: str) -> bytes:
        """Formats a string into an 8.3 filename byte representation."""
        parts = name.upper().split(".", 1)
        base_name = parts[0]
        extension = parts[1] if len(parts) > 1 else ""
        formatted_name = base_name.ljust(8)[:8].encode("cp437", errors='replace')
        formatted_ext = extension.ljust(3)[:3].encode("cp437", errors='replace')
        return formatted_name + formatted_ext

    def _free_cluster_chain(self, start_cluster: int) -> None:
        """Frees an entire cluster chain in the FAT by setting entries to 0."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if start_cluster < 2:
            self.logger.warning(f"Invalid start cluster for freeing chain: {start_cluster}")
            return
        if self.fat_cache is None and not self._load_fat_cache():
            return

        current = start_cluster
        freed_count = 0
        for _ in range(self.num_clusters + 2):  # Safety break for loops
            if not (2 <= current < self.num_clusters + 2):
                self.logger.error(f"Invalid cluster {current} in chain from {start_cluster}.")
                break
            next_cluster = self._read_fat_entry_cached(current, load_if_missing=False)
            self._set_fat_entry_cached(current, 0)
            freed_count += 1
            if FAT12_EOC_MIN <= next_cluster <= FAT12_EOC or next_cluster == 0:
                break
            current = next_cluster
        self.logger.debug(f"Freed {freed_count} clusters in chain starting at {start_cluster}")

    def _get_cluster_chain(self, start_cluster: int) -> List[int]:
        """Reads the FAT to trace and return a list of clusters in a chain."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if not (2 <= start_cluster < self.num_clusters + 2):
            self.logger.warning(f"Invalid start cluster for chain retrieval: {start_cluster}")
            return []
        if self.fat_cache is None and not self._load_fat_cache():
            return []

        chain = []
        current = start_cluster
        for _ in range(self.num_clusters + 2):  # Safety break for loops
            if current in chain:
                self.logger.error(f"Loop detected in cluster chain at cluster {current}.")
                break
            chain.append(current)
            next_cluster = self._read_fat_entry_cached(current)
            if FAT12_EOC_MIN <= next_cluster <= FAT12_EOC:
                break
            if next_cluster in {0, FAT12_BAD_CLUSTER}:
                self.logger.error(f"Chain terminated unexpectedly at cluster {current} with value {next_cluster:03X}.")
                break
            current = next_cluster

        self.logger.debug(f"Retrieved cluster chain for {start_cluster}: {chain}")
        return chain

    def _get_directory_cluster(self, dir_path: str) -> int:
        """Returns the starting cluster number for a given directory path."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        dir_path = self._normalize_path(dir_path)
        if dir_path == "/":
            return 0  # 0 represents the root directory

        dir_entry_info = self._find_path(dir_path)
        if not dir_entry_info:
            raise FileNotFoundError(f"Directory not found: {dir_path}")
        if not dir_entry_info.is_dir:
            raise NotADirectoryError(f"Not a directory: {dir_path}")
        return dir_entry_info.starting_cluster

    def _get_offset_for_directory_entry(self, dir_cluster_num: int, entry_offset_in_cluster: int) -> int:
        """Calculates the absolute disk offset for a directory entry."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if dir_cluster_num == 0:  # Root directory
            return self.root_dir_start_offset + entry_offset_in_cluster
        if dir_cluster_num >= 2:  # Subdirectory
            return self._cluster_to_offset(dir_cluster_num) + entry_offset_in_cluster
        raise ValueError(f"Invalid cluster number for directory entry: {dir_cluster_num}")

    def _initialize_filesystem_parameters(self) -> None:
        """Calculates and sets key filesystem offsets and sizes from the BPB."""
        if not self.boot_sector or not self.boot_sector.is_valid():
            raise ValueError("Cannot initialize FAT parameters: Invalid Boot Sector / BPB")
        bpb = self.boot_sector
        if bpb.bytes_per_sector == 0 or bpb.sectors_per_cluster == 0:
            raise ValueError("BPB contains zero value for sector size or sectors per cluster.")

        self.allocation_unit_size = bpb.sectors_per_cluster * bpb.bytes_per_sector
        self.fat_start_offset = bpb.reserved_sectors * bpb.bytes_per_sector
        self.fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start_offset = self.fat_start_offset + (bpb.num_fats * self.fat_size_bytes)
        self.root_dir_bytes = bpb.root_entries * 32
        self.data_area_start_offset = self.root_dir_start_offset + self.root_dir_bytes
        first_data_sector = (self.data_area_start_offset + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        total_data_sectors = max(0, bpb.total_sectors - first_data_sector)
        self.num_clusters = total_data_sectors // bpb.sectors_per_cluster
        self._init_completed = True
        self.logger.debug("FAT filesystem parameters initialized.")

    _invalid_83_chars_pattern = re.compile(r'[\\/:*?"<>|\s+]')
    _reserved_names = {"CON", "PRN", "AUX", "NUL", "CLOCK$",
                      "COM1", "COM2", "COM3", "COM4",
                      "LPT1", "LPT2", "LPT3", "LPT4"}

    def _is_valid_83_filename(self, name: str, allow_dots: bool = True, allow_extension: bool = True) -> bool:
        """Checks if a name conforms to the 8.3 filename standard."""
        if not name or (name.endswith(".") and name not in [".", ".."]):
            return False
        if name in [".", ".."]:
            return allow_dots
        if self._invalid_83_chars_pattern.search(name) or any(0 < ord(c) < 32 for c in name):
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
        if base.startswith(".") or (ext and ext.startswith(".")):
            return False
        return True

    def _list_directory_by_cluster(self, cluster: int) -> List[FileInfo]:
        """Reads and parses all directory entries for a given cluster chain."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if cluster == 0:  # Root directory
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            return self._parse_directory_data(dir_data)
        if cluster >= 2:  # Subdirectory
            cluster_chain = self._get_cluster_chain(cluster)
            dir_data = self._read_cluster_chain_data(cluster_chain)
            return self._parse_directory_data(dir_data)
        return []

    def _load_boot_sector(self) -> None:
        """Reads sector 0 and attempts to parse it as a FAT boot sector."""
        try:
            boot_sector_data = self.disk.read_sector(0, 0, 1)
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
        """Loads the first FAT from the disk into the memory cache."""
        if self.fat_cache is not None:
            return True
        if not self._init_completed:
            return False
        try:
            self.logger.debug(f"Loading FAT cache from offset {self.fat_start_offset}, size {self.fat_size_bytes} bytes.")
            self.fat_cache = bytearray(self._read_bytes(self.fat_start_offset, self.fat_size_bytes))
            self.fat_dirty = False
            return True
        except IOError as e:
            self.logger.error(f"IOError loading FAT cache: {e}")
            self.fat_cache = None
            return False

    def _normalize_path(self, path: str) -> str:
        """Normalizes a path string to use forward slashes and removes duplicates."""
        path = path.replace("\\", "/").strip()
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        path = re.sub('/+', '/', path)
        return path if path else "/"

    def _parse_directory_data(self, dir_data: bytes) -> List[FileInfo]:
        """Parses a block of raw directory data into a list of FileInfo objects."""
        entries = []
        for i in range(0, len(dir_data), 32):
            entry_data = dir_data[i:i + 32]
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
        """Converts FAT's packed date and time format into a datetime object."""
        try:
            year = 1980 + ((date_val >> 9) & 0x7F)
            month = (date_val >> 5) & 0x0F
            day = date_val & 0x1F
            hour = (time_val >> 11) & 0x1F
            mins = (time_val >> 5) & 0x3F
            secs = (time_val & 0x1F) * 2
            return datetime.datetime(year, month, day, hour, mins, secs)
        except ValueError:
            return datetime.datetime(1980, 1, 1)  # Return default on error

    def _parse_single_directory_entry(self, entry_data: bytes) -> Optional[FileInfo]:
        """Parses a single 32-byte directory entry."""
        try:
            attributes = entry_data[11]
            if attributes & ATTR_LONG_NAME == ATTR_LONG_NAME:
                return None  # LFN entries are ignored
            if attributes & ATTR_VOLUME_ID:
                return FileInfo(name=entry_data[0:11].decode("cp437").strip(),
                                size=0, is_dir=False, datetime=datetime.datetime.min,
                                attributes="VOL", starting_cluster=0)

            raw_name = entry_data[0:8]
            if raw_name[0] == 0x05:  # Special case for E5 character
                raw_name = b'\xE5' + raw_name[1:]
            base_name = raw_name.decode("cp437").rstrip()
            extension = entry_data[8:11].decode("cp437").rstrip()
            full_name = f"{base_name}.{extension}" if extension else base_name

            if full_name not in [".", ".."] and not self._is_valid_83_filename(full_name, allow_dots=False):
                self.logger.warning(f"Skipping directory entry with invalid 8.3 name: {full_name!r}")
                return None

            is_dir = bool(attributes & ATTR_DIRECTORY)
            size = struct.unpack("<I", entry_data[28:32])[0]
            cluster = struct.unpack("<H", entry_data[26:28])[0]
            dt = self._parse_fat_datetime(struct.unpack("<H", entry_data[24:26])[0],
                                          struct.unpack("<H", entry_data[22:24])[0])

            attr_str = "".join([
                "R" if attributes & ATTR_READ_ONLY else "",
                "H" if attributes & ATTR_HIDDEN else "",
                "S" if attributes & ATTR_SYSTEM else "",
                "D" if is_dir else "",
                "A" if attributes & ATTR_ARCHIVE else "",
            ]) or "-"

            return FileInfo(name=full_name, size=size if not is_dir else 0,
                            is_dir=is_dir, datetime=dt, attributes=attr_str,
                            starting_cluster=cluster)
        except (UnicodeDecodeError, struct.error, IndexError) as e:
            self.logger.error(f"Failed to parse directory entry: {e}, data: {entry_data!r}")
            return None

    def _read_bytes(self, offset: int, length: int) -> bytes:
        """Reads a specific number of bytes from an absolute disk offset."""
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
                raise IOError(f"Short read: expected {end_offset_in_data} bytes in buffer, got {len(all_data)}")
            return all_data[start_offset_in_data:end_offset_in_data]
        except (ValueError, IOError) as e:
            raise IOError(f"Failed to read {length} bytes at offset {offset}: {e}") from e

    def _read_cluster_chain_data(self, cluster_chain: List[int]) -> bytes:
        """Reads the full data content of a file from its cluster chain."""
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
        """Reads a 12-bit FAT entry from the cache."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None:
            if load_if_missing and not self._load_fat_cache():
                raise IOError("Failed to load FAT cache for reading entry")
            elif self.fat_cache is None:
                raise ValueError("FAT cache unavailable")
        if not (0 <= cluster < self.num_clusters + 2):
            return FAT12_BAD_CLUSTER

        byte_offset = int(cluster * 1.5)
        if byte_offset + 1 >= len(self.fat_cache):
            return FAT12_BAD_CLUSTER

        value = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
        return (value & 0x0FFF) if (cluster % 2 == 0) else (value >> 4)

    def _set_fat_entry_cached(self, cluster: int, value: int) -> None:
        """Writes a 12-bit FAT entry to the cache."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and not self._load_fat_cache():
            raise IOError("Failed to load FAT cache for writing entry")
        if not (2 <= cluster < self.num_clusters + 2):
            raise ValueError(f"Invalid cluster number: {cluster}")

        byte_offset = int(cluster * 1.5)
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
            self._cached_allocated_clusters = None  # Invalidate cache on change

    def _split_path(self, path: str) -> Tuple[str, str]:
        """Splits a path into its parent directory path and the final component name."""
        path = self._normalize_path(path)
        if path == "/":
            return "/", ""
        last_slash = path.rfind("/")
        if last_slash == 0:
            return "/", path[1:]
        return path[:last_slash], path[last_slash + 1:]

    def _try_initialize(self) -> None:
        """
        Attempts to load the boot sector and initialize filesystem parameters.
        This method suppresses errors to allow for safe instantiation on non-FAT disks.
        """
        try:
            # Attempt to load from the disk itself first
            self._load_boot_sector()
            # Fallback to a profile-associated BPB if disk BPB is invalid
            if not self.boot_sector or not self.boot_sector.is_valid():
                if (self.disk and self.disk.physical_format and
                        hasattr(self.disk.physical_format, '_associated_filesystem_config')):
                    fs_config = getattr(self.disk.physical_format, '_associated_filesystem_config')
                    if isinstance(fs_config, FATVolumeInfo):
                        self.boot_sector = fs_config

            # Initialize if a valid BPB is now available
            if self.boot_sector and self.boot_sector.is_valid():
                self._initialize_filesystem_parameters()
                self._load_fat_cache()
                self._init_completed = True
        except (ValueError, IOError) as e:
            self.logger.debug(f"FAT Initialization failed during initial check: {e}")
            self._init_completed = False

    def _write_bytes(self, offset: int, data: bytes) -> None:
        """Writes a byte string to an absolute disk offset, handling sector alignment."""
        if not data:
            return
        if not self._init_completed or not self.boot_sector:
            raise ValueError("Filesystem not initialized for writing.")
        bps = self.boot_sector.bytes_per_sector
        start_lba = offset // bps
        offset_in_first_sector = offset % bps

        data_to_write = data
        current_lba = start_lba

        # Handle partial first sector
        if offset_in_first_sector != 0:
            c, h, s = self.disk.physical_format.lba_to_chs(start_lba)
            sector_data = bytearray(self.disk.read_sector(c, h, s))
            bytes_in_sector = min(len(data), bps - offset_in_first_sector)
            sector_data[offset_in_first_sector: offset_in_first_sector + bytes_in_sector] = data[:bytes_in_sector]
            self.disk.write_sector(c, h, s, bytes(sector_data))
            data_to_write = data[bytes_in_sector:]
            current_lba += 1

        # Handle full sectors
        num_full_sectors = len(data_to_write) // bps
        if num_full_sectors > 0:
            full_sectors_data = data_to_write[:num_full_sectors * bps]
            c, h, s = self.disk.physical_format.lba_to_chs(current_lba)
            self.disk.write_sectors(c, h, s, full_sectors_data)
            data_to_write = data_to_write[len(full_sectors_data):]
            current_lba += num_full_sectors

        # Handle partial last sector
        if data_to_write:
            c, h, s = self.disk.physical_format.lba_to_chs(current_lba)
            sector_data = bytearray(self.disk.read_sector(c, h, s))
            sector_data[:len(data_to_write)] = data_to_write
            self.disk.write_sector(c, h, s, bytes(sector_data))

    def _write_cluster_chain_data(self, cluster_chain: List[int], data: bytes) -> None:
        """Writes file data across a chain of clusters."""
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if not cluster_chain and data:
            raise ValueError("Cluster chain is empty but data is present")

        data_pos = 0
        for cluster in cluster_chain:
            offset = self._cluster_to_offset(cluster)
            chunk_size = min(len(data) - data_pos, self.allocation_unit_size)
            chunk = data[data_pos: data_pos + chunk_size]
            self._write_bytes(offset, chunk)
            data_pos += chunk_size
            if data_pos >= len(data):
                break

    def _write_fat_sectors(self) -> None:
        """Writes the cached FAT to all FAT copies on the disk."""
        if not self._init_completed or not self.boot_sector:
            self.logger.error("Cannot write FAT sectors: Filesystem not initialized.")
            return
        if self.fat_cache is None or not self.fat_dirty:
            return
        try:
            self.logger.info(f"Writing {self.boot_sector.num_fats} copies of FAT cache...")
            for i in range(self.boot_sector.num_fats):
                offset = self.fat_start_offset + (i * self.fat_size_bytes)
                self._write_bytes(offset, self.fat_cache)
            self.fat_dirty = False
            self.logger.info("Successfully wrote FAT sectors.")
        except IOError as e:
            raise IOError("Failed to write FAT cache to disk") from e
