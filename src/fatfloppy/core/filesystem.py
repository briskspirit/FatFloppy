# src/fatfloppy/core/filesystem.py
import re
import struct
import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .formats import FormatProfile, FATVolumeInfo
from .utils.logging_config import get_logger
from .disk import Disk

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


@dataclass
class FileInfo:
    name: str
    size: int
    is_dir: bool
    datetime: datetime.datetime
    attributes: str
    starting_cluster: int = 0


class BootSector:
    def __init__(self, sector_data: bytes):
        self.logger = get_logger(f"{self.__class__.__name__}")
        self.data = sector_data

    def is_valid(self) -> bool:
        if len(self.data) < 128:
            return False
        return True


class FATBootSector(BootSector):
    def __init__(self, sector_data: bytes):
        super().__init__(sector_data)
        self._parse_bpb()

    def is_valid(self) -> bool:
        if not super().is_valid():
            return False
        return (
            self.bytes_per_sector in [128, 256, 512, 1024, 2048, 4096]
            and self.sectors_per_cluster in [1, 2, 4, 8, 16, 32, 64, 128]
            and self.total_sectors > 0
            and self.sectors_per_fat > 0
            and self.num_fats in [1, 2]
            and self.reserved_sectors >= 1
        )

    def calculate_fat_type(self) -> str:
        if self.bytes_per_sector == 0 or self.sectors_per_cluster == 0:
            return "UNKNOWN"
        root_dir_bytes = self.root_entries * 32
        root_dir_sectors = (root_dir_bytes + self.bytes_per_sector - 1) // self.bytes_per_sector
        fat_sectors = self.num_fats * self.sectors_per_fat
        first_data_sector = self.reserved_sectors + fat_sectors + root_dir_sectors
        data_sectors = self.total_sectors - first_data_sector
        if data_sectors <= 0:
            return "UNKNOWN"
        total_clusters = data_sectors // self.sectors_per_cluster
        if total_clusters <= FAT12_MAX_CLUSTERS:
            return "FAT12"
        elif total_clusters < 65525:
            return "FAT16"
        return "FAT32"

    def _parse_bpb(self) -> None:
        try:
            self.bytes_per_sector = struct.unpack_from("<H", self.data, 0x00B)[0]
            self.sectors_per_cluster = self.data[0x00D]
            self.reserved_sectors = struct.unpack_from("<H", self.data, 0x00E)[0]
            self.num_fats = self.data[0x010]
            self.root_entries = struct.unpack_from("<H", self.data, 0x011)[0]
            self.total_sectors_16 = struct.unpack_from("<H", self.data, 0x013)[0]
            self.media_descriptor = self.data[0x015]
            self.sectors_per_fat_16 = struct.unpack_from("<H", self.data, 0x016)[0]
            self.sectors_per_track = struct.unpack_from("<H", self.data, 0x018)[0]
            self.num_heads = struct.unpack_from("<H", self.data, 0x01A)[0]
            self.hidden_sectors = struct.unpack_from("<I", self.data, 0x01C)[0]
            self.total_sectors_32 = struct.unpack_from("<I", self.data, 0x020)[0]
            self.total_sectors = self.total_sectors_32 if self.total_sectors_16 == 0 else self.total_sectors_16
            self.sectors_per_fat = self.sectors_per_fat_16
            self.drive_number = self.data[0x024]
            self.boot_signature = self.data[0x026]
            self.volume_id = struct.unpack_from("<I", self.data, 0x027)[0]
            try:
                self.volume_label = self.data[0x02B:0x02B + 11].decode("cp437").strip()
                self.fs_type = self.data[0x036:0x036 + 8].decode("cp437").strip()
            except UnicodeDecodeError:
                self.volume_label = "INVALID"
                self.fs_type = "INVALID"
        except (struct.error, IndexError) as e:
            self.logger.error(f"BPB parsing error: {e}")
            self.bytes_per_sector = 0
            self.sectors_per_cluster = 0
            self.total_sectors = 0
            self.sectors_per_fat = 0
            self.num_heads = 0
            self.sectors_per_track = 0
            raise ValueError("Failed to parse BPB.") from e


