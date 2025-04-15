import datetime
import struct
from dataclasses import dataclass
from typing import List, Optional, Union, Callable, Tuple

from ..utils.logging_config import get_logger
from .disk import Disk

logger = get_logger()

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
        self.logger = get_logger(self.__class__.__name__)
        self.data = sector_data
        self.logger.debug(f"BootSector initialized with {len(sector_data)} bytes")

    def is_valid(self) -> bool:
        valid = len(self.data) >= 512 and struct.unpack_from('<H', self.data, 0x1FE)[0] == 0xAA55
        self.logger.debug(f"Boot sector signature validation: {'valid' if valid else 'invalid'}")
        return valid

class FATBootSector(BootSector):
    def __init__(self, sector_data: bytes):
        super().__init__(sector_data)
        self.parse_bpb()

    def parse_bpb(self) -> None:
        self.logger.debug("Parsing BPB structure")
        try:
            self.bytes_per_sector = struct.unpack_from('<H', self.data, 0x00B)[0]
            self.sectors_per_cluster = self.data[0x00D]
            self.reserved_sectors = struct.unpack_from('<H', self.data, 0x00E)[0]
            self.num_fats = self.data[0x010]
            self.root_entries = struct.unpack_from('<H', self.data, 0x011)[0]
            self.total_sectors = struct.unpack_from('<H', self.data, 0x013)[0]
            self.media_descriptor = self.data[0x015]
            self.sectors_per_fat = struct.unpack_from('<H', self.data, 0x016)[0]
            self.sectors_per_track = struct.unpack_from('<H', self.data, 0x018)[0]
            self.num_heads = struct.unpack_from('<H', self.data, 0x01A)[0]
            self.hidden_sectors = struct.unpack_from('<I', self.data, 0x01C)[0]

            if self.total_sectors == 0:
                self.total_sectors = struct.unpack_from('<I', self.data, 0x020)[0]

            try:
                self.volume_label = self.data[0x02B:0x036].decode('cp437').strip()
                self.fs_type = self.data[0x036:0x03E].decode('cp437').strip()
            except Exception as e:
                self.logger.warning(f"Error parsing volume label or fs_type: {e}")
                self.volume_label = "NO NAME"
                self.fs_type = "FAT12"

            self.logger.debug(f"BPB parsed: {self.bytes_per_sector} bytes/sector, "
                          f"{self.sectors_per_cluster} sectors/cluster, "
                          f"{self.total_sectors} total sectors, "
                          f"{self.sectors_per_track} sectors/track, "
                          f"{self.num_heads} heads")
        except Exception as e:
            self.logger.error(f"Error parsing BPB: {e}", exc_info=True)
            raise

    def is_valid(self) -> bool:
        if not super().is_valid():
            return False

        valid = (self.bytes_per_sector in [128, 256, 512, 1024, 2048, 4096] and
                self.sectors_per_cluster in [1, 2, 4, 8, 16, 32, 64, 128] and
                self.total_sectors > 0 and self.sectors_per_fat > 0)

        self.logger.debug(f"BPB validation: {'valid' if valid else 'invalid'}")
        if not valid:
            self.logger.warning(f"Invalid BPB values: bytes_per_sector={self.bytes_per_sector}, "
                            f"sectors_per_cluster={self.sectors_per_cluster}, "
                            f"total_sectors={self.total_sectors}, "
                            f"sectors_per_fat={self.sectors_per_fat}")
        return valid

    def get_fat_type(self) -> str:
        root_dir_sectors = (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        fat_sectors = self.num_fats * self.sectors_per_fat
        data_sectors = self.total_sectors - (self.reserved_sectors + fat_sectors + root_dir_sectors)
        total_clusters = data_sectors // self.sectors_per_cluster

        if total_clusters < 4085:
            fat_type = "FAT12"
        elif total_clusters < 65525:
            fat_type = "FAT16"
        else:
            fat_type = "FAT32"

        self.logger.debug(f"Detected FAT type: {fat_type} ({total_clusters} clusters)")
        return fat_type

class Filesystem:
    def __init__(self, disk: Disk):
        self.logger = get_logger(self.__class__.__name__)
        self.disk = disk
        self.logger.debug("Filesystem base class initialized")

    def is_valid(self) -> bool:
        raise NotImplementedError("Subclasses must implement is_valid")

    def list_directory(self, path: str) -> List[FileInfo]:
        raise NotImplementedError("Subclasses must implement list_directory")

    def read_file(self, path: str, progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        raise NotImplementedError("Subclasses must implement read_file")

    def write_file(self, path: str, data: bytes,
                  progress_callback: Optional[Callable[[float], None]] = None) -> None:
        raise NotImplementedError("Subclasses must implement write_file")

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("Subclasses must implement create_directory")

    def delete(self, path: str) -> None:
        raise NotImplementedError("Subclasses must implement delete")

class FATFilesystem(Filesystem):
    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self._init_completed = False
        self.boot_sector = self._read_boot_sector()
        self._cached_allocated_clusters = None
        self.fat_cache: Optional[bytearray] = None # In-memory cache for FAT sectors

        if self.is_valid():
            self._initialize_filesystem_parameters()
            self._init_completed = True
            self.logger.info("Valid FAT filesystem initialized")
        else:
            self.logger.warning("Invalid or unrecognized filesystem")

    def is_valid(self) -> bool:
        valid = isinstance(self.boot_sector, FATBootSector) and self.boot_sector.is_valid()
        self.logger.debug(f"Filesystem validation: {'valid' if valid else 'invalid'}")
        return valid

    def list_directory(self, path: str = "/") -> List[FileInfo]:
        if not self.is_valid():
            self.logger.warning("Cannot list directory: Invalid filesystem")
            return []

        self.logger.debug(f"Listing directory: {path}")
        if path == "/":
            results = self._list_root_directory()
        else:
            dir_entry = self._find_path(path)
            if not dir_entry or not dir_entry.is_dir:
                self.logger.warning(f"Directory not found or not a directory: {path}")
                return []
            results = self._list_directory_by_cluster(dir_entry.starting_cluster)

        filtered_results = [entry for entry in results if entry.name not in [".", ".."]]
        self.logger.debug(f"Found {len(filtered_results)} items in directory {path} (excluding . and ..)")
        return filtered_results

    def read_file(self, path: str, progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        if not self.is_valid():
            error_msg = f"Cannot read file {path}: Invalid filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Reading file: {path}")
        file_entry = self._find_path(path)
        if not file_entry or file_entry.is_dir:
            error_msg = f"File not found: {path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if file_entry.starting_cluster < 2:
            self.logger.warning(f"File {path} has invalid starting cluster {file_entry.starting_cluster}, returning empty content")
            return b''

        try:
            cluster_chain = self._get_cluster_chain(file_entry.starting_cluster)
            self.logger.debug(f"File {path} has {len(cluster_chain)} clusters")

            file_data = self._read_cluster_chain(cluster_chain, progress_callback)
            result = file_data[:file_entry.size]
            self.logger.info(f"Read {len(result)} bytes from file {path}")
            return result
        except Exception as e:
            self.logger.error(f"Error reading file {path}: {e}", exc_info=True)
            raise

    def write_file(self, path: str, data: bytes,
                  progress_callback: Optional[Callable[[float], None]] = None) -> None:
        if not self.is_valid():
            error_msg = f"Cannot write file {path}: Invalid filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if not self._is_valid_83_name(path):
            error_msg = f"Invalid 8.3 filename: {path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Writing file: {path}, {len(data)} bytes")
        parent_path, file_name = self._split_path(path)
        parent_entry = self._find_path(parent_path)

        if not parent_entry:
            if parent_path == "/":
                parent_cluster = 0
                self.logger.debug(f"Writing to root directory")
            else:
                error_msg = f"Parent directory not found: {parent_path}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
        elif not parent_entry.is_dir:
            error_msg = f"Not a directory: {parent_path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        else:
            parent_cluster = parent_entry.starting_cluster
            self.logger.debug(f"Writing to directory in cluster {parent_cluster}")

        for entry in self.list_directory(parent_path):
            if entry.name.upper() == file_name.upper():
                self.logger.info(f"File {file_name} already exists, deleting it first")
                self.delete(path)
                break

        num_clusters_needed = (len(data) + self.cluster_size - 1) // self.cluster_size
        if num_clusters_needed == 0:
            num_clusters_needed = 1

        self.logger.debug(f"Allocating {num_clusters_needed} clusters for file {path}")
        clusters = self._allocate_cluster_chain(num_clusters_needed)
        if not clusters:
            error_msg = "Not enough free space on disk"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Writing data to allocated clusters")
        self._write_cluster_chain(clusters, data, progress_callback)

        now = datetime.datetime.now()
        self.logger.debug(f"Creating directory entry for {file_name}")
        entry = self._create_directory_entry(file_name, False, clusters[0], len(data), now)

        entry_offset = self._find_free_directory_entry(parent_cluster)
        if entry_offset is None:
            self.logger.error(f"No space in directory {parent_path}")
            self.logger.debug("Freeing allocated clusters as directory entry could not be created")
            self._free_cluster_chain(clusters[0])
            raise ValueError("No space in directory")

        self.logger.debug(f"Writing directory entry at offset {entry_offset}")
        self._write_bytes(entry_offset, entry)

        self._cached_allocated_clusters = None
        self.logger.info(f"Successfully wrote file {path}, {len(data)} bytes in {len(clusters)} clusters")
        self.disk.flush()

    def create_directory(self, path: str) -> None:
        if not self.is_valid():
            error_msg = f"Cannot create directory {path}: Invalid filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Creating directory: {path}")
        parent_path, dir_name = self._split_path(path)

        if not self._is_valid_83_name(dir_name):
            error_msg = f"Invalid 8.3 directory name: {dir_name}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        parent_entry = self._find_path(parent_path)
        if not parent_entry and parent_path != "/":
            error_msg = f"Parent directory not found: {parent_path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if parent_entry and not parent_entry.is_dir:
            error_msg = f"Not a directory: {parent_path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        parent_cluster = 0 if parent_path == "/" else parent_entry.starting_cluster
        self.logger.debug(f"Parent directory is in cluster {parent_cluster}")

        for entry in self.list_directory(parent_path):
            if entry.name.upper() == dir_name.upper():
                if entry.is_dir:
                    self.logger.info(f"Directory {dir_name} already exists")
                    return
                error_msg = f"File with same name exists: {path}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            error_msg = "No free clusters available"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Allocated cluster {new_cluster} for directory {dir_name}")
        self._set_fat_entry(new_cluster, 0xFFF)

        cluster_offset = self.data_area_start + (new_cluster - 2) * self.cluster_size
        self.logger.debug(f"Initializing directory cluster at offset {cluster_offset}")
        self._write_bytes(cluster_offset, b'\x00' * self.cluster_size)

        now = datetime.datetime.now()
        self.logger.debug("Creating . and .. entries")
        dot_entry = self._create_directory_entry(".", True, new_cluster, 0, now)
        dotdot_entry = self._create_directory_entry("..", True, parent_cluster, 0, now)

        self.logger.debug("Writing . and .. entries to new directory")
        self._write_bytes(cluster_offset, dot_entry)
        self._write_bytes(cluster_offset + 32, dotdot_entry)

        self.logger.debug(f"Creating directory entry for {dir_name} in parent directory")
        dir_entry = self._create_directory_entry(dir_name, True, new_cluster, 0, now)

        entry_offset = self._find_free_directory_entry(parent_cluster)
        if entry_offset is None:
            self.logger.error("No space in parent directory, freeing allocated cluster")
            self._set_fat_entry(new_cluster, 0)
            raise ValueError("No space in parent directory")

        self.logger.debug(f"Writing directory entry at offset {entry_offset}")
        self._write_bytes(entry_offset, dir_entry)

        self._cached_allocated_clusters = None
        self.logger.info(f"Successfully created directory {path} in cluster {new_cluster}")
        self.disk.flush()

    def delete(self, path: str) -> None:
        self.logger.debug(f"Deleting item: {path}")
        if path == "/":
            error_msg = "Cannot delete root directory"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        parent_path, name = self._split_path(path)
        self.logger.debug(f"Parent path: {parent_path}, item name: {name}")

        parent_entry = self._find_path(parent_path)
        if not parent_entry and parent_path != "/":
            error_msg = f"Parent directory not found: {parent_path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        parent_cluster = 0 if parent_path == "/" else parent_entry.starting_cluster
        self.logger.debug(f"Parent directory is in cluster {parent_cluster}")

        entry_to_delete = None
        entry_offset = None

        if parent_cluster == 0:
            self.logger.debug("Searching for entry in root directory")
            root_dir_data = self._read_bytes(self.root_dir_start, self.boot_sector.root_entries * 32)
            for i in range(0, len(root_dir_data), 32):
                entry_data = root_dir_data[i:i+32]
                if entry_data[0] == 0:
                    break

                if entry_data[0] != 0xE5:
                    entry = self._parse_directory_entry(entry_data)
                    if entry and entry.name.upper() == name.upper():
                        entry_to_delete = entry
                        entry_offset = self.root_dir_start + i
                        self.logger.debug(f"Found entry to delete at offset {entry_offset}")
                        break
        else:
            self.logger.debug(f"Searching for entry in directory cluster chain starting at {parent_cluster}")
            cluster_chain = self._get_cluster_chain(parent_cluster)
            for cluster in cluster_chain:
                cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
                self.logger.debug(f"Checking cluster {cluster} at offset {cluster_offset}")
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)

                for i in range(0, len(cluster_data), 32):
                    entry_data = cluster_data[i:i+32]
                    if entry_data[0] == 0:
                        break

                    if entry_data[0] != 0xE5:
                        entry = self._parse_directory_entry(entry_data)
                        if entry and entry.name.upper() == name.upper():
                            entry_to_delete = entry
                            entry_offset = cluster_offset + i
                            self.logger.debug(f"Found entry to delete at offset {entry_offset} in cluster {cluster}")
                            break

                if entry_to_delete:
                    break

        if not entry_to_delete or entry_offset is None:
            error_msg = f"Item not found: {path}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Found item to delete: {entry_to_delete.name}, is_dir={entry_to_delete.is_dir}, "
                        f"starting_cluster={entry_to_delete.starting_cluster}")

        if entry_to_delete.is_dir:
            self.logger.debug(f"Checking if directory {path} is empty")
            dir_entries = self._list_directory_by_cluster(entry_to_delete.starting_cluster)
            non_dot_entries = [e for e in dir_entries if e.name not in [".", ".."]]
            if non_dot_entries:
                error_msg = f"Directory not empty: {path}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            self.logger.debug("Directory is empty, proceeding with deletion")

        self.logger.debug(f"Marking entry as deleted at offset {entry_offset}")
        self._write_bytes(entry_offset, b'\xE5' + self._read_bytes(entry_offset + 1, 31))

        if entry_to_delete.starting_cluster >= 2:
            self.logger.debug(f"Freeing cluster chain starting at {entry_to_delete.starting_cluster}")
            self._free_cluster_chain(entry_to_delete.starting_cluster)

        self.logger.debug("Invalidating cached allocated clusters")
        self._cached_allocated_clusters = None

        self.logger.info(f"Successfully deleted {path}")
        self.disk.flush()

    def get_allocated_clusters(self) -> List[int]:
        self.logger.debug("Getting allocated clusters")
        if not self.is_valid():
            self.logger.warning("Cannot get allocated clusters: Invalid filesystem")
            return []

        if self._cached_allocated_clusters is not None:
            self.logger.debug(f"Using cached allocated clusters ({len(self._cached_allocated_clusters)} clusters)")
            return self._cached_allocated_clusters

        allocated_clusters = []
        try:
            self.logger.debug(f"Scanning FAT for allocated clusters (range: 2-{self.num_clusters+1})")
            fat_data = self._read_fat_sectors()[0]
            for cluster in range(2, self.num_clusters + 2):
                fat_entry = self._read_fat_entry_mem(cluster, fat_data)
                if fat_entry != 0 and fat_entry < 0xFF0:
                    allocated_clusters.append(cluster)
                elif fat_entry >= 0xFF8 and fat_entry <= 0xFFF:
                    allocated_clusters.append(cluster)
            self.logger.info(f"Found {len(allocated_clusters)} allocated clusters")
        except Exception as e:
            self.logger.error(f"Error getting allocated clusters: {e}", exc_info=True)

        self._cached_allocated_clusters = allocated_clusters
        self.logger.debug("Cached allocated clusters list")
        return allocated_clusters

    def get_free_space(self) -> Tuple[int, int]:
        self.logger.debug("Calculating filesystem free space")
        if not self.is_valid():
            self.logger.warning("Cannot get free space: Invalid filesystem")
            return (0, 0)

        try:
            total_bytes = self.boot_sector.total_sectors * self.boot_sector.bytes_per_sector
            self.logger.debug(f"Total disk space: {total_bytes} bytes "
                            f"({total_bytes/1024:.1f}KB, {total_bytes/1024/1024:.2f}MB)")

            allocated_clusters = self.get_allocated_clusters()
            total_clusters = self.num_clusters
            free_clusters = total_clusters - len(allocated_clusters)
            self.logger.debug(f"Free clusters: {free_clusters}/{total_clusters}")

            free_bytes = free_clusters * self.cluster_size
            percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
            self.logger.info(f"Free space: {free_bytes} bytes ({free_bytes/1024:.1f}KB, {percent_free:.1f}%)")

            return (free_bytes, total_bytes)
        except Exception as e:
            self.logger.error(f"Error calculating free space: {e}", exc_info=True)
            return (0, total_bytes)

    def _read_boot_sector(self) -> Union[FATBootSector, None]:
        try:
            self.logger.debug("Reading boot sector")
            if hasattr(self.disk.driver, 'read_bytes_direct'):
                self.logger.debug("Using direct read method for boot sector")
                boot_sector_data = self.disk.driver.read_bytes_direct(0, 512)
            else:
                self.logger.debug("Using sector read method for boot sector")
                boot_sector_data = self.disk.read_sector(0, 0, 1)

            return FATBootSector(boot_sector_data)
        except Exception as e:
            self.logger.error(f"Error reading boot sector: {e}", exc_info=True)
            return None

    def _initialize_filesystem_parameters(self) -> None:
        if self._init_completed:
            self.logger.debug("Filesystem parameters already initialized")
            return

        self.logger.debug("Initializing filesystem parameters")
        bpb = self.boot_sector

        if hasattr(bpb, 'hidden_sectors') and bpb.hidden_sectors > 100:
            self.logger.warning(f"Unreasonable hidden_sectors value: {bpb.hidden_sectors}, capping at 0")
            bpb.hidden_sectors = 0

        self.cluster_size = bpb.sectors_per_cluster * bpb.bytes_per_sector

        self.fat_start = bpb.reserved_sectors * bpb.bytes_per_sector
        if hasattr(bpb, 'hidden_sectors') and bpb.hidden_sectors > 0 and bpb.hidden_sectors < 100:
            self.fat_start += bpb.hidden_sectors * bpb.bytes_per_sector

        fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start = self.fat_start + (bpb.num_fats * fat_size_bytes)

        self.root_dir_sectors = (bpb.root_entries * 32 + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        root_dir_size = self.root_dir_sectors * bpb.bytes_per_sector

        self.data_area_start = self.root_dir_start + root_dir_size

        data_sectors = bpb.total_sectors - (bpb.reserved_sectors +
                                        bpb.num_fats * bpb.sectors_per_fat +
                                        self.root_dir_sectors)
        self.num_clusters = data_sectors // bpb.sectors_per_cluster

        self.fat_type = bpb.get_fat_type()

        self.logger.info(f"Filesystem parameters initialized: "
                      f"cluster_size={self.cluster_size}, "
                      f"num_clusters={self.num_clusters}, "
                      f"fat_type={self.fat_type}")

    def _list_root_directory(self) -> List[FileInfo]:
        self.logger.debug("Reading root directory")
        entries = []

        try:
            root_dir_data = self._read_bytes(self.root_dir_start, self.boot_sector.root_entries * 32)
            self.logger.debug(f"Read {len(root_dir_data)} bytes from root directory")

            for i in range(0, len(root_dir_data), 32):
                if root_dir_data[i] == 0x00:
                    break

                entry_data = root_dir_data[i:i+32]
                entry = self._parse_directory_entry(entry_data)
                if entry:
                    entries.append(entry)

            self.logger.debug(f"Found {len(entries)} entries in root directory")
            return entries
        except Exception as e:
            self.logger.error(f"Error reading root directory: {e}", exc_info=True)
            return []

    def _list_directory_by_cluster(self, cluster: int) -> List[FileInfo]:
        entries = []

        if cluster == 0:
            self.logger.debug("Cluster 0 requested, redirecting to root directory")
            return self._list_root_directory()

        if cluster < 2:
            self.logger.warning(f"Invalid directory cluster: {cluster}")
            return entries

        try:
            self.logger.debug(f"Reading directory from cluster: {cluster}")
            cluster_chain = self._get_cluster_chain(cluster)
            if not cluster_chain:
                self.logger.warning(f"Empty cluster chain for cluster: {cluster}")
                return entries

            total_bytes = len(cluster_chain) * self.cluster_size
            start_offset = self.data_area_start + (cluster_chain[0] - 2) * self.cluster_size
            data = self._read_bytes(start_offset, total_bytes)
            self.logger.debug(f"Read {len(data)} bytes from {len(cluster_chain)} clusters")

            for i in range(0, len(data), 32):
                if i + 32 > len(data):
                    break
                entry_data = data[i:i+32]
                if entry_data[0] == 0x00:
                    break
                if entry_data[0] == 0xE5:
                    continue
                entry = self._parse_directory_entry(entry_data)
                if entry:
                    entries.append(entry)

            self.logger.debug(f"Found {len(entries)} entries in directory cluster {cluster}")
            return entries
        except Exception as e:
            self.logger.error(f"Error reading directory from cluster {cluster}: {e}", exc_info=True)
            return []

    def _parse_directory_entry(self, entry_data):
        if len(entry_data) < 32:
            self.logger.warning(f"Directory entry too short: {len(entry_data)} bytes")
            return None

        first_byte = entry_data[0]
        if first_byte == 0x00 or first_byte == 0xE5:
            return None

        attr = entry_data[11]
        if attr & 0x08 or attr & 0x0F == 0x0F:
            return None

        try:
            name = entry_data[0:8].decode('cp437').strip()
            ext = entry_data[8:11].decode('cp437').strip()
            full_name = f"{name}.{ext}" if ext else name

            invalid_chars = set('"*/:<>?\\|')
            if any(c < ' ' or c in invalid_chars for c in full_name):
                self.logger.warning(f"Invalid characters in filename: {repr(full_name)}")
                return None

            is_dir = bool(attr & 0x10)
            size = struct.unpack('<I', entry_data[28:32])[0]
            starting_cluster = struct.unpack('<H', entry_data[26:28])[0]

            if is_dir and starting_cluster < 2 and full_name not in [".", ".."]:
                self.logger.warning(f"Invalid directory entry: {full_name}, cluster {starting_cluster}")
                return None

            time_val = struct.unpack('<H', entry_data[22:24])[0]
            date_val = struct.unpack('<H', entry_data[24:26])[0]

            second = (time_val & 0x1F) * 2
            minute = (time_val >> 5) & 0x3F
            hour = (time_val >> 11) & 0x1F
            day = date_val & 0x1F
            month = (date_val >> 5) & 0x0F
            year = 1980 + ((date_val >> 9) & 0x7F)

            try:
                dt = datetime.datetime(year, month, day, hour, minute, second)
            except ValueError:
                self.logger.warning(f"Invalid date/time for {full_name}: Y:{year} M:{month} D:{day}")
                dt = datetime.datetime(1980, 1, 1, 0, 0, 0)

            attributes = []
            if attr & 0x01: attributes.append("RO")
            if attr & 0x02: attributes.append("H")
            if attr & 0x04: attributes.append("S")
            if attr & 0x20: attributes.append("A")
            attr_str = " ".join(attributes) if attributes else "-"

            entry = FileInfo(
                name=full_name,
                size=0 if is_dir else size,
                is_dir=is_dir,
                datetime=dt,
                attributes=attr_str,
                starting_cluster=starting_cluster
            )

            self.logger.debug(f"Parsed entry: {full_name}, {'dir' if is_dir else f'file ({size} bytes)'}, "
                           f"cluster: {starting_cluster}")
            return entry
        except Exception as e:
            self.logger.error(f"Error parsing directory entry: {e}", exc_info=True)
            return None

    def _find_path(self, path: str) -> Optional[FileInfo]:
        self.logger.debug(f"Finding path: {path}")
        if path == "/" or path == "":
            self.logger.debug("Root directory requested, returning None")
            return None

        parts = path.strip("/").split("/")
        self.logger.debug(f"Path parts: {parts}")

        current_cluster = 0
        current_entry = None

        for part in parts:
            if current_cluster == 0:
                self.logger.debug(f"Getting entries from root directory")
                entries = self._list_root_directory()
            else:
                self.logger.debug(f"Getting entries from cluster {current_cluster}")
                entries = self._list_directory_by_cluster(current_cluster)

            found = False
            for entry in entries:
                if entry.name.upper() == part.upper():
                    if not entry.is_dir and part != parts[-1]:
                        self.logger.warning(f"'{part}' is not a directory in path")
                        return None
                    current_entry = entry
                    current_cluster = entry.starting_cluster
                    self.logger.debug(f"Found '{part}' with starting cluster {current_cluster}")
                    found = True
                    break

            if not found:
                self.logger.warning(f"Path component '{part}' not found")
                return None

        self.logger.debug(f"Path found: {path}")
        return current_entry

    def _split_path(self, path: str) -> Tuple[str, str]:
        self.logger.debug(f"Splitting path: {path}")
        path = path.rstrip("/")
        if "/" not in path:
            self.logger.debug(f"No directory separator found, returning root and '{path}'")
            return "/", path

        parent_path = path[:path.rindex("/")]
        if not parent_path:
            parent_path = "/"

        name = path[path.rindex("/")+1:]
        self.logger.debug(f"Split result: parent='{parent_path}', name='{name}'")
        return parent_path, name

    def _read_bytes(self, offset: int, length: int) -> bytes:
        self.logger.debug(f"Reading {length} bytes from offset {offset}")
        sector_size = self.boot_sector.bytes_per_sector
        start_sector = offset // sector_size
        end_sector = (offset + length - 1) // sector_size

        if start_sector == end_sector:
            self.logger.debug(f"Single sector read optimization for sector {start_sector}")
            cylinder, head, sector = self._lba_to_chs(start_sector)
            sector_data = self.disk.read_sector(cylinder, head, sector)
            sector_offset = offset % sector_size
            result = sector_data[sector_offset:sector_offset + length]
            self.logger.debug(f"Read {len(result)} bytes from sector C:{cylinder} H:{head} S:{sector}")
            return result

        self.logger.debug(f"Multi-sector read from sector {start_sector} to {end_sector}")
        data = bytearray()
        current_offset = offset
        remaining_length = length

        while remaining_length > 0:
            sector_num = current_offset // sector_size
            sector_offset = current_offset % sector_size
            sectors_to_read = 1
            bytes_in_first_sector = sector_size - sector_offset

            if remaining_length > bytes_in_first_sector:
                additional_sectors = (remaining_length - bytes_in_first_sector + sector_size - 1) // sector_size
                sectors_to_read += additional_sectors
                self.logger.debug(f"Reading {sectors_to_read} sectors")

            cylinder, head, sector = self._lba_to_chs(sector_num)
            self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
            sector_data = self.disk.read_sector(cylinder, head, sector)

            bytes_to_read = min(remaining_length, sector_size - sector_offset)
            data.extend(sector_data[sector_offset:sector_offset + bytes_to_read])

            current_offset += bytes_to_read
            remaining_length -= bytes_to_read

        self.logger.debug(f"Completed multi-sector read, got {len(data)} bytes")
        return bytes(data)

    def _write_bytes(self, offset: int, data: bytes) -> None:
        if not data:
            self.logger.debug("No data to write, returning")
            return

        self.logger.debug(f"Writing {len(data)} bytes at offset {offset}")
        sector_size = self.boot_sector.bytes_per_sector
        start_sector = offset // sector_size
        end_sector = (offset + len(data) - 1) // sector_size

        if start_sector == end_sector:
            sector_offset = offset % sector_size
            self.logger.debug(f"Single sector write optimization for sector {start_sector}")
            cylinder, head, sector = self._lba_to_chs(start_sector)
            sector_data = bytearray(self.disk.read_sector(cylinder, head, sector))
            sector_data[sector_offset:sector_offset + len(data)] = data
            self.disk.write_sector(cylinder, head, sector, sector_data)
            return

        self.logger.debug(f"Multi-sector write from sector {start_sector} to {end_sector}")
        current_offset = offset
        data_pos = 0

        while data_pos < len(data):
            sector_num = current_offset // sector_size
            sector_offset = current_offset % sector_size
            cylinder, head, sector = self._lba_to_chs(sector_num)
            self.logger.debug(f"Processing sector C:{cylinder} H:{head} S:{sector}")
            sector_data = bytearray(self.disk.read_sector(cylinder, head, sector))
            bytes_to_write = min(len(data) - data_pos, sector_size - sector_offset)
            self.logger.debug(f"Writing {bytes_to_write} bytes to sector")
            sector_data[sector_offset:sector_offset + bytes_to_write] = data[data_pos:data_pos + bytes_to_write]
            self.disk.write_sector(cylinder, head, sector, sector_data)
            current_offset += bytes_to_write
            data_pos += bytes_to_write

        self.logger.debug(f"Completed multi-sector write of {len(data)} bytes")

    def _lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        self.logger.debug(f"Converting LBA {lba} to CHS")
        sectors_per_track = self.boot_sector.sectors_per_track
        heads = self.boot_sector.num_heads

        sector = (lba % sectors_per_track) + 1
        temp = lba // sectors_per_track
        head = temp % heads
        cylinder = temp // heads

        self.logger.debug(f"LBA {lba} = C:{cylinder} H:{head} S:{sector}")
        return cylinder, head, sector

    def _read_fat_sectors(self) -> None:
        """Reads all FAT sectors for the primary FAT into the cache."""
        if self.fat_cache is not None:
            self.logger.debug("FAT cache already loaded")
            return
        try:
            self.logger.debug("Reading all FAT sectors into memory")
            fat_size_bytes = self.boot_sector.sectors_per_fat * self.boot_sector.bytes_per_sector
            # Read only the primary FAT (FAT #0)
            self.fat_cache = bytearray(self._read_bytes(self.fat_start, fat_size_bytes))
            self.logger.info(f"Read {len(self.fat_cache)} bytes into FAT cache (FAT #1)")
        except Exception as e:
            self.logger.error(f"Error reading FAT sectors into cache: {e}", exc_info=True)
            self.fat_cache = None # Ensure cache is None on error
            raise # Re-raise the exception

    def _write_fat_sectors(self) -> None:
        """Writes the cached FAT sectors back to both FAT copies on disk."""
        if self.fat_cache is None:
            self.logger.warning("FAT cache is not loaded, cannot write back.")
            return
        try:
            self.logger.debug("Writing all FAT sectors back to disk")
            fat_size_bytes = len(self.fat_cache)
            for fat_num in range(self.boot_sector.num_fats):
                fat_offset = self.fat_start + (fat_num * fat_size_bytes)
                self._write_bytes(fat_offset, self.fat_cache)
                self.logger.info(f"Wrote {fat_size_bytes} bytes from cache to FAT #{fat_num+1} at offset {fat_offset}")
            # Invalidate cluster list cache after writing FAT
            self._cached_allocated_clusters = None
        except Exception as e:
            self.logger.error(f"Error writing FAT sectors from cache: {e}", exc_info=True)
            raise

    def _read_fat_entry(self, cluster: int) -> int:
        """Read a FAT12 entry for the given cluster number using the cache."""
        self.logger.debug(f"Reading FAT entry for cluster {cluster} (using cache)")
        return self._read_fat_entry_mem(cluster) # Read from cache

    def _read_fat_entry_mem(self, cluster: int) -> int:
        """Reads a FAT12 entry from the in-memory cache."""
        if self.fat_cache is None:
            self.logger.debug("FAT cache miss, loading FAT sectors.")
            self._read_fat_sectors()
            if self.fat_cache is None: # Check again in case read failed
                 self.logger.error(f"Cannot read FAT entry for cluster {cluster}: Cache load failed.")
                 # Or potentially fall back to direct read? For now, raise error
                 raise IOError("Failed to load FAT cache for reading entry")

        # self.logger.debug(f"Reading FAT entry from memory for cluster {cluster}")
        if self.fat_type == "FAT12":
            byte_offset = int(cluster * 1.5)
            # Check bounds
            if byte_offset + 1 >= len(self.fat_cache):
                 self.logger.error(f"FAT cache bounds error reading cluster {cluster} (offset {byte_offset})")
                 # Handle error appropriately, e.g., return a specific value or raise
                 return 0xFFFF # Indicate an error? Or raise?
            # Read 2 bytes directly from cache
            value = struct.unpack_from('<H', self.fat_cache, byte_offset)[0]
            if cluster % 2 == 0:
                fat_value = value & 0x0FFF
            else:
                fat_value = value >> 4
            # self.logger.debug(f"Memory FAT entry for cluster {cluster} = 0x{fat_value:03X}")
            return fat_value
        else:
            raise NotImplementedError("Only FAT12 is supported")

    def _set_fat_entry_mem(self, cluster: int, value: int) -> None:
        """Sets a FAT12 entry in the in-memory cache."""
        if self.fat_cache is None:
             self.logger.debug("FAT cache miss on set, loading FAT sectors.")
             self._read_fat_sectors()
             if self.fat_cache is None:
                  self.logger.error(f"Cannot set FAT entry for cluster {cluster}: Cache load failed.")
                  raise IOError("Failed to load FAT cache for setting entry")

        self.logger.debug(f"Setting FAT entry in memory for cluster {cluster} to 0x{value:03X}")
        if self.fat_type == "FAT12":
            value &= 0x0FFF
            byte_offset = int(cluster * 1.5)
            # Check bounds
            if byte_offset + 1 >= len(self.fat_cache):
                 self.logger.error(f"FAT cache bounds error writing cluster {cluster} (offset {byte_offset})")
                 raise IndexError("FAT cache offset out of bounds")

            # Read current 2 bytes from cache
            current_value = struct.unpack_from('<H', self.fat_cache, byte_offset)[0]
            if cluster % 2 == 0: # Even
                new_value = (current_value & 0xF000) | value
            else: # Odd
                new_value = (current_value & 0x000F) | (value << 4)
            # Write the modified 2 bytes back to cache
            struct.pack_into('<H', self.fat_cache, byte_offset, new_value)
            old_fat_value = 0 # Assume free initially for simplicity of check
            if cluster % 2 == 0: old_fat_value = current_value & 0x0FFF
            else: old_fat_value = current_value >> 4

            if (old_fat_value == 0 and value != 0) or \
            (old_fat_value != 0 and value == 0):
                # We allocated a free cluster or freed an allocated one
                self.logger.debug("Invalidating _cached_allocated_clusters due to FAT change")
                self._cached_allocated_clusters = None
            # self.logger.debug(f"Updated memory FAT entry at offset {byte_offset} to 0x{new_value:04X}")
        else:
            raise NotImplementedError("Only FAT12 is supported")

    def _set_fat_entry(self, cluster: int, value: int) -> None:
        """Sets FAT entry in cache and writes cache back to disk buffer."""
        self.logger.debug(f"Setting FAT entry for cluster {cluster} to 0x{value:03X} (will write cache)")
        self._set_fat_entry_mem(cluster, value) # Update cache
        self._write_fat_sectors()              # Write cache back immediately

    def _get_cluster_chain(self, start_cluster: int) -> List[int]:
        """Get the list of clusters in a chain starting from start_cluster"""
        self.logger.debug(f"Getting cluster chain starting from cluster {start_cluster}")
        if start_cluster == 0:
            self.logger.debug("Root directory (cluster 0) has no cluster chain")
            return []
        if start_cluster < 2:
            self.logger.warning(f"Invalid starting cluster {start_cluster}")
            return []

        # ***** REMOVE THIS LINE *****
        # fat_data = self._read_fat_sectors()[0] # Incorrect - _read_fat_sectors returns None
        # **************************

        chain = []
        cluster = start_cluster
        max_length = self.num_clusters + 2 # Allow slightly more than total for error detection
        self.logger.debug(f"Following chain with max length {max_length}")

        while cluster >= 2 and cluster < 0xFF0 and len(chain) < max_length:
            chain.append(cluster)

            # ***** USE _read_fat_entry *****
            # This method correctly uses the cache via _read_fat_entry_mem
            next_cluster = self._read_fat_entry(cluster)
            # *****************************

            self.logger.debug(f"Cluster {cluster} -> Next cluster: 0x{next_cluster:03X}")

            if next_cluster >= 0xFF8: # Check for EOC (>= 0xFF8 for FAT12) or bad cluster
                self.logger.debug(f"End of chain or special marker reached at cluster {cluster}")
                break
            if next_cluster == 0: # Pointing to free cluster - chain broken
                 self.logger.warning(f"Chain broken at cluster {cluster}: points to free cluster 0")
                 break
            if next_cluster < 2: # Invalid cluster pointer
                 self.logger.warning(f"Chain broken at cluster {cluster}: points to invalid cluster {next_cluster}")
                 break

            # Detect circular chains more robustly
            if next_cluster in chain:
                self.logger.warning(f"Circular reference detected at cluster {next_cluster} (points back into chain)")
                # Decide how to handle - truncate or raise? Truncate for now.
                break

            cluster = next_cluster

        if len(chain) >= max_length:
             self.logger.warning(f"Cluster chain exceeded maximum length ({max_length}), possibly unterminated or looped.")

        self.logger.debug(f"Cluster chain contains {len(chain)} clusters")
        return chain

    def _find_free_cluster(self) -> Optional[int]:
        """Find the next free cluster using the in-memory FAT cache."""
        self.logger.debug("Searching for a free cluster (using cache)")
        if self.fat_cache is None: # Load cache if not already loaded
            self._read_fat_sectors()
        if self.fat_cache is None: # Check if loading failed
            self.logger.error("Cannot find free cluster: FAT cache failed to load.")
            return None

        for cluster in range(2, self.num_clusters + 2):
            if self._read_fat_entry_mem(cluster) == 0: # Read from cache
                self.logger.debug(f"Found free cluster: {cluster}")
                return cluster
        self.logger.warning("No free clusters found")
        return None

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[List[int]]:
        self.logger.debug(f"Allocating chain of {num_clusters} clusters")
        if num_clusters <= 0:
            self.logger.debug("No clusters requested, returning empty list")
            return []

        # Ensure FAT cache is loaded and up-to-date before starting
        if self.fat_cache is None:
            self._read_fat_sectors()
        # If allocation happened before, ensure subsequent writes flushed the cache
        # For simplicity here, assume cache is ready or read again if needed.

        clusters = []
        prev_cluster = None

        for i in range(num_clusters):
            cluster = self._find_free_cluster() # Finds based on current cache state
            if cluster is None:
                # ... (handle error, free previously allocated clusters IN CACHE) ...
                self.logger.warning(f"Could not allocate {num_clusters} clusters (allocated {len(clusters)} so far)")
                for c in clusters:
                    self.logger.debug(f"Freeing previously allocated cluster {c} in cache")
                    self._set_fat_entry_mem(c, 0) # Use memory version
                # Note: We might not need to write FAT back here if we failed.
                return None

            # ***** FIX: Mark cluster as non-free *immediately* in the cache *****
            # Temporarily mark it, e.g., with a temporary value like 0xFFE or the next cluster if known
            # Let's use 0xFFF for now, it will be overwritten by the link later if needed
            self._set_fat_entry_mem(cluster, 0xFFF) # Mark as used in cache
            # *****************************************************************

            clusters.append(cluster)
            self.logger.debug(f"Allocated cluster {cluster} ({i+1}/{num_clusters})")

            if prev_cluster is not None:
                self.logger.debug(f"Linking cluster {prev_cluster} -> {cluster} in cache")
                # This links the *previous* cluster to the *current* one
                self._set_fat_entry_mem(prev_cluster, cluster)

            prev_cluster = cluster

        # Mark the *actual* last cluster as end of chain (0xFFF)
        # This might overwrite the temporary marker placed above, which is fine.
        if prev_cluster is not None:
            self.logger.debug(f"Marking cluster {prev_cluster} as end of chain in cache")
            self._set_fat_entry_mem(prev_cluster, 0xFFF)

        # ----> Crucial: Write the modified cache back to disk buffer <----
        self._write_fat_sectors()

        self.logger.info(f"Successfully allocated chain of {len(clusters)} clusters")
        return clusters

    def _free_cluster_chain(self, start_cluster: int) -> None:
        """Frees a chain of clusters in the FAT, starting from start_cluster."""
        self.logger.debug(f"Freeing cluster chain starting at cluster {start_cluster}")
        if start_cluster < 2:
             self.logger.warning(f"Attempted to free invalid start cluster {start_cluster}")
             return

        cluster = start_cluster
        freed_count = 0
        # Limit loop iterations to prevent infinite loops on corrupted FATs
        max_iterations = self.num_clusters + 2

        for _ in range(max_iterations):
            if cluster < 2 or cluster >= 0xFF0: # Stop if invalid or EOC/bad
                break

            # Read the *next* cluster number BEFORE overwriting the current entry
            next_cluster = self._read_fat_entry(cluster)

            # Mark the *current* cluster as free (0)
            self.logger.debug(f"Freeing cluster {cluster} (next was 0x{next_cluster:03X})")
            self._set_fat_entry(cluster, 0) # This uses cache + writes back immediately
            freed_count += 1

            # Move to the next cluster in the chain
            cluster = next_cluster

            # Check for explicit end of chain marker after reading it
            if next_cluster >= 0xFF8: # >= 0xFF8 for FAT12 EOC/reserved
                 self.logger.debug("End of chain marker reached during free.")
                 break
            # Check for broken chain (pointing to already free cluster)
            if next_cluster == 0:
                 self.logger.warning("Chain broken during free: encountered pointer to free cluster.")
                 break

        if _ == max_iterations - 1 and cluster >= 2 and cluster < 0xFF0:
             self.logger.warning(f"Cluster chain freeing exceeded maximum iterations ({max_iterations}), possibly looped.")

        # No need to explicitly write FAT sectors here, as _set_fat_entry does it now.
        self.logger.info(f"Freed {freed_count} clusters in chain")
        # Invalidate the cluster list cache as allocation has changed
        self._cached_allocated_clusters = None

    def _read_cluster_chain(self, cluster_chain: List[int],
                        progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        self.logger.debug(f"Reading data from cluster chain with {len(cluster_chain)} clusters")
        result = bytearray()
        total_clusters = len(cluster_chain)

        for i, cluster in enumerate(cluster_chain):
            cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
            self.logger.debug(f"Reading cluster {cluster} at offset {cluster_offset}")
            cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
            result.extend(cluster_data)

            if progress_callback and total_clusters > 0:
                progress = (i + 1) / total_clusters
                self.logger.debug(f"Read progress: {progress:.2f}")
                progress_callback(progress)

        self.logger.debug(f"Read {len(result)} bytes from cluster chain")
        return bytes(result)

    def _write_cluster_chain(self, cluster_chain: List[int], data: bytes,
                        progress_callback: Optional[Callable[[float], None]] = None) -> None:
        self.logger.debug(f"Writing {len(data)} bytes to cluster chain with {len(cluster_chain)} clusters")
        remaining = len(data)
        data_pos = 0
        total_clusters = len(cluster_chain)

        for i, cluster in enumerate(cluster_chain):
            cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
            self.logger.debug(f"Writing to cluster {cluster} at offset {cluster_offset}")
            chunk_size = min(remaining, self.cluster_size)
            chunk = data[data_pos:data_pos + chunk_size]
            self.logger.debug(f"Writing {chunk_size} bytes to cluster")
            if chunk_size < self.cluster_size:
                self.logger.debug(f"Padding last chunk from {chunk_size} to {self.cluster_size} bytes")
                chunk = chunk + bytes(self.cluster_size - chunk_size)
            self._write_bytes(cluster_offset, chunk)
            data_pos += chunk_size
            remaining -= chunk_size

            if progress_callback and total_clusters > 0:
                progress = (i + 1) / total_clusters
                self.logger.debug(f"Write progress: {progress:.2f}")
                progress_callback(progress)

        self.logger.debug(f"Completed writing {len(data)} bytes to cluster chain")

    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[int]:
        self.logger.debug(f"Looking for free directory entry in cluster {dir_cluster}")
        if dir_cluster == 0:
            self.logger.debug("Searching root directory for free entry")
            root_dir_data = self._read_bytes(self.root_dir_start, self.boot_sector.root_entries * 32)
            for i in range(0, len(root_dir_data), 32):
                if root_dir_data[i] == 0 or root_dir_data[i] == 0xE5:
                    self.logger.debug(f"Found free entry in root directory at offset {self.root_dir_start + i}")
                    return self.root_dir_start + i
            self.logger.warning("No free entry found in root directory")
            return None
        else:
            self.logger.debug(f"Searching directory cluster chain for free entry")
            cluster_chain = self._get_cluster_chain(dir_cluster)
            for cluster in cluster_chain:
                cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
                self.logger.debug(f"Checking cluster {cluster} at offset {cluster_offset}")
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)

                for i in range(0, len(cluster_data), 32):
                    if i + 32 > len(cluster_data):
                        break
                    if cluster_data[i] == 0 or cluster_data[i] == 0xE5:
                        self.logger.debug(f"Found free entry at offset {cluster_offset + i}")
                        return cluster_offset + i

            last_cluster = cluster_chain[-1]
            new_cluster = self._find_free_cluster()
            if new_cluster is None:
                self.logger.warning("Cannot extend directory: no free clusters")
                return None

            self.logger.info(f"Extending directory by adding new cluster {new_cluster}")
            self._set_fat_entry(last_cluster, new_cluster)
            self._set_fat_entry(new_cluster, 0xFFF)
            cluster_offset = self.data_area_start + (new_cluster - 2) * self.cluster_size
            self._write_bytes(cluster_offset, bytes(self.cluster_size))
            self.logger.debug(f"Returning first entry in new cluster at offset {cluster_offset}")
            return cluster_offset

    def _create_directory_entry(self, name: str, is_dir: bool,
                            starting_cluster: int, size: int, dt: datetime.datetime) -> bytes:
        self.logger.debug(f"Creating directory entry: name='{name}', is_dir={is_dir}, "
                        f"cluster={starting_cluster}, size={size}")
        entry = bytearray(32)

        if is_dir and name in [".", ".."]:
            name_part = name.ljust(8)
            ext_part = "   "
            self.logger.debug(f"Special directory entry: {name}")
        else:
            parts = name.upper().split('.')
            name_part = parts[0].ljust(8)
            ext_part = parts[1].ljust(3) if len(parts) > 1 else "   "
            self.logger.debug(f"Regular entry: name='{name_part}', ext='{ext_part}'")

        attr = 0x10 if is_dir else 0x00
        time_val = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
        date_val = (((dt.year - 1980) & 0x7F) << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)
        self.logger.debug(f"Date/time: {dt} -> time=0x{time_val:04X}, date=0x{date_val:04X}")

        entry[0:8] = name_part.encode('cp437')
        entry[8:11] = ext_part.encode('cp437')
        entry[11] = attr
        entry[12:22] = bytes(10)
        entry[22:24] = struct.pack('<H', time_val)
        entry[24:26] = struct.pack('<H', date_val)
        entry[26:28] = struct.pack('<H', starting_cluster)
        entry[28:32] = struct.pack('<I', size)

        self.logger.debug(f"Created directory entry ({len(entry)} bytes)")
        return bytes(entry)

    def _is_valid_83_name(self, name: str) -> bool:
        self.logger.debug(f"Validating 8.3 filename: '{name}'")
        if isinstance(name, str):
            name = name.split('/')[-1]

        invalid_chars = '"*/:<>?\\|+,;=[]'
        if any(c in invalid_chars for c in name):
            self.logger.warning(f"Invalid characters in name '{name}'")
            return False

        parts = name.split('.')
        if len(parts) > 2 or not parts[0]:
            self.logger.warning(f"Invalid name structure: {parts}")
            return False

        name_part = parts[0]
        ext_part = parts[1] if len(parts) == 2 else ""

        # --- FIX: Add checks for trailing/leading dots ---
        if name_part.endswith('.') or (ext_part and ext_part.endswith('.')):
            self.logger.warning(f"Name or extension ends with a dot: '{name}'")
            return False
        if name_part.startswith('.') or (ext_part and ext_part.startswith('.')):
            self.logger.warning(f"Name or extension starts with a dot: '{name}'")
            return False # Technically allowed by some OS, but often problematic
        # --- END FIX ---

        # Check name and extension lengths
        if len(name_part) > 8 or len(ext_part) > 3:
            self.logger.warning(f"Name or extension too long: {parts}")
            return False

        reserved = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                    "LPT1", "LPT2", "LPT3", "LPT4"]
        if parts[0].upper() in reserved:
            self.logger.warning(f"Reserved name: {parts[0].upper()}")
            return False

        self.logger.debug(f"Valid 8.3 filename: '{name}'")
        return True
