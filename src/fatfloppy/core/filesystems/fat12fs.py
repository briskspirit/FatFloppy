# src/fatfloppy/core/filesystems/fat12fs.py
import re
import struct
import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any

from .fs_base import Filesystem, FileInfo
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger
from ..disk import Disk
from ..drivers.base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat


@dataclass
class FATVolumeInfo:
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
            instance.volume_serial = struct.unpack_from("<I", data, 0x027)[0]
            try:
                if data[0x26] == 0x29:
                    instance.volume_label = data[0x02B:0x02B + 11].decode("cp437").strip()
                    instance.fs_type = data[0x036:0x036 + 8].decode("cp437").strip()
                else:
                    instance.volume_label = ""
                    instance.fs_type = ""
            except (UnicodeDecodeError, IndexError):
                instance.volume_label = "INVALID"
                instance.fs_type = "INVALID"
        except (struct.error, IndexError) as e:
            cls.logger.error(f"BPB parsing error: {e}")
            instance.bytes_per_sector = 0
            instance.sectors_per_cluster = 0
            instance.total_sectors = 0
            instance.sectors_per_fat = 0
            instance.num_heads = 0
            instance.sectors_per_track = 0
            raise ValueError("Failed to parse BPB.") from e
        return instance

    def is_valid(self) -> bool:
        if self.bytes_per_sector not in [128, 256, 512, 1024, 2048, 4096]: return False
        if self.sectors_per_cluster not in [1, 2, 4, 8, 16, 32, 64, 128]: return False
        if (self.bytes_per_sector * self.sectors_per_cluster) > 65536: return False
        if self.reserved_sectors == 0: return False
        if self.num_fats not in [1, 2]: return False
        if self.sectors_per_fat == 0: return False
        if self.total_sectors == 0: return False
        root_dir_bytes = self.root_entries * 32
        if self.bytes_per_sector == 0: return False # Avoid division by zero
        if root_dir_bytes % self.bytes_per_sector != 0: return False
        root_dir_sectors = root_dir_bytes // self.bytes_per_sector
        first_data_sector = self.reserved_sectors + (self.num_fats * self.sectors_per_fat) + root_dir_sectors
        if first_data_sector >= self.total_sectors: return False
        return True

    def to_bytes(self) -> bytes:
        boot_sector = bytearray(self.bytes_per_sector)
        boot_sector[0:3] = b'\xEB\xFE\x90'
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
        struct.pack_into('<B', boot_sector, 0x025, 0)
        struct.pack_into('<B', boot_sector, 0x026, 0x29)
        struct.pack_into('<I', boot_sector, 0x027, self.volume_serial)
        boot_sector[0x02B:0x036] = self.volume_label.encode('cp437').ljust(11)
        boot_sector[0x036:0x03E] = self.fs_type.encode('cp437').ljust(8)
        struct.pack_into('<H', boot_sector, self.bytes_per_sector - 2, 0xAA55)
        return bytes(boot_sector)

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
    VALIDITY_THRESHOLD = 40  # Score above which the filesystem is considered usable

    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self._init_completed = False
        self.boot_sector: Optional[FATVolumeInfo] = None
        self._cached_allocated_clusters: Optional[List[int]] = None
        self.fat_cache: Optional[bytearray] = None
        self.fat_dirty = False
        self._cached_validity_score: Optional[int] = None

        # Initialization is now deferred to get_validity_score to avoid
        # raising errors on non-FAT disks.
        self._try_initialize()

    def _try_initialize(self):
        """
        Attempt to load boot sector and initialize parameters.
        Prioritizes on-disk BPB, but falls back to profile-associated BPB if available.
        """
        try:
            # First, attempt to load from the disk itself
            self._load_boot_sector()

            # If disk BPB is invalid or missing, try falling back to a profile
            if not self.boot_sector or not self.boot_sector.is_valid():
                if self.disk and self.disk.physical_format and hasattr(self.disk.physical_format, '_associated_filesystem_config'):
                    fs_config = getattr(self.disk.physical_format, '_associated_filesystem_config')
                    if isinstance(fs_config, FATVolumeInfo):
                        self.logger.debug("Using BPB from associated format profile for initialization.")
                        self.boot_sector = fs_config

            # Now, with either a disk BPB or a profile BPB, try to initialize
            if self.boot_sector and self.boot_sector.is_valid():
                self._initialize_filesystem_parameters()
                self._load_fat_cache()
                self._init_completed = True
                self.logger.debug("FAT Filesystem initialized successfully for subsequent operations.")
            else:
                self._init_completed = False
        except (ValueError, IOError) as e:
            self.logger.debug(f"FAT Initialization failed during initial check: {e}")
            self._init_completed = False
        except Exception as e:
            self.logger.debug(f"Unexpected FAT Initialization error: {e}", exc_info=False)
            self._init_completed = False

    def _check_and_adjust_geometry(self, driver: DiskIODriver, explicit_format_set: bool) -> None:
        bs = self.boot_sector
        if hasattr(driver, 'physical_format') and driver.physical_format:
            actual_sectors = driver.physical_format.track_formats[0].sectors_per_track
            if actual_sectors != self.disk.physical_format.track_formats[0].sectors_per_track:
                updated_track_format = TrackFormat(
                    track_start=0,
                    track_end=self.disk.physical_format.cylinders - 1,
                    head_start=0,
                    head_end=self.disk.physical_format.heads - 1,
                    sectors_per_track=actual_sectors,
                    encoding=self.disk.physical_format.track_formats[0].encoding,
                    rate=self.disk.physical_format.track_formats[0].rate,
                    gap3_bytes=self.disk.physical_format.track_formats[0].gap3_bytes,
                    interleave=self.disk.physical_format.track_formats[0].interleave
                )
                updated_geometry = PhysicalFormat(
                    cylinders=self.disk.physical_format.cylinders,
                    heads=self.disk.physical_format.heads,
                    rpm=self.disk.physical_format.rpm,
                    heads_inverted=self.disk.physical_format.heads_inverted,
                    bytes_per_sector=self.disk.physical_format.bytes_per_sector,
                    track_formats=[updated_track_format]
                )
                self.disk.set_geometry(updated_geometry)
                self.logger.debug("Adjusted geometry based on driver physical format")
        elif (not explicit_format_set and bs and
              hasattr(bs, 'sectors_per_track') and bs.sectors_per_track > 0 and
              hasattr(bs, 'num_heads') and bs.num_heads > 0):
            sectors_per_track = bs.sectors_per_track
            heads = bs.num_heads
            if (self.disk.physical_format.track_formats[0].sectors_per_track != sectors_per_track or
                    self.disk.physical_format.heads != heads):
                updated_track_format = TrackFormat(
                    track_start=0,
                    track_end=self.disk.physical_format.cylinders - 1,
                    head_start=0,
                    head_end=heads - 1,
                    sectors_per_track=sectors_per_track,
                    encoding=self.disk.physical_format.track_formats[0].encoding,
                    rate=self.disk.physical_format.track_formats[0].rate,
                    gap3_bytes=self.disk.physical_format.track_formats[0].gap3_bytes,
                    interleave=self.disk.physical_format.track_formats[0].interleave
                )
                updated_geometry = PhysicalFormat(
                    cylinders=self.disk.physical_format.cylinders,
                    heads=heads,
                    rpm=self.disk.physical_format.rpm,
                    heads_inverted=self.disk.physical_format.heads_inverted,
                    bytes_per_sector=self.disk.physical_format.bytes_per_sector,
                    track_formats=[updated_track_format]
                )
                self.disk.set_geometry(updated_geometry)
                self.logger.debug("Adjusted geometry based on boot sector")

    def get_specific_config(self) -> Optional[FATVolumeInfo]:
        return self.boot_sector

    def get_validity_score(self) -> int:
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        score = 0
        try:
            boot_sector_data = self.disk.read_sector(0, 0, 1)
            if not boot_sector_data:
                return 0

            if len(boot_sector_data) >= 512 and boot_sector_data[510:512] == b'\x55\xAA':
                score += 25
            if boot_sector_data[0] in (0xEB, 0xE9):
                score += 5

            parsed_bpb = None
            try:
                parsed_bpb = FATVolumeInfo.from_bytes(boot_sector_data)
            except (ValueError, struct.error):
                parsed_bpb = None

            if parsed_bpb and parsed_bpb.is_valid():
                score += 50  # High score for a valid, self-described format
                self.boot_sector = parsed_bpb
                self._try_initialize()
            else:
                # Fallback: No valid BPB on disk. Let's see if a profile was provided.
                profile_bpb = None
                if self.disk and self.disk.physical_format and hasattr(self.disk.physical_format, '_associated_filesystem_config'):
                    fs_config = getattr(self.disk.physical_format, '_associated_filesystem_config')
                    if isinstance(fs_config, FATVolumeInfo):
                        profile_bpb = fs_config

                if profile_bpb:
                    self.logger.debug("No valid BPB on disk, validating against profile BPB.")
                    score += 10 # Credit for having a profile to test against
                    self.boot_sector = profile_bpb
                    self._try_initialize() # Re-init with the profile's BPB
                else:
                    self.logger.debug("No valid BPB on disk and no profile BPB available.")
            
            # If we have a working BPB (from disk or profile), validate FAT and Dir
            if self._init_completed and self.boot_sector:
                # Check FAT media descriptor
                if self.fat_cache and len(self.fat_cache) > 0:
                    if self.fat_cache[0] == self.boot_sector.media_descriptor:
                        score += 40  # Very strong indicator
                
                # Analyze root directory for filename validity
                try:
                    root_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
                    parsed_entries, valid_name_entries = 0, 0
                    for i in range(0, len(root_data), 32):
                        entry_data = root_data[i:i + 32]
                        if len(entry_data) < 32 or entry_data[0] == ENTRY_UNUSED: break
                        if entry_data[0] == ENTRY_DELETED: continue
                        parsed_entries += 1
                        if self._parse_single_directory_entry(entry_data):
                            valid_name_entries += 1
                    
                    if parsed_entries > 0:
                        valid_name_ratio = valid_name_entries / parsed_entries
                        bonus = int(valid_name_ratio * 30)
                        score += bonus
                        self.logger.debug(f"Directory name score bonus: {bonus}")
                except Exception as dir_e:
                    self.logger.debug(f"Could not analyze root directory for scoring: {dir_e}")

        except Exception as e:
            self.logger.debug(f"Could not read boot sector for scoring: {e}")
            return 0
        
        final_score = min(score, 100)
        self.logger.info(f"FAT validation score: {final_score}")
        self._cached_validity_score = final_score
        return final_score

    def list_directory(self, path: str = "/") -> List[FileInfo]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
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
        return [
            entry for entry in results
            if entry.name not in [".", ".."]
            and not (entry.attributes == "VOL" or (entry.attributes == "LFN" if hasattr(entry, 'is_lfn') else False))
        ]

    def read_file(self, path: str) -> bytes:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
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
            self.logger.warning(f"Could not get cluster chain for file {path} starting at {file_entry_info.starting_cluster}")
            return b""
        file_data = self._read_cluster_chain_data(cluster_chain)
        return file_data[:file_entry_info.size]

    def write_file(self, path: str, data: bytes) -> None:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Writing file: {path}, size: {len(data)} bytes")
        parent_path, file_name = self._split_path(path)
        if not self._is_valid_83_filename(file_name):
            raise ValueError(f"Invalid 8.3 filename: '{file_name}'")
        parent_dir_cluster = self._get_directory_cluster(parent_path)
        try:
            existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, file_name)
            if existing_entry:
                if existing_entry.is_dir:
                    raise IsADirectoryError(f"Cannot overwrite directory with a file: {path}")
                self.logger.debug(f"File '{file_name}' exists, deleting before overwrite.")
                self.delete(path)
        except FileNotFoundError:
            pass
        num_clusters_needed = (len(data) + self.allocation_unit_size - 1) // self.allocation_unit_size if len(data) > 0 else 0
        start_cluster_for_entry = 0
        if num_clusters_needed > 0:
            clusters = self._allocate_cluster_chain(num_clusters_needed)
            if not clusters:
                raise IOError("Not enough free space")
            start_cluster_for_entry = clusters[0]
            self._write_cluster_chain_data(clusters, data)
        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            if num_clusters_needed > 0:
                self._free_cluster_chain(start_cluster_for_entry)
                self._commit_fat()
            raise IOError(f"No space in directory {parent_path}")
        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)
        now = datetime.datetime.now()
        entry_bytes = self._create_directory_entry_bytes(
            name=file_name, is_dir=False, starting_cluster=start_cluster_for_entry,
            size=len(data), dt=now
        )
        self._write_bytes(entry_disk_offset, entry_bytes)
        self._commit_fat()
        self.disk.flush()

    def create_directory(self, path: str) -> None:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as FAT.")
        path = self._normalize_path(path)
        self.logger.debug(f"Creating directory: {path}")
        parent_path, dir_name = self._split_path(path)

        if not self._is_valid_83_filename(dir_name, allow_extension=False):
            raise ValueError(f"Invalid 8.3 directory name: '{dir_name}'")

        parent_dir_cluster = self._get_directory_cluster(parent_path)

        try:
            existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, dir_name)
            if existing_entry:
                if existing_entry.is_dir:
                    self.logger.warning(f"Directory '{path}' already exists.")
                    return
                else:
                    raise FileExistsError(f"A file with the name '{dir_name}' already exists in '{parent_path}'")
        except FileNotFoundError:
            pass

        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            raise IOError("No free clusters available to create directory")
        self._set_fat_entry_cached(new_cluster, FAT12_EOC)
        cluster_offset = self._cluster_to_offset(new_cluster)
        try:
            self._write_bytes(cluster_offset, b"\x00" * self.allocation_unit_size)
            now = datetime.datetime.now()
            dot_entry = self._create_directory_entry_bytes(
                name=".", is_dir=True, starting_cluster=new_cluster, size=0, dt=now
            )
            dotdot_entry = self._create_directory_entry_bytes(
                name="..", is_dir=True,
                starting_cluster=parent_dir_cluster if parent_dir_cluster > 0 else 0,
                size=0, dt=now
            )
            self._write_bytes(cluster_offset, dot_entry)
            self._write_bytes(cluster_offset + 32, dotdot_entry)

        except Exception as write_err:
            self.logger.error(f"Error writing '.'/'..' entries, freeing allocated cluster: {write_err}")
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise IOError("Failed to initialize new directory cluster") from write_err

        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            self.logger.error(f"No space in parent directory '{parent_path}' to create entry for '{dir_name}'")
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise IOError(f"No space in parent directory {parent_path}")

        parent_entry_dir_cluster, parent_entry_offset_in_cluster = entry_location
        parent_entry_disk_offset = self._get_offset_for_directory_entry(
            parent_entry_dir_cluster, parent_entry_offset_in_cluster
        )

        dir_entry_bytes = self._create_directory_entry_bytes(
            name=dir_name, is_dir=True, starting_cluster=new_cluster, size=0, dt=now
        )
        try:
            self._write_bytes(parent_entry_disk_offset, dir_entry_bytes)
        except Exception as parent_write_err:
            self.logger.error(f"Error writing parent directory entry, freeing allocated cluster: {parent_write_err}")
            self._set_fat_entry_cached(new_cluster, 0)
            raise IOError("Failed to write directory entry in parent") from parent_write_err

        self._commit_fat()
        self.disk.flush()
        self.logger.info(f"Successfully created directory '{path}'")

    def delete(self, path: str) -> None:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
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

        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)
        if entry_to_delete.is_dir:
            try:
                dir_contents = self._list_directory_by_cluster(entry_to_delete.starting_cluster)
                non_dot_entries = [e for e in dir_contents if e.name not in [".", ".."]]
                if non_dot_entries:
                    raise OSError(f"Directory not empty: {path}")
            except Exception as list_err:
                 self.logger.error(f"Could not verify if directory '{path}' is empty due to error: {list_err}")
                 raise OSError(f"Could not verify directory contents before deleting: {path}") from list_err

        try:
            self._write_bytes(entry_disk_offset, bytes([ENTRY_DELETED]))
        except Exception as write_err:
            self.logger.error(f"Failed to mark entry deleted for '{path}': {write_err}")
            raise IOError(f"Failed to update directory entry for deletion: {path}") from write_err

        if entry_to_delete.starting_cluster >= 2:
            self.logger.debug(f"Freeing cluster chain starting at {entry_to_delete.starting_cluster}")
            self._free_cluster_chain(entry_to_delete.starting_cluster)

        self._commit_fat()
        self.disk.flush()
        self._cached_allocated_clusters = None
        self.logger.info(f"Successfully deleted '{path}'")

    def delete_recursive(self, path: str) -> bool:
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

    def get_allocated_units(self) -> List[int]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD or self.fat_cache is None:
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

    def get_free_space(self) -> Tuple[int, int]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return 0, 0
        total_data_bytes = self.num_clusters * self.allocation_unit_size
        allocated_count = len(self.get_allocated_units())
        free_clusters = max(self.num_clusters - allocated_count, 0)
        free_bytes = free_clusters * self.allocation_unit_size
        self.logger.debug(f"Free space: {free_bytes} bytes ({free_clusters} clusters), Total data space: {total_data_bytes} bytes ({self.num_clusters} clusters)")
        return free_bytes, total_data_bytes

    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None:
        if not isinstance(profile, FormatProfile):
            raise TypeError("profile must be a FormatProfile object")
        if not profile.filesystem_config or not isinstance(profile.filesystem_config, FATVolumeInfo):
            raise ValueError("Invalid or missing FATVolumeInfo in FormatProfile.filesystem_config for FAT formatting")
        self.logger.info(f"Formatting disk with FAT12 profile: {profile.name}")

        boot_sector = profile.filesystem_config
        if volume_label:
            boot_sector.volume_label = volume_label.ljust(11)[:11]
        elif not boot_sector.volume_label or not boot_sector.volume_label.strip():
            boot_sector.volume_label = "NO NAME".ljust(11)

        boot_sector_bytes = boot_sector.to_bytes()

        self.disk.write_sector(0, 0, 1, boot_sector_bytes)
        self.logger.info("Wrote boot sector")

        bytes_per_sector = boot_sector.bytes_per_sector
        sectors_per_fat = boot_sector.sectors_per_fat
        num_fats = boot_sector.num_fats
        root_entries = boot_sector.root_entries
        reserved_sectors = boot_sector.reserved_sectors
        media_descriptor = boot_sector.media_descriptor

        fat_size_bytes = sectors_per_fat * bytes_per_sector
        fat_data = bytearray(fat_size_bytes)
        fat_data[0] = media_descriptor
        fat_data[1] = 0xFF
        fat_data[2] = 0xFF
        for i in range(3, fat_size_bytes):
             fat_data[i] = 0x00
        fat_combined = fat_data * num_fats
        fat_start_lba = reserved_sectors
        c, h, s = self.disk.lba_to_chs(fat_start_lba)
        self.disk.write_sectors(c, h, s, fat_combined)
        self.logger.info(f"Wrote {num_fats} FATs ({sectors_per_fat} sectors each)")

        root_dir_bytes = root_entries * 32
        root_dir_sectors = (root_dir_bytes + bytes_per_sector - 1) // bytes_per_sector
        root_dir_data = bytearray(root_dir_sectors * bytes_per_sector)
        root_dir_start_lba = reserved_sectors + num_fats * sectors_per_fat
        c, h, s = self.disk.lba_to_chs(root_dir_start_lba)
        self.disk.write_sectors(c, h, s, root_dir_data)
        self.logger.info(f"Wrote root directory ({root_dir_sectors} sectors)")

        self.boot_sector = FATVolumeInfo.from_bytes(boot_sector_bytes)
        self.fat_cache = bytearray(fat_data)
        self.fat_dirty = False
        self._cached_allocated_clusters = []
        self._initialize_filesystem_parameters()

    def get_display_info(self) -> Dict[str, str]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD or not self.boot_sector:
            return {"Error": "FAT filesystem not valid or BPB missing"}

        bs = self.boot_sector
        info = {
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
        return info

    def get_disk_map_layout(self) -> Dict[str, Any]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD or not self.boot_sector: return {}
        bs = self.boot_sector
        reserved = bs.reserved_sectors
        fat_size_total = bs.sectors_per_fat * bs.num_fats
        fat1_end = reserved + bs.sectors_per_fat
        fat2_end = reserved + fat_size_total
        root_dir_sectors = (bs.root_entries * 32 + bs.bytes_per_sector - 1) // bs.bytes_per_sector
        first_data_sector = reserved + fat_size_total + root_dir_sectors

        def get_fat_sector_type(lba: int) -> str:
            if lba < reserved: return "boot"
            elif lba < fat1_end: return "fat1"
            elif bs.num_fats > 1 and lba < fat2_end: return "fat2"
            elif lba < first_data_sector: return "root"
            else:
                relative_lba = lba - first_data_sector
                if bs.sectors_per_cluster == 0:
                    return "data_free"
                cluster_index = relative_lba // bs.sectors_per_cluster
                fat_cluster_number = cluster_index + 2
                allocated_units = self.get_allocated_units()
                if fat_cluster_number in allocated_units:
                    return "data_used"
                else:
                    return "data_free"

        legend_colors = {
            "Boot Sector": "#FF0000",
            "FAT1": "#00FF00",
            "FAT2": "#0000FF",
            "Root Directory": "#FFFF00",
            "Used Data Sector": "#FF00FF",
            "Free Data Sector": "#808080",
        }
        legend = [("Boot Sector", legend_colors["Boot Sector"])]
        legend.append(("FAT1", legend_colors["FAT1"]))
        if bs.num_fats > 1:
            legend.append(("FAT2", legend_colors["FAT2"]))
        legend.append(("Root Directory", legend_colors["Root Directory"]))
        legend.append(("Used Data Sector", legend_colors["Used Data Sector"]))
        legend.append(("Free Data Sector", legend_colors["Free Data Sector"]))

        type_map = {
            "boot": legend_colors["Boot Sector"],
            "fat1": legend_colors["FAT1"],
            "fat2": legend_colors["FAT2"] if bs.num_fats > 1 else "#D3D3D3",
            "root": legend_colors["Root Directory"],
            "data_used": legend_colors["Used Data Sector"],
            "data_free": legend_colors["Free Data Sector"],
            "unknown": "#008B8B"
        }

        return {
            'legend': legend,
            'get_sector_type': get_fat_sector_type,
            'allocation_unit_size_sectors': bs.sectors_per_cluster,
            'first_data_sector': first_data_sector,
            'type_color_map': type_map
        }

    def _normalize_path(self, path: str) -> str:
        path = path.replace("\\", "/")
        if len(path) > 1:
            path = path.rstrip("/")
        if not path:
            path = "/"
        if not path.startswith("/"):
            path = "/" + path
        path = re.sub('/+', '/', path)
        return path

    def _split_path(self, path: str) -> Tuple[str, str]:
        path = self._normalize_path(path)
        if path == "/":
            return "/", ""
        last_slash_index = path.rfind("/")
        if last_slash_index == 0:
            parent_path = "/"
            name = path[1:]
        else:
            parent_path = path[:last_slash_index]
            name = path[last_slash_index + 1:]
        return parent_path, name

    def _load_boot_sector(self) -> None:
        try:
            boot_sector_data = self.disk.read_sector(0, 0, 1)
            if not boot_sector_data:
                self.boot_sector = None
                self.logger.error("Boot sector data is empty (read returned empty)")
                return
            self.boot_sector = FATVolumeInfo.from_bytes(boot_sector_data)
            self.logger.debug("Loaded boot sector")
        except Exception as e:
            self.logger.error(f"Error reading boot sector: {e}")
            self.boot_sector = None

    def _initialize_filesystem_parameters(self) -> None:
        if not self.boot_sector or not self.boot_sector.is_valid():
            raise ValueError("Cannot initialize FAT parameters: Invalid Boot Sector / BPB")
        bpb = self.boot_sector
        if bpb.bytes_per_sector == 0:
            raise ValueError("Bytes per sector is zero in BPB, cannot initialize parameters.")
        self.allocation_unit_size = bpb.sectors_per_cluster * bpb.bytes_per_sector
        vbr_lba = bpb.hidden_sectors if bpb.hidden_sectors < bpb.total_sectors else 0
        self.fat_start_offset = (vbr_lba + bpb.reserved_sectors) * bpb.bytes_per_sector
        self.fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start_offset = self.fat_start_offset + (bpb.num_fats * self.fat_size_bytes)
        self.root_dir_bytes = bpb.root_entries * 32
        self.root_dir_sectors = (self.root_dir_bytes + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        self.data_area_start_offset = self.root_dir_start_offset + self.root_dir_bytes
        first_data_sector_lba = (self.data_area_start_offset + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        if first_data_sector_lba >= (vbr_lba + bpb.total_sectors):
             raise ValueError(f"Calculated first data sector LBA ({first_data_sector_lba}) exceeds total sectors ({vbr_lba + bpb.total_sectors})")
        total_data_sectors = max(0, (vbr_lba + bpb.total_sectors) - first_data_sector_lba)
        if bpb.sectors_per_cluster == 0:
            raise ValueError("Sectors per cluster is zero in BPB, cannot calculate number of clusters.")
        self.num_clusters = total_data_sectors // bpb.sectors_per_cluster
        self._init_completed = True
        self.logger.debug("FAT filesystem parameters initialized:")
        self.logger.debug(f"  Cluster Size: {self.allocation_unit_size} bytes")
        self.logger.debug(f"  FAT Start Offset: {self.fat_start_offset}")
        self.logger.debug(f"  FAT Size: {self.fat_size_bytes} bytes")
        self.logger.debug(f"  Root Dir Start Offset: {self.root_dir_start_offset}")
        self.logger.debug(f"  Root Dir Size: {self.root_dir_bytes} bytes ({self.root_dir_sectors} sectors)")
        self.logger.debug(f"  Data Area Start Offset: {self.data_area_start_offset}")
        self.logger.debug(f"  Number of Clusters: {self.num_clusters}")

    def _cluster_to_offset(self, cluster: int) -> int:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if cluster < 2:
            raise ValueError(f"Invalid cluster number: {cluster}")
        return self.data_area_start_offset + (cluster - 2) * self.allocation_unit_size

    def _get_directory_cluster(self, dir_path: str) -> int:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        dir_path = self._normalize_path(dir_path)
        if dir_path == "/":
            return 0
        dir_entry_info = self._find_path(dir_path)
        if not dir_entry_info:
            raise FileNotFoundError(f"Directory not found: {dir_path}")
        if not dir_entry_info.is_dir:
            raise NotADirectoryError(f"Not a directory: {dir_path}")
        return dir_entry_info.starting_cluster

    def _list_directory_by_cluster(self, cluster: int) -> List[FileInfo]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        entries = []
        if cluster == 0:
            if self.root_dir_bytes == 0:
                return []
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            entries = self._parse_directory_data(dir_data)
        elif cluster >= 2:
            cluster_chain = self._get_cluster_chain(cluster)
            if not cluster_chain:
                self.logger.warning(f"Could not get cluster chain for directory cluster {cluster}")
                return []
            dir_data = self._read_cluster_chain_data(cluster_chain)
            entries = self._parse_directory_data(dir_data)
        self.logger.debug(f"Listed {len(entries)} entries in cluster {cluster}")
        return entries

    def _parse_directory_data(self, dir_data: bytes) -> List[FileInfo]:
        entries = []
        if not dir_data:
            return entries
        for i in range(0, len(dir_data), 32):
            entry_data = dir_data[i:i + 32]
            if len(entry_data) < 32:
                break
            first_byte = entry_data[0]
            if first_byte == ENTRY_UNUSED:
                break
            if first_byte == ENTRY_DELETED:
                continue
            entry = self._parse_single_directory_entry(entry_data)
            if entry:
                if entry.attributes != "LFN":
                    entries.append(entry)
        return entries

    def _parse_single_directory_entry(self, entry_data: bytes) -> Optional[FileInfo]:
        try:
            attributes = entry_data[11]
            if attributes & ATTR_LONG_NAME == ATTR_LONG_NAME:
                return None
            if attributes & ATTR_VOLUME_ID:
                try:
                    vol_name = entry_data[0:11].decode("cp437").strip()
                    return FileInfo(
                        name=vol_name, size=0, is_dir=False,
                        datetime=datetime.datetime.min,
                        attributes="VOL",
                        starting_cluster=0
                    )
                except UnicodeDecodeError:
                    self.logger.warning("Failed to decode volume label entry.")
                    return None
            raw_name = entry_data[0:8]
            raw_ext = entry_data[8:11]
            base_name = (b'\xE5' + raw_name[1:] if raw_name[0] == 0x05 else raw_name).decode("cp437").rstrip()
            extension = raw_ext.decode("cp437").rstrip()

            full_name = base_name if not extension else f"{base_name}.{extension}"
            if '\x00' in full_name:
                 self.logger.warning(f"Skipping entry with null char in name: {entry_data[:11]!r}")
                 return None
            if full_name not in [".", ".."] and not self._is_valid_83_filename(full_name, allow_dots=False):
                return None
            is_dir = bool(attributes & ATTR_DIRECTORY)
            size = struct.unpack("<I", entry_data[28:32])[0]
            starting_cluster = struct.unpack("<H", entry_data[26:28])[0]
            if is_dir and size != 0:
                size = 0
            if (size > 0 and starting_cluster < 2) or (is_dir and starting_cluster < 2 and full_name not in [".", ".."]):
                return None
            time_val = struct.unpack("<H", entry_data[22:24])[0]
            date_val = struct.unpack("<H", entry_data[24:26])[0]
            dt = self._parse_fat_datetime(date_val, time_val)
            attr_list = []
            if attributes & ATTR_READ_ONLY:
                attr_list.append("R")
            if attributes & ATTR_HIDDEN:
                attr_list.append("H")
            if attributes & ATTR_SYSTEM:
                attr_list.append("S")
            if attributes & ATTR_ARCHIVE:
                attr_list.append("A")
            if is_dir:
                attr_list.append("D")
            attr_str = "".join(attr_list) if attr_list else "-"
            return FileInfo(
                name=full_name, size=size, is_dir=is_dir,
                datetime=dt, attributes=attr_str, starting_cluster=starting_cluster
            )
        except (UnicodeDecodeError, struct.error, IndexError) as e:
            self.logger.error(f"Failed to parse directory entry: {e}, data: {entry_data!r}")
            return None

    def _parse_fat_datetime(self, date_val: int, time_val: int) -> datetime.datetime:
        try:
            secs = (time_val & 0x1F) * 2
            mins = (time_val >> 5) & 0x3F
            hour = (time_val >> 11) & 0x1F
            day = date_val & 0x1F
            month = (date_val >> 5) & 0x0F
            year = 1980 + ((date_val >> 9) & 0x7F)
            if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= mins <= 59 and 0 <= secs <= 59):
                raise ValueError("Invalid date/time")
            return datetime.datetime(year, month, day, hour, mins, secs)
        except ValueError as e:
            self.logger.warning(f"Invalid FAT date/time value (Date: {date_val}, Time: {time_val}): {e}. Using default.")
            return datetime.datetime(1980, 1, 1, 0, 0, 0)

    def _find_path(self, path: str) -> Optional[FileInfo]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        path = self._normalize_path(path)
        if path == "/":
            root_dt = datetime.datetime(1980, 1, 1)
            try:
                root_contents = self._list_directory_by_cluster(0)
                for item in root_contents:
                    if item.attributes == "VOL":
                        root_dt = item.datetime
                        break
            except Exception:
                pass
            return FileInfo(name="/", size=0, is_dir=True, datetime=root_dt, attributes="D", starting_cluster=0)
        parts = path.strip("/").split("/")
        current_cluster = 0
        current_entry_info = None
        for i, part_name in enumerate(parts):
            is_last_part = i == len(parts) - 1
            entries_in_current = self._list_directory_by_cluster(current_cluster)
            for entry in entries_in_current:
                if entry.name.upper() == part_name.upper():
                    if not is_last_part and not entry.is_dir:
                        raise NotADirectoryError(f"Path component '{part_name}' is not a directory")
                    current_entry_info = entry
                    current_cluster = entry.starting_cluster
                    break
            else:
                raise FileNotFoundError(f"Path not found: {path}")
        return current_entry_info

    def _load_fat_cache(self) -> bool:
        if self.fat_cache is not None:
            return True
        if not self._init_completed or not self.boot_sector:
            return False
        try:
            self.logger.debug(f"Loading FAT cache from offset {self.fat_start_offset}, size {self.fat_size_bytes} bytes.")
            self.fat_cache = bytearray(self._read_bytes(self.fat_start_offset, self.fat_size_bytes))
            self.fat_dirty = False
            if len(self.fat_cache) != self.fat_size_bytes:
                 self.logger.error(f"FAT cache read size mismatch: expected {self.fat_size_bytes}, got {len(self.fat_cache)}")
                 self.fat_cache = None
                 return False
            return True
        except IOError as e:
            self.logger.error(f"IOError loading FAT cache: {e}")
            self.fat_cache = None
            return False
        except Exception as e:
            self.logger.exception(f"Unexpected error loading FAT cache: {e}")
            self.fat_cache = None
            return False

    def _write_fat_sectors(self) -> None:
        if not self._init_completed or not self.boot_sector:
             self.logger.error("Cannot write FAT sectors: Filesystem not initialized or no boot sector.")
             return
        if self.fat_cache is None:
            self.logger.error("Cannot write FAT sectors: FAT cache is not loaded.")
            return
        if not self.fat_dirty:
            self.logger.debug("FAT cache not dirty, skipping write.")
            return
        try:
            self.logger.info(f"Writing {self.boot_sector.num_fats} copies of FAT cache...")
            for fat_num in range(self.boot_sector.num_fats):
                fat_offset = self.fat_start_offset + (fat_num * self.fat_size_bytes)
                self.logger.debug(f"Writing FAT copy {fat_num + 1} to offset {fat_offset}")
                self._write_bytes(fat_offset, self.fat_cache)
            self.fat_dirty = False
            self._cached_allocated_clusters = None
            self.logger.info("Successfully wrote FAT sectors.")
        except IOError as e:
            self.logger.error(f"IOError writing FAT sectors: {e}")
            raise IOError("Failed to write FAT cache to disk") from e
        except Exception as e:
            self.logger.exception(f"Unexpected error writing FAT sectors: {e}")
            raise IOError("Failed to write FAT cache to disk") from e

    def _commit_fat(self) -> None:
        self._write_fat_sectors()

    def _read_fat_entry_cached(self, cluster: int, load_if_missing: bool = True) -> int:
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and load_if_missing:
            if not self._load_fat_cache():
                raise IOError("Failed to load FAT cache for reading entry")
        if self.fat_cache is None:
            raise ValueError("FAT cache unavailable")
        if cluster < 0:
             self.logger.warning(f"Attempted to read FAT entry for invalid cluster: {cluster}")
             return FAT12_BAD_CLUSTER
        if not (0 <= cluster < self.num_clusters + 2):
            self.logger.warning(f"Attempted to read FAT entry for cluster {cluster} outside valid range [0..{self.num_clusters + 1}]")
            return FAT12_BAD_CLUSTER
        byte_offset = int(cluster * 1.5)
        if byte_offset + 1 >= len(self.fat_cache):
            self.logger.error(f"Calculated FAT offset {byte_offset} out of bounds for cache size {len(self.fat_cache)} (cluster {cluster})")
            return FAT12_BAD_CLUSTER
        try:
            value = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
        except struct.error as e:
             self.logger.error(f"Struct error reading FAT entry at offset {byte_offset} for cluster {cluster}: {e}")
             return FAT12_BAD_CLUSTER
        return value & 0x0FFF if cluster % 2 == 0 else value >> 4

    def _set_fat_entry_cached(self, cluster: int, value: int) -> None:
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None:
            if not self._load_fat_cache():
                raise IOError("Failed to load FAT cache for writing entry")
        if self.fat_cache is None:
             raise ValueError("FAT cache unavailable")
        if not (2 <= cluster < self.num_clusters + 2):
            raise ValueError(f"Invalid cluster number for setting FAT entry: {cluster}. Must be between 2 and {self.num_clusters + 1}.")
        fat_value_to_set = value & 0x0FFF
        byte_offset = int(cluster * 1.5)
        if byte_offset + 1 >= len(self.fat_cache):
            raise IndexError(f"Calculated FAT offset {byte_offset} out of bounds for cache size {len(self.fat_cache)} (cluster {cluster})")
        try:
            current_word = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
            if cluster % 2 == 0:
                new_value = (current_word & 0xF000) | fat_value_to_set
            else:
                new_value = (current_word & 0x000F) | (fat_value_to_set << 4)
            struct.pack_into("<H", self.fat_cache, byte_offset, new_value)
            if new_value != current_word:
                self.fat_dirty = True
                old_fat_value = (current_word & 0x0FFF) if cluster % 2 == 0 else current_word >> 4
                if (old_fat_value == 0 and fat_value_to_set != 0) or \
                   (old_fat_value != 0 and fat_value_to_set == 0):
                    self._cached_allocated_clusters = None
        except struct.error as e:
             self.logger.error(f"Struct error setting FAT entry at offset {byte_offset} for cluster {cluster}: {e}")
             raise IOError("Failed to update FAT cache") from e

    def _get_cluster_chain(self, start_cluster: int) -> List[int]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if start_cluster < 2 or start_cluster >= self.num_clusters + 2:
             self.logger.warning(f"Invalid start cluster for chain retrieval: {start_cluster}")
             return []
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Failed to load FAT cache for cluster chain retrieval.")
            return []
        chain = []
        current_cluster = start_cluster
        max_chain_length = self.num_clusters + 2
        while len(chain) < max_chain_length:
            if not (2 <= current_cluster < self.num_clusters + 2):
                self.logger.error(f"Invalid cluster {current_cluster} encountered in chain starting at {start_cluster}.")
                break
            if current_cluster in chain:
                self.logger.error(f"Loop detected in cluster chain starting at {start_cluster} (cluster {current_cluster} repeated).")
                break
            chain.append(current_cluster)
            next_cluster = self._read_fat_entry_cached(current_cluster)
            if FAT12_EOC_MIN <= next_cluster <= FAT12_EOC:
                break
            if next_cluster == FAT12_BAD_CLUSTER:
                 self.logger.error(f"Bad cluster marker ({FAT12_BAD_CLUSTER:03X}) encountered in chain starting at {start_cluster} after cluster {current_cluster}.")
                 break
            if next_cluster == 0:
                self.logger.error(f"Unexpected zero entry encountered in chain starting at {start_cluster} after cluster {current_cluster}.")
                break
            current_cluster = next_cluster

        if len(chain) >= max_chain_length:
             self.logger.warning(f"Cluster chain starting at {start_cluster} exceeded maximum length ({max_chain_length}). Possible corruption.")
        self.logger.debug(f"Retrieved cluster chain for {start_cluster}: {chain}")
        return chain

    def _find_free_cluster(self) -> Optional[int]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Failed to load FAT cache for finding free cluster.")
            return None
        for cluster in range(2, self.num_clusters + 2):
            if self._read_fat_entry_cached(cluster, load_if_missing=False) == 0:
                return cluster
        self.logger.warning("No free clusters found on the disk.")
        return None

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[List[int]]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
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
                    self.logger.error(f"Could not allocate {num_clusters} clusters, only found {len(allocated_clusters)}. Rolling back.")
                    for c in allocated_clusters:
                        self._set_fat_entry_cached(c, 0)
                    return None
                self._set_fat_entry_cached(free_cluster, FAT12_EOC)
                allocated_clusters.append(free_cluster)
                if last_allocated is not None:
                    self._set_fat_entry_cached(last_allocated, free_cluster)
                last_allocated = free_cluster
            self.logger.debug(f"Successfully allocated cluster chain: {allocated_clusters}")
            return allocated_clusters
        except Exception as e:
            self.logger.exception(f"Error during cluster allocation: {e}. Rolling back.")
            for c in allocated_clusters:
                try:
                    self._set_fat_entry_cached(c, 0)
                except Exception as rollback_e:
                    self.logger.error(f"Error rolling back FAT entry for cluster {c}: {rollback_e}")
            return None

    def _free_cluster_chain(self, start_cluster: int) -> None:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if start_cluster < 2 or (self.fat_cache is None and not self._load_fat_cache()):
            self.logger.warning(f"Invalid start cluster for freeing chain: {start_cluster}")
            return
        current_cluster = start_cluster
        max_iterations = self.num_clusters + 2
        freed_count = 0
        for _ in range(max_iterations):
            if not (2 <= current_cluster < self.num_clusters + 2):
                self.logger.error(f"Invalid cluster {current_cluster} encountered while freeing chain starting at {start_cluster}.")
                break
            next_cluster = self._read_fat_entry_cached(current_cluster, load_if_missing=False)
            self._set_fat_entry_cached(current_cluster, 0)
            freed_count += 1
            if next_cluster == 0:
                 self.logger.warning(f"Unexpected zero entry found after cluster {current_cluster} while freeing chain starting at {start_cluster}.")
                 break
            if next_cluster < 2: # Also check if it points to FAT ID area
                 self.logger.error(f"Invalid next cluster value ({next_cluster}) found after cluster {current_cluster} while freeing chain.")
                 break
            if FAT12_EOC_MIN <= next_cluster <= FAT12_EOC:
                break
            if next_cluster == FAT12_BAD_CLUSTER:
                 self.logger.error(f"Bad cluster marker encountered after cluster {current_cluster} while freeing chain.")
                 break
            current_cluster = next_cluster
        if freed_count >= max_iterations : # Check if loop ran max_iterations times
            self.logger.warning(f"Cluster chain freeing starting at {start_cluster} reached max iterations ({max_iterations}). Possible corruption.")
        self.logger.debug(f"Freed {freed_count} clusters in chain starting at {start_cluster}")

    def _read_bytes(self, offset: int, length: int) -> bytes:
        if not self._init_completed or not self.boot_sector or not self.disk.physical_format:
            raise ValueError("Filesystem, boot sector, or disk geometry not initialized for reading.")
        bytes_per_sector = self.boot_sector.bytes_per_sector
        if bytes_per_sector <= 0: raise ValueError("Invalid bytes_per_sector")
        if length <= 0: return b""
        start_lba = offset // bytes_per_sector
        end_lba = (offset + length - 1) // bytes_per_sector
        num_sectors = end_lba - start_lba + 1
        try:
            start_cyl, start_head, start_sec = self.disk.lba_to_chs(start_lba)
            all_data_read = self.disk.read_sectors(start_cyl, start_head, start_sec, num_sectors)
            start_offset_in_first_sector_read = offset % bytes_per_sector
            if len(all_data_read) < start_offset_in_first_sector_read + length:
                raise IOError(f"Short read: got {len(all_data_read)} bytes, needed {start_offset_in_first_sector_read + length} from start of read buffer")
            return all_data_read[start_offset_in_first_sector_read : start_offset_in_first_sector_read + length]
        except ValueError as e:
            raise IOError(f"Failed to read data at offset {offset}: {e}") from e

    def _write_bytes(self, offset: int, data: bytes) -> None:
        if not data:
            return
        if not self._init_completed or not self.boot_sector or not self.disk.physical_format:
            raise ValueError("Filesystem, boot sector, or disk geometry not initialized for writing.")
        bytes_per_sector = self.boot_sector.bytes_per_sector
        if bytes_per_sector <= 0: raise ValueError("Invalid bytes_per_sector")
        length = len(data)
        start_lba = offset // bytes_per_sector
        # end_lba = (offset + length - 1) // bytes_per_sector # Not directly needed for current logic flow

        try:
            data_written_count = 0
            # Handle partial first sector if write doesn't align to sector boundary
            if offset % bytes_per_sector != 0:
                cyl, head, sec = self.disk.lba_to_chs(start_lba)
                sector_data = bytearray(self.disk.read_sector(cyl, head, sec))
                start_offset_in_sector = offset % bytes_per_sector
                bytes_to_write_in_sector = min(length, bytes_per_sector - start_offset_in_sector)
                sector_data[start_offset_in_sector : start_offset_in_sector + bytes_to_write_in_sector] = data[:bytes_to_write_in_sector]
                self.disk.write_sector(cyl, head, sec, bytes(sector_data))
                data_written_count += bytes_to_write_in_sector
                start_lba += 1 # Move to next LBA for full sectors

            # Handle full sectors
            num_full_sectors = (length - data_written_count) // bytes_per_sector
            if num_full_sectors > 0:
                full_sectors_data = data[data_written_count : data_written_count + num_full_sectors * bytes_per_sector]
                cyl_full, head_full, sec_full = self.disk.lba_to_chs(start_lba)
                self.disk.write_sectors(cyl_full, head_full, sec_full, full_sectors_data)
                data_written_count += len(full_sectors_data)
                start_lba += num_full_sectors

            # Handle partial last sector
            remaining_bytes = length - data_written_count
            if remaining_bytes > 0:
                cyl, head, sec = self.disk.lba_to_chs(start_lba)
                sector_data = bytearray(self.disk.read_sector(cyl, head, sec))
                sector_data[0:remaining_bytes] = data[data_written_count:]
                self.disk.write_sector(cyl, head, sec, bytes(sector_data))

        except ValueError as e:
            raise IOError(f"Failed to write at offset {offset}: {e}") from e

    def _read_cluster_chain_data(self, cluster_chain: List[int]) -> bytes:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if not cluster_chain: return b""
        result = bytearray()
        for cluster in cluster_chain:
            cluster_offset = self._cluster_to_offset(cluster)
            cluster_data = self._read_bytes(cluster_offset, self.allocation_unit_size)
            if len(cluster_data) < self.allocation_unit_size:
                self.logger.warning(f"Short read for cluster {cluster}: got {len(cluster_data)}, expected {self.allocation_unit_size}. Padding with zeros.")
                cluster_data += bytes(self.allocation_unit_size - len(cluster_data))
            elif len(cluster_data) > self.allocation_unit_size:
                self.logger.warning(f"Over-read for cluster {cluster}: got {len(cluster_data)}, expected {self.allocation_unit_size}. Truncating.")
                cluster_data = cluster_data[:self.allocation_unit_size]
            result.extend(cluster_data)
        return bytes(result)

    def _write_cluster_chain_data(self, cluster_chain: List[int], data: bytes) -> None:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if not cluster_chain and len(data) > 0:
            raise ValueError("Cluster chain empty but data present")
        data_pos = 0
        for cluster_index, cluster in enumerate(cluster_chain):
            cluster_offset = self._cluster_to_offset(cluster)
            bytes_to_write_for_this_cluster = min(len(data) - data_pos, self.allocation_unit_size)
            chunk = data[data_pos : data_pos + bytes_to_write_for_this_cluster]

            if bytes_to_write_for_this_cluster < self.allocation_unit_size:
                # This implies it's the last part of the file, or file is smaller than one cluster
                # Pad only if it's a full cluster write of partial data,
                # but _write_bytes handles sector-level padding if needed.
                # For cluster data, we write what we have. If a sector needs padding, _write_bytes handles it.
                # If the file itself is smaller than the cluster, the remaining space in the cluster is undefined/old data.
                # If `_write_bytes` writes full sectors, it will handle padding the last sector if `chunk` is not sector-aligned.
                self._write_bytes(cluster_offset, chunk)
            else:
                self._write_bytes(cluster_offset, chunk) # chunk is full cluster size

            data_pos += bytes_to_write_for_this_cluster
            if data_pos >= len(data):
                break

    def _find_entry_in_directory(self, dir_cluster: int, name_to_find: str) -> Tuple[FileInfo, Tuple[int, int]]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        name_upper = name_to_find.upper()
        if dir_cluster == 0:
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            for i in range(0, len(dir_data), 32):
                entry_data = dir_data[i:i + 32]
                if len(entry_data) < 32 or entry_data[0] == ENTRY_UNUSED:
                    break
                if entry_data[0] == ENTRY_DELETED:
                    continue
                entry = self._parse_single_directory_entry(entry_data)
                if entry and entry.name.upper() == name_upper:
                    return entry, (0, i)
            raise FileNotFoundError(f"Entry '{name_to_find}' not found in root")
        elif dir_cluster >= 2:
            cluster_chain = self._get_cluster_chain(dir_cluster)
            if not cluster_chain:
                raise FileNotFoundError(f"Directory cluster {dir_cluster} invalid or unreadable")
            for current_c_idx, current_c in enumerate(cluster_chain):
                cluster_offset = self._cluster_to_offset(current_c)
                cluster_data = self._read_bytes(cluster_offset, self.allocation_unit_size)
                for i in range(0, len(cluster_data), 32):
                    entry_data = cluster_data[i:i + 32]
                    if len(entry_data) < 32 or entry_data[0] == ENTRY_UNUSED:
                        # If this is not the last cluster in the chain, it's an error.
                        # If it IS the last, then it's truly the end of dir.
                        if current_c_idx < len(cluster_chain) -1:
                            self.logger.warning(f"Premature end of directory marker in cluster {current_c} but chain continues.")
                        break # Stop processing entries in this cluster
                    if entry_data[0] == ENTRY_DELETED:
                        continue
                    entry = self._parse_single_directory_entry(entry_data)
                    if entry and entry.name.upper() == name_upper:
                        return entry, (current_c, i)
                if entry_data[0] == ENTRY_UNUSED and current_c_idx == len(cluster_chain) -1 : # Check if we broke due to UNUSED on the last cluster
                    break
            raise FileNotFoundError(f"Entry '{name_to_find}' not found in directory")
        raise FileNotFoundError(f"Entry '{name_to_find}' not found in directory cluster {dir_cluster}")

    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[Tuple[int, int]]:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if dir_cluster == 0: # Root directory
            if self.root_dir_bytes == 0 : return None # No root dir space (e.g. FAT32 without root dir entries)
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            for i in range(0, len(dir_data), 32):
                entry_data = dir_data[i:i + 32]
                if len(entry_data) < 32: # Should not happen if root_dir_bytes is multiple of 32
                    self.logger.warning("Root directory data size not a multiple of 32.")
                    break
                if entry_data[0] in (ENTRY_UNUSED, ENTRY_DELETED):
                    return 0, i # Offset relative to start of root dir data
            self.logger.warning("Root directory is full.")
            return None
        elif dir_cluster >= 2: # Subdirectory
            cluster_chain = self._get_cluster_chain(dir_cluster)
            if not cluster_chain:
                    self.logger.error(f"Cannot find free entry: Invalid cluster chain for directory {dir_cluster}")
                    return None
            last_cluster_in_chain = -1
            for current_c in cluster_chain:
                last_cluster_in_chain = current_c
                cluster_offset = self._cluster_to_offset(current_c)
                cluster_data = self._read_bytes(cluster_offset, self.allocation_unit_size)
                for i in range(0, len(cluster_data), 32):
                    entry_data = cluster_data[i:i + 32]
                    if len(entry_data) < 32: break # Should not happen if allocation_unit_size is multiple of 32
                    if entry_data[0] in (ENTRY_UNUSED, ENTRY_DELETED):
                        return current_c, i # Offset relative to start of this cluster
            # If no free entry found in existing clusters, try to extend the directory
            if last_cluster_in_chain != -1 : # Ensure chain was not empty
                new_cluster = self._find_free_cluster()
                if new_cluster is None:
                    self.logger.warning(f"Subdirectory in cluster {dir_cluster} is full and no free clusters to extend.")
                    return None # No free cluster to extend the directory
                self._set_fat_entry_cached(last_cluster_in_chain, new_cluster)
                self._set_fat_entry_cached(new_cluster, FAT12_EOC) # Mark new cluster as end of chain
                # Initialize new directory cluster with all zeros (implies ENTRY_UNUSED for all entries)
                new_cluster_offset = self._cluster_to_offset(new_cluster)
                self._write_bytes(new_cluster_offset, bytes(self.allocation_unit_size))
                return new_cluster, 0 # First entry in the new cluster
        raise ValueError(f"Invalid directory cluster specified: {dir_cluster}")


    def _get_offset_for_directory_entry(self, dir_cluster_num: int, entry_offset_in_cluster: int) -> int:
        if not self._init_completed: raise ValueError("Filesystem not initialized")
        if dir_cluster_num == 0:
            return self.root_dir_start_offset + entry_offset_in_cluster
        elif dir_cluster_num >= 2:
            cluster_start_offset = self._cluster_to_offset(dir_cluster_num)
            return cluster_start_offset + entry_offset_in_cluster
        raise ValueError(f"Invalid cluster number specified for directory entry: {dir_cluster_num}")

    def _format_83_filename(self, name: str) -> bytes:
        parts = name.upper().split(".", 1)
        base_name = parts[0]
        extension = parts[1] if len(parts) > 1 else ""
        formatted_name = base_name.ljust(8)[:8].encode("cp437", errors='replace')
        formatted_ext = extension.ljust(3)[:3].encode("cp437", errors='replace')
        return formatted_name + formatted_ext

    def _create_directory_entry_bytes(self, name: str, is_dir: bool, starting_cluster: int, size: int, dt: datetime.datetime) -> bytes:
        entry = bytearray(32)
        fname_bytes = name.ljust(8).encode("cp437") + b"   " if name in [".", ".."] else self._format_83_filename(name)
        entry[0:11] = fname_bytes
        attr = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE
        entry[11] = attr
        entry[12:22] = bytes(10) # Reserved bytes
        time_val = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
        date_val = (((dt.year - 1980) & 0x7F) << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)
        struct.pack_into("<H", entry, 22, time_val)
        struct.pack_into("<H", entry, 24, date_val)
        struct.pack_into("<H", entry, 26, starting_cluster & 0xFFFF)
        struct.pack_into("<I", entry, 28, size if not is_dir else 0)
        return bytes(entry)

    _invalid_83_chars_pattern = re.compile(r'[\\/:*?"<>|\s+]') # Added + for one or more spaces
    _reserved_names = {"CON", "PRN", "AUX", "NUL",
                      "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                      "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
                      "CLOCK$"}

    def _is_valid_83_filename(self, name: str, allow_dots=True, allow_extension=True) -> bool:
        if not name or name[0] == " " or (name.endswith(".") and name not in [".", ".."]):
            return False
        if name in [".", ".."] and not allow_dots: # "." and ".." are valid but usually handled separately
            return False
        if self._invalid_83_chars_pattern.search(name) or any(0 < ord(c) < 32 for c in name):
            return False

        parts = name.split(".", 1)
        base = parts[0]
        ext = parts[1] if len(parts) > 1 else ""

        if not base or len(base) > 8:
             self.logger.debug(f"Invalid 8.3 name '{name}': Base part length error (1-8 required).")
             return False
        if len(ext) > 3:
             self.logger.debug(f"Invalid 8.3 name '{name}': Extension length error (0-3 required).")
             return False
        if ext and not allow_extension: # e.g. for directory names
            self.logger.debug(f"Invalid 8.3 name '{name}': Extension not allowed here.")
            return False
        if base.upper() in self._reserved_names:
            self.logger.debug(f"Invalid 8.3 name '{name}': Reserved device name.")
            return False
        # Check for leading/trailing spaces/dots in parts
        if base.strip() != base or ext.strip() != ext:
             return False
        if base.startswith(".") or (ext and ext.startswith(".")): # Dot is only allowed for extension separator
             return False
        return True