class Filesystem:
    def __init__(self, disk: Disk):
        self.logger = get_logger(self.__class__.__name__)
        self.disk = disk

    def is_valid(self) -> bool:
        raise NotImplementedError

    def list_directory(self, path: str) -> List[FileInfo]:
        raise NotImplementedError

    def read_file(self, path: str) -> bytes:
        raise NotImplementedError

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    def create_directory(self, path: str) -> None:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError

    def get_allocated_clusters(self) -> List[int]:
        raise NotImplementedError

    def get_free_space(self) -> Tuple[int, int]:
        raise NotImplementedError


class FATFilesystem(Filesystem):
    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self._init_completed = False
        self.boot_sector: Optional[FATBootSector] = None
        self._cached_allocated_clusters: Optional[List[int]] = None
        self.fat_cache: Optional[bytearray] = None
        self.fat_dirty = False
        self._load_boot_sector()
        if self.boot_sector and self.boot_sector.is_valid():
            try:
                self._initialize_filesystem_parameters()
                self._load_fat_cache()
                self._init_completed = True
            except Exception as e:
                self.logger.error(f"Initialization failed: {e}")
                self._init_completed = False

    def is_valid(self) -> bool:
        return self._init_completed

    def list_directory(self, path: str = "/") -> List[FileInfo]:
        if not self.is_valid():
            return []
        path = self._normalize_path(path)
        if path == "/":
            results = self._list_directory_by_cluster(0)
        else:
            dir_entry_info = self._find_path(path)
            if not dir_entry_info or not dir_entry_info.is_dir:
                return []
            results = self._list_directory_by_cluster(dir_entry_info.starting_cluster)
        return [
            entry for entry in results
            if entry.name not in [".", ".."]
            and not ("LFN" in entry.attributes or "VOL" in entry.attributes)
        ]

    def read_file(self, path: str) -> bytes:
        if not self.is_valid():
            raise ValueError("Invalid filesystem")
        path = self._normalize_path(path)
        file_entry_info = self._find_path(path)
        if not file_entry_info:
            raise FileNotFoundError(f"File not found: {path}")
        if file_entry_info.is_dir:
            raise IsADirectoryError(f"Path is a directory: {path}")
        if file_entry_info.size == 0 or file_entry_info.starting_cluster < 2:
            return b""
        cluster_chain = self._get_cluster_chain(file_entry_info.starting_cluster)
        if not cluster_chain:
            return b""
        file_data = self._read_cluster_chain_data(cluster_chain)
        return file_data[:file_entry_info.size]

    def write_file(self, path: str, data: bytes) -> None:
        if not self.is_valid():
            raise ValueError("Invalid filesystem")
        path = self._normalize_path(path)
        parent_path, file_name = self._split_path(path)
        if not self._is_valid_83_filename(file_name):
            raise ValueError(f"Invalid 8.3 filename: '{file_name}'")
        parent_dir_cluster = self._get_directory_cluster(parent_path)
        try:
            existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, file_name)
            if existing_entry:
                if existing_entry.is_dir:
                    raise IsADirectoryError(f"Cannot overwrite directory: {path}")
                self.delete(path)
        except FileNotFoundError:
            pass
        num_clusters_needed = (len(data) + self.cluster_size - 1) // self.cluster_size if len(data) > 0 else 0
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
        if not self.is_valid():
            raise ValueError("Invalid filesystem")
        path = self._normalize_path(path)
        parent_path, dir_name = self._split_path(path)
        if not self._is_valid_83_filename(dir_name, allow_extension=False):
            raise ValueError(f"Invalid 8.3 directory name: '{dir_name}'")
        parent_dir_cluster = self._get_directory_cluster(parent_path)
        try:
            existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, dir_name)
            if existing_entry:
                if existing_entry.is_dir:
                    return
                raise FileExistsError(f"A file with name '{dir_name}' exists")
        except FileNotFoundError:
            pass
        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            raise IOError("No free clusters available")
        self._set_fat_entry_cached(new_cluster, FAT12_EOC)
        cluster_offset = self._cluster_to_offset(new_cluster)
        self._write_bytes(cluster_offset, b"\x00" * self.cluster_size)
        now = datetime.datetime.now()
        dot_entry = self._create_directory_entry_bytes(".", True, new_cluster, 0, now)
        dotdot_entry = self._create_directory_entry_bytes(
            "..", True, parent_dir_cluster if parent_dir_cluster > 0 else 0, 0, now
        )
        self._write_bytes(cluster_offset, dot_entry)
        self._write_bytes(cluster_offset + 32, dotdot_entry)
        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            self._set_fat_entry_cached(new_cluster, 0)
            self._commit_fat()
            raise IOError(f"No space in parent directory {parent_path}")
        parent_entry_dir_cluster, parent_entry_offset_in_cluster = entry_location
        parent_entry_disk_offset = self._get_offset_for_directory_entry(
            parent_entry_dir_cluster, parent_entry_offset_in_cluster
        )
        dir_entry_bytes = self._create_directory_entry_bytes(dir_name, True, new_cluster, 0, now)
        self._write_bytes(parent_entry_disk_offset, dir_entry_bytes)
        self._commit_fat()
        self.disk.flush()

    def delete(self, path: str) -> None:
        if not self.is_valid():
            raise ValueError("Invalid filesystem")
        path = self._normalize_path(path)
        if path == "/":
            raise ValueError("Cannot delete root directory")
        parent_path, name = self._split_path(path)
        parent_dir_cluster = self._get_directory_cluster(parent_path)
        entry_to_delete, entry_location = self._find_entry_in_directory(parent_dir_cluster, name)
        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)
        if entry_to_delete.is_dir:
            dir_contents = self._list_directory_by_cluster(entry_to_delete.starting_cluster)
            non_dot_entries = [e for e in dir_contents if e.name not in [".", ".."]]
            if non_dot_entries:
                raise OSError(f"Directory not empty: {path}")
        self._write_bytes(entry_disk_offset, bytes([ENTRY_DELETED]))
        if entry_to_delete.starting_cluster >= 2:
            self._free_cluster_chain(entry_to_delete.starting_cluster)
        self._commit_fat()
        self.disk.flush()
        self._cached_allocated_clusters = None

    def get_allocated_clusters(self) -> List[int]:
        if not self.is_valid() or self.fat_cache is None:
            return []
        if self._cached_allocated_clusters is not None and not self.fat_dirty:
            return self._cached_allocated_clusters
        allocated_clusters = [
            cluster for cluster in range(2, self.num_clusters + 2)
            if self._read_fat_entry_cached(cluster, load_if_missing=False) != 0
        ]
        self._cached_allocated_clusters = allocated_clusters
        return allocated_clusters

    def format_fs(self, profile: FormatProfile) -> None:
        """Format the disk with a FAT12 filesystem based on the given FormatProfile."""
        if not isinstance(profile, FormatProfile):
            raise TypeError("profile must be a FormatProfile object")
        if not profile.boot_sector or not isinstance(profile.boot_sector, FATVolumeInfo):
            raise ValueError("Invalid boot_sector in FormatProfile")

        boot_sector = profile.boot_sector
        boot_sector_bytes = boot_sector.to_bytes()

        self.disk.write_boot_sector(boot_sector_bytes)

        bytes_per_sector = boot_sector.bytes_per_sector
        logger.debug(f"Bytes per sector: {bytes_per_sector}")
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

        for fat_num in range(num_fats):
            start_lba = reserved_sectors + fat_num * sectors_per_fat
            c, h, s = self.disk.lba_to_chs(start_lba)
            self.disk.write_sectors(c, h, s, fat_data)

        root_dir_bytes = root_entries * 32
        root_dir_sectors = (root_dir_bytes + bytes_per_sector - 1) // bytes_per_sector
        root_dir_data = bytearray(root_dir_sectors * bytes_per_sector)
        root_dir_start_lba = reserved_sectors + num_fats * sectors_per_fat
        c, h, s = self.disk.lba_to_chs(root_dir_start_lba)
        self.disk.write_sectors(c, h, s, root_dir_data)

        self.boot_sector = FATBootSector(boot_sector_bytes)
        self.fat_cache = bytearray(fat_data)
        self.fat_dirty = False
        self._cached_allocated_clusters = []
        self._initialize_filesystem_parameters()

    def get_free_space(self) -> Tuple[int, int]:
        if not self.is_valid():
            return 0, 0
        total_data_bytes = self.num_clusters * self.cluster_size
        allocated_count = len(self.get_allocated_clusters())
        free_clusters = max(self.num_clusters - allocated_count, 0)
        free_bytes = free_clusters * self.cluster_size
        return free_bytes, total_data_bytes

    def _normalize_path(self, path: str) -> str:
        path = path.replace("\\", "/")
        if len(path) > 1:
            path = path.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
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
            boot_sector_data = self.disk.read_boot_sector()
            if not boot_sector_data:
                self.boot_sector = None
                logger.error("Boot sector data is empty")
                return
            self.boot_sector = FATBootSector(boot_sector_data)
        except Exception as e:
            self.logger.error(f"Error reading boot sector: {e}")
            self.boot_sector = None

    def _initialize_filesystem_parameters(self) -> None:
        if not self.boot_sector or not self.boot_sector.is_valid():
            raise ValueError("Invalid Boot Sector / BPB")
        bpb = self.boot_sector
        self.cluster_size = bpb.sectors_per_cluster * bpb.bytes_per_sector
        vbr_lba = 0 if bpb.hidden_sectors == 0 or bpb.hidden_sectors >= bpb.total_sectors / 2 else bpb.hidden_sectors
        self.fat_start_offset = (vbr_lba + bpb.reserved_sectors) * bpb.bytes_per_sector
        self.fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start_offset = self.fat_start_offset + (bpb.num_fats * self.fat_size_bytes)
        self.root_dir_bytes = bpb.root_entries * 32
        self.root_dir_sectors = (self.root_dir_bytes + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        self.data_area_start_offset = self.root_dir_start_offset + self.root_dir_bytes
        first_data_sector_lba = (self.data_area_start_offset + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        total_data_sectors = max(bpb.total_sectors - first_data_sector_lba, 0)
        self.num_clusters = total_data_sectors // bpb.sectors_per_cluster
        self.fat_type = bpb.calculate_fat_type()
        if self.data_area_start_offset > bpb.total_sectors * bpb.bytes_per_sector:
            raise ValueError("Data area offset exceeds disk size")
        self._init_completed = True

    def _cluster_to_offset(self, cluster: int) -> int:
        if cluster < 2:
            raise ValueError(f"Invalid cluster number: {cluster}")
        return self.data_area_start_offset + (cluster - 2) * self.cluster_size

    def _get_directory_cluster(self, dir_path: str) -> int:
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
        entries = []
        if cluster == 0:
            if self.root_dir_bytes == 0:
                return []
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            entries = self._parse_directory_data(dir_data)
        elif cluster >= 2:
            cluster_chain = self._get_cluster_chain(cluster)
            if not cluster_chain:
                return []
            dir_data = self._read_cluster_chain_data(cluster_chain)
            entries = self._parse_directory_data(dir_data)
        return entries

    def _parse_directory_data(self, dir_data: bytes) -> List[FileInfo]:
        entries = []
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
                        datetime=datetime.datetime.min, attributes="VOL", starting_cluster=0
                    )
                except Exception:
                    return None
            base_name = entry_data[0:8].decode("cp437").rstrip()
            extension = entry_data[8:11].decode("cp437").rstrip()
            if entry_data[0] == 0x05:
                base_name = "\xE5" + base_name[1:]
            full_name = base_name if not extension else f"{base_name}.{extension}"
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
        except (UnicodeDecodeError, struct.error):
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
        except ValueError:
            return datetime.datetime(1980, 1, 1, 0, 0, 0)

    def _find_path(self, path: str) -> Optional[FileInfo]:
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
            self.fat_cache = bytearray(self._read_bytes(self.fat_start_offset, self.fat_size_bytes))
            self.fat_dirty = False
            return True
        except Exception as e:
            self.logger.error(f"Error loading FAT cache: {e}")
            self.fat_cache = None
            return False

    def _write_fat_sectors(self) -> None:
        if self.fat_cache is None or not self.fat_dirty:
            return
        try:
            for fat_num in range(self.boot_sector.num_fats):
                fat_offset = self.fat_start_offset + (fat_num * self.fat_size_bytes)
                self._write_bytes(fat_offset, self.fat_cache)
            self.fat_dirty = False
            self._cached_allocated_clusters = None
        except Exception as e:
            self.logger.error(f"Error writing FAT cache: {e}")
            raise IOError("Failed to write FAT cache") from e

    def _commit_fat(self) -> None:
        self._write_fat_sectors()

    def _read_fat_entry_cached(self, cluster: int, load_if_missing: bool = True) -> int:
        if not self._init_completed:
            raise ValueError("Filesystem not initialized")
        if self.fat_cache is None and load_if_missing:
            if not self._load_fat_cache():
                raise IOError("Failed to load FAT cache")
        if self.fat_cache is None:
            raise ValueError("FAT cache not loaded")
        if not (0 <= cluster < self.num_clusters + 2):
            return FAT12_BAD_CLUSTER
        byte_offset = int(cluster * 1.5)
        if byte_offset + 1 >= len(self.fat_cache):
            return FAT12_BAD_CLUSTER
        value = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
        return value & 0x0FFF if cluster % 2 == 0 else value >> 4

    def _set_fat_entry_cached(self, cluster: int, value: int) -> None:
        if self.fat_cache is None and not self._load_fat_cache():
            raise IOError("Failed to load FAT cache")
        if not (2 <= cluster < self.num_clusters + 2):
            raise ValueError(f"Invalid cluster {cluster}")
        fat_value_to_set = value & 0x0FFF
        byte_offset = int(cluster * 1.5)
        if byte_offset + 1 >= len(self.fat_cache):
            raise IndexError("FAT cache offset out of bounds")
        current_value = struct.unpack_from("<H", self.fat_cache, byte_offset)[0]
        if cluster % 2 == 0:
            new_value = (current_value & 0xF000) | fat_value_to_set
        else:
            new_value = (current_value & 0x000F) | (fat_value_to_set << 4)
        struct.pack_into("<H", self.fat_cache, byte_offset, new_value)
        if new_value != current_value:
            self.fat_dirty = True
            old_fat_value = (current_value & 0x0FFF) if cluster % 2 == 0 else current_value >> 4
            if (old_fat_value == 0 and fat_value_to_set != 0) or (old_fat_value != 0 and fat_value_to_set == 0):
                self._cached_allocated_clusters = None

    def _get_cluster_chain(self, start_cluster: int) -> List[int]:
        if start_cluster < 2 or (self.fat_cache is None and not self._load_fat_cache()):
            return []
        chain = []
        current_cluster = start_cluster
        max_chain_length = self.num_clusters + 1
        while len(chain) < max_chain_length:
            if not (2 <= current_cluster < self.num_clusters + 2) or current_cluster in chain:
                break
            chain.append(current_cluster)
            next_cluster = self._read_fat_entry_cached(current_cluster)
            if next_cluster >= FAT12_EOC_MIN and next_cluster <= FAT12_EOC:
                break
            if next_cluster == 0 or next_cluster < 2:
                break
            current_cluster = next_cluster
        return chain

    def _find_free_cluster(self) -> Optional[int]:
        if self.fat_cache is None and not self._load_fat_cache():
            return None
        for cluster in range(2, self.num_clusters + 2):
            if self._read_fat_entry_cached(cluster, load_if_missing=False) == 0:
                return cluster
        return None

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[List[int]]:
        if num_clusters <= 0:
            return []
        if self.fat_cache is None and not self._load_fat_cache():
            return None
        allocated_clusters = []
        last_allocated = None
        try:
            for _ in range(num_clusters):
                free_cluster = self._find_free_cluster()
                if free_cluster is None:
                    for c in allocated_clusters:
                        self._set_fat_entry_cached(c, 0)
                    return None
                self._set_fat_entry_cached(free_cluster, FAT12_EOC)
                allocated_clusters.append(free_cluster)
                if last_allocated is not None:
                    self._set_fat_entry_cached(last_allocated, free_cluster)
                last_allocated = free_cluster
            return allocated_clusters
        except Exception as e:
            self.logger.error(f"Allocation error: {e}")
            for c in allocated_clusters:
                self._set_fat_entry_cached(c, 0)
            return None

    def _free_cluster_chain(self, start_cluster: int) -> None:
        if start_cluster < 2 or (self.fat_cache is None and not self._load_fat_cache()):
            return
        current_cluster = start_cluster
        max_iterations = self.num_clusters + 1
        for _ in range(max_iterations):
            if not (2 <= current_cluster < self.num_clusters + 2):
                break
            next_cluster = self._read_fat_entry_cached(current_cluster)
            self._set_fat_entry_cached(current_cluster, 0)
            if next_cluster == 0 or next_cluster < 2 or next_cluster >= FAT12_EOC_MIN:
                break
            current_cluster = next_cluster

    def _read_bytes(self, offset: int, length: int) -> bytes:
        if self.disk.geometry.bytes_per_sector != self.boot_sector.bytes_per_sector:
            self.logger.error(f"Geometry mismatch: disk={self.disk.geometry.bytes_per_sector}, boot_sector={self.boot_sector.bytes_per_sector}")
        if length <= 0:
            return b""
        if not self.boot_sector or self.boot_sector.bytes_per_sector == 0:
            raise ValueError("Invalid boot sector")
        if not self.disk or not self.disk.geometry:
            raise ValueError("Disk or geometry not available")
        bytes_per_sector = self.boot_sector.bytes_per_sector
        start_lba = offset // bytes_per_sector
        end_lba = (offset + length - 1) // bytes_per_sector
        num_sectors = end_lba - start_lba + 1
        try:
            start_cyl, start_head, start_sec = self.disk.lba_to_chs(start_lba)
            all_data_read = self.disk.read_sectors(start_cyl, start_head, start_sec, num_sectors)
            start_offset = offset % bytes_per_sector
            if len(all_data_read) < start_offset + length:
                raise IOError(f"Short read: got {len(all_data_read)} bytes, needed {start_offset + length}")
            return all_data_read[start_offset:start_offset + length]
        except ValueError as e:
            raise IOError(f"Failed to read data at offset {offset}: {e}") from e

    def _write_bytes(self, offset: int, data: bytes) -> None:
        if not data:
            return
        if not self.boot_sector or self.boot_sector.bytes_per_sector == 0:
            raise ValueError("Invalid boot sector")
        if not self.disk or not self.disk.geometry:
            raise ValueError("Disk or geometry not available")
        bytes_per_sector = self.boot_sector.bytes_per_sector
        length = len(data)
        start_lba = offset // bytes_per_sector
        end_lba = (offset + length - 1) // bytes_per_sector
        try:
            data_written = 0
            if offset % bytes_per_sector != 0:
                cyl, head, sec = self.disk.lba_to_chs(start_lba)
                sector_data = bytearray(self.disk.read_sector(cyl, head, sec))
                start_offset = offset % bytes_per_sector
                bytes_to_write = min(length, bytes_per_sector - start_offset)
                sector_data[start_offset:start_offset + bytes_to_write] = data[:bytes_to_write]
                self.disk.write_sector(cyl, head, sec, bytes(sector_data))
                data_written = bytes_to_write
            full_start_lba = start_lba if offset % bytes_per_sector == 0 else start_lba + 1
            bytes_remaining = length - data_written
            num_full_sectors = bytes_remaining // bytes_per_sector
            if num_full_sectors > 0:
                full_data = data[data_written:data_written + num_full_sectors * bytes_per_sector]
                full_start_cyl, full_start_head, full_start_sec = self.disk.lba_to_chs(full_start_lba)
                self.disk.write_sectors(full_start_cyl, full_start_head, full_start_sec, full_data)
                data_written += num_full_sectors * bytes_per_sector
            if (offset + length) % bytes_per_sector != 0 and data_written < length:
                last_lba = end_lba
                cyl, head, sec = self.disk.lba_to_chs(last_lba)
                sector_data = bytearray(self.disk.read_sector(cyl, head, sec))
                bytes_to_write = length - data_written
                sector_data[0:bytes_to_write] = data[data_written:data_written + bytes_to_write]
                self.disk.write_sector(cyl, head, sec, bytes(sector_data))
        except ValueError as e:
            raise IOError(f"Failed to write at offset {offset}: {e}") from e

    def _read_cluster_chain_data(self, cluster_chain: List[int]) -> bytes:
        result = bytearray()
        for cluster in cluster_chain:
            cluster_offset = self._cluster_to_offset(cluster)
            cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
            if len(cluster_data) < self.cluster_size:
                cluster_data += bytes(self.cluster_size - len(cluster_data))
            elif len(cluster_data) > self.cluster_size:
                cluster_data = cluster_data[:self.cluster_size]
            result.extend(cluster_data)
        return bytes(result)

    def _write_cluster_chain_data(self, cluster_chain: List[int], data: bytes) -> None:
        if not cluster_chain and len(data) > 0:
            raise ValueError("Cluster chain empty but data present")
        data_pos = 0
        for cluster in cluster_chain:
            cluster_offset = self._cluster_to_offset(cluster)
            bytes_remaining = len(data) - data_pos
            chunk_size = min(bytes_remaining, self.cluster_size)
            chunk = data[data_pos:data_pos + chunk_size]
            if chunk_size < self.cluster_size:
                padded_chunk = chunk + bytes(self.cluster_size - chunk_size)
                self._write_bytes(cluster_offset, padded_chunk)
            else:
                self._write_bytes(cluster_offset, chunk)
            data_pos += chunk_size
            if data_pos >= len(data):
                break

    def _find_entry_in_directory(self, dir_cluster: int, name_to_find: str) -> Tuple[FileInfo, Tuple[int, int]]:
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
                raise FileNotFoundError(f"Directory cluster {dir_cluster} invalid")
            for current_c in cluster_chain:
                cluster_offset = self._cluster_to_offset(current_c)
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
                for i in range(0, len(cluster_data), 32):
                    entry_data = cluster_data[i:i + 32]
                    if len(entry_data) < 32 or entry_data[0] == ENTRY_UNUSED:
                        break
                    if entry_data[0] == ENTRY_DELETED:
                        continue
                    entry = self._parse_single_directory_entry(entry_data)
                    if entry and entry.name.upper() == name_upper:
                        return entry, (current_c, i)
            raise FileNotFoundError(f"Entry '{name_to_find}' not found in directory")
        raise ValueError(f"Invalid directory cluster: {dir_cluster}")

    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[Tuple[int, int]]:
        if dir_cluster == 0:
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            for i in range(0, len(dir_data), 32):
                entry_data = dir_data[i:i + 32]
                if len(entry_data) < 32:
                    break
                if entry_data[0] in (ENTRY_UNUSED, ENTRY_DELETED):
                    return 0, i
            return None
        elif dir_cluster >= 2:
            cluster_chain = self._get_cluster_chain(dir_cluster)
            last_cluster_in_chain = -1
            for current_c in cluster_chain:
                last_cluster_in_chain = current_c
                cluster_offset = self._cluster_to_offset(current_c)
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
                for i in range(0, len(cluster_data), 32):
                    entry_data = cluster_data[i:i + 32]
                    if len(entry_data) < 32:
                        break
                    if entry_data[0] in (ENTRY_UNUSED, ENTRY_DELETED):
                        return current_c, i
            if last_cluster_in_chain == -1:
                return None
            new_cluster = self._find_free_cluster()
            if new_cluster is None:
                return None
            self._set_fat_entry_cached(last_cluster_in_chain, new_cluster)
            self._set_fat_entry_cached(new_cluster, FAT12_EOC)
            new_cluster_offset = self._cluster_to_offset(new_cluster)
            self._write_bytes(new_cluster_offset, bytes([ENTRY_UNUSED]) * self.cluster_size)
            return new_cluster, 0
        raise ValueError(f"Invalid directory cluster: {dir_cluster}")

    def _get_offset_for_directory_entry(self, dir_cluster_num: int, entry_offset_in_cluster: int) -> int:
        if dir_cluster_num == 0:
            return self.root_dir_start_offset + entry_offset_in_cluster
        elif dir_cluster_num >= 2:
            cluster_start_offset = self._cluster_to_offset(dir_cluster_num)
            return cluster_start_offset + entry_offset_in_cluster
        raise ValueError(f"Invalid cluster number: {dir_cluster_num}")

    def _format_83_filename(self, name: str) -> bytes:
        parts = name.upper().split(".", 1)
        base_name = parts[0]
        extension = parts[1] if len(parts) > 1 else ""
        formatted_name = base_name.ljust(8).encode("cp437")
        formatted_ext = extension.ljust(3).encode("cp437")
        return formatted_name + formatted_ext

    def _create_directory_entry_bytes(self, name: str, is_dir: bool, starting_cluster: int, size: int, dt: datetime.datetime) -> bytes:
        entry = bytearray(32)
        fname_bytes = name.ljust(8).encode("cp437") + b"   " if name in [".", ".."] else self._format_83_filename(name)
        entry[0:11] = fname_bytes
        attr = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE
        entry[11] = attr
        entry[12:22] = bytes(10)
        time_val = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
        date_val = (((dt.year - 1980) & 0x7F) << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)
        entry[22:24] = struct.pack("<H", time_val)
        entry[24:26] = struct.pack("<H", date_val)
        entry[26:28] = struct.pack("<H", starting_cluster & 0xFFFF)
        entry[28:32] = struct.pack("<I", size)
        return bytes(entry)

    _invalid_83_chars_pattern = re.compile(r'[\\/:*?"<>|\s+]')
    _reserved_names = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "LPT1", "LPT2", "LPT3"}

    def _is_valid_83_filename(self, name: str, allow_dots=True, allow_extension=True) -> bool:
        if not name or name[0] == " " or (name.endswith(".") and name not in [".", ".."]):
            return False
        if name in [".", ".."] and not allow_dots:
            return False
        if self._invalid_83_chars_pattern.search(name) or any(0 < ord(c) < 32 for c in name):
            return False
        parts = name.split(".", 1)
        base = parts[0]
        ext = parts[1] if len(parts) > 1 else ""
        if not base or len(base) > 8 or len(ext) > 3:
            return False
        if ext and not allow_extension:
            return False
        if base.upper() in self._reserved_names:
            return False
        if base.endswith(" ") or base.endswith(".") or ext.endswith(" ") or ext.endswith("."):
            return False
        return True
