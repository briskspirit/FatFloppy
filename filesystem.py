# filesystem.py

import datetime
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union, Callable

from disk import Disk

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
        self.data = sector_data

    def is_valid(self) -> bool:
        return len(self.data) >= 512 and struct.unpack_from('<H', self.data, 0x1FE)[0] == 0xAA55

class FATBootSector(BootSector):
    def __init__(self, sector_data: bytes):
        super().__init__(sector_data)
        self.parse_bpb()

    def parse_bpb(self) -> None:
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
        except:
            self.volume_label = "NO NAME"
            self.fs_type = "FAT12"

    def is_valid(self) -> bool:
        return (super().is_valid() and
                self.bytes_per_sector in [128, 256, 512, 1024, 2048, 4096] and
                self.sectors_per_cluster in [1, 2, 4, 8, 16, 32, 64, 128] and
                self.total_sectors > 0 and self.sectors_per_fat > 0)

    def get_fat_type(self) -> str:
        root_dir_sectors = (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        fat_sectors = self.num_fats * self.sectors_per_fat
        data_sectors = self.total_sectors - (self.reserved_sectors + fat_sectors + root_dir_sectors)
        total_clusters = data_sectors // self.sectors_per_cluster

        if total_clusters < 4085:
            return "FAT12"
        elif total_clusters < 65525:
            return "FAT16"
        else:
            return "FAT32"

class Filesystem:
    def __init__(self, disk: Disk):
        self.disk = disk

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
        self._init_completed = False
        self.boot_sector = self._read_boot_sector()
        self._cached_allocated_clusters = None

        if self.is_valid():
            self._initialize_filesystem_parameters()
            self._init_completed = True

    def is_valid(self) -> bool:
        return isinstance(self.boot_sector, FATBootSector) and self.boot_sector.is_valid()

    def _read_boot_sector(self) -> Union[FATBootSector, None]:
        try:
            # Try direct read if driver supports it
            if hasattr(self.disk.driver, 'read_bytes_direct'):
                boot_sector_data = self.disk.driver.read_bytes_direct(0, 512)
            else:
                boot_sector_data = self.disk.read_sector(0, 0, 1)

            return FATBootSector(boot_sector_data)
        except Exception as e:
            print(f"Error reading boot sector: {e}")
            return None

    def _initialize_filesystem_parameters(self) -> None:
        """Initialize filesystem parameters based on boot sector"""
        # Only run this once
        if self._init_completed:
            return

        bpb = self.boot_sector

        # Basic parameters
        self.cluster_size = bpb.sectors_per_cluster * bpb.bytes_per_sector

        # Calculate important offsets
        self.fat_start = (bpb.reserved_sectors + bpb.hidden_sectors) * bpb.bytes_per_sector

        # Root directory follows the FATs
        fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start = self.fat_start + (bpb.num_fats * fat_size_bytes)

        # Root directory size
        self.root_dir_sectors = (bpb.root_entries * 32 + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        root_dir_size = self.root_dir_sectors * bpb.bytes_per_sector

        # Data area follows the root directory
        self.data_area_start = self.root_dir_start + root_dir_size

        # Calculate number of data clusters
        data_sectors = bpb.total_sectors - (bpb.reserved_sectors +
                                        bpb.num_fats * bpb.sectors_per_fat +
                                        self.root_dir_sectors)
        self.num_clusters = data_sectors // bpb.sectors_per_cluster

        self.fat_type = bpb.get_fat_type()

    def list_directory(self, path: str = "/") -> List[FileInfo]:
        if not self.is_valid():
            return []

        if path == "/":
            results = self._list_root_directory()
        else:
            dir_entry = self._find_path(path)
            if not dir_entry or not dir_entry.is_dir:
                return []
            results = self._list_directory_by_cluster(dir_entry.starting_cluster)

        # Filter out . and .. entries
        return [entry for entry in results if entry.name not in [".", ".."]]

    def _list_root_directory(self) -> List[FileInfo]:
        entries = []

        root_dir_data = self._read_bytes(self.root_dir_start, self.boot_sector.root_entries * 32)
        for i in range(0, len(root_dir_data), 32):
            # Stop at end of directory marker
            if root_dir_data[i] == 0x00:
                break

            entry_data = root_dir_data[i:i+32]
            entry = self._parse_directory_entry(entry_data)
            if entry:
                entries.append(entry)

        return entries

    def _list_directory_by_cluster(self, cluster: int) -> List[FileInfo]:
        entries = []

        if cluster == 0:
            return self._list_root_directory()

        if cluster < 2:
            print(f"WARNING: Invalid directory cluster {cluster}")
            return entries

        cluster_chain = self._get_cluster_chain(cluster)
        if not cluster_chain:
            return entries

        # Calculate total bytes to read
        total_bytes = len(cluster_chain) * self.cluster_size
        start_offset = self.data_area_start + (cluster_chain[0] - 2) * self.cluster_size
        data = self._read_bytes(start_offset, total_bytes)
        # Process all entries
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
        return entries

    def _parse_directory_entry(self, entry_data):
        # Check for valid entry data
        if len(entry_data) < 32:
            return None

        # Check for end of directory marker or deleted entry
        first_byte = entry_data[0]
        if first_byte == 0x00:  # End of directory
            return None
        if first_byte == 0xE5:  # Deleted entry
            return None

        # Check for special attribute flags
        attr = entry_data[11]
        if attr & 0x08:  # Volume label
            return None
        if attr & 0x0F == 0x0F:  # Long filename entry
            return None

        # Validate the entry
        try:
            # Get name and extension
            name = entry_data[0:8].decode('cp437').strip()
            ext = entry_data[8:11].decode('cp437').strip()

            # Create full name
            full_name = f"{name}.{ext}" if ext else name

            # Check for valid characters
            invalid_chars = set('"*/:<>?\\|')
            if any(c < ' ' or c in invalid_chars for c in full_name):
                return None

            is_dir = bool(attr & 0x10)
            size = struct.unpack('<I', entry_data[28:32])[0]
            starting_cluster = struct.unpack('<H', entry_data[26:28])[0]

            # Validate directory entries
            if is_dir and starting_cluster < 2 and name not in [".", ".."]:
                return None

            # Parse date and time
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
                dt = datetime.datetime(1980, 1, 1, 0, 0, 0)

            # Parse attributes
            attributes = []
            if attr & 0x01: attributes.append("RO")
            if attr & 0x02: attributes.append("H")
            if attr & 0x04: attributes.append("S")
            if attr & 0x20: attributes.append("A")
            attr_str = " ".join(attributes) if attributes else "-"

            # Create the file entry
            entry = FileInfo(
                name=full_name,
                size=0 if is_dir else size,
                is_dir=is_dir,
                datetime=dt,
                attributes=attr_str,
                starting_cluster=starting_cluster
            )
            return entry
        except Exception as e:
            return None

    def read_file(self, path: str, progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        file_entry = self._find_path(path)
        if not file_entry or file_entry.is_dir:
            raise ValueError(f"File not found: {path}")

        if file_entry.starting_cluster < 2:
            return b''

        cluster_chain = self._get_cluster_chain(file_entry.starting_cluster)
        file_data = self._read_cluster_chain(cluster_chain, progress_callback)

        return file_data[:file_entry.size]

    def write_file(self, path: str, data: bytes,
                  progress_callback: Optional[Callable[[float], None]] = None) -> None:
        if not self._is_valid_83_name(path):
            raise ValueError("Invalid 8.3 filename")

        parent_path, file_name = self._split_path(path)
        parent_entry = self._find_path(parent_path)

        if not parent_entry:
            if parent_path == "/":
                parent_cluster = 0  # Root directory
            else:
                raise ValueError(f"Parent directory not found: {parent_path}")
        elif not parent_entry.is_dir:
            raise ValueError(f"Not a directory: {parent_path}")
        else:
            parent_cluster = parent_entry.starting_cluster

        # Check if file already exists
        for entry in self.list_directory(parent_path):
            if entry.name.upper() == file_name.upper():
                # Delete existing file
                self.delete(path)
                break

        # Allocate clusters for file data
        num_clusters_needed = (len(data) + self.cluster_size - 1) // self.cluster_size
        if num_clusters_needed == 0:
            num_clusters_needed = 1

        clusters = self._allocate_cluster_chain(num_clusters_needed)
        if not clusters:
            raise ValueError("Not enough free space on disk")

        # Write file data to clusters
        self._write_cluster_chain(clusters, data, progress_callback)

        # Create directory entry
        now = datetime.datetime.now()
        entry = self._create_directory_entry(file_name, False, clusters[0], len(data), now)

        # Find free directory entry slot
        entry_offset = self._find_free_directory_entry(parent_cluster)
        if entry_offset is None:
            raise ValueError("No space in directory")

        # Write directory entry
        self._write_bytes(entry_offset, entry)

        # Invalidate the cached allocated clusters
        self._cached_allocated_clusters = None

        self.disk.flush()

    def create_directory(self, path: str) -> None:
        parent_path, dir_name = self._split_path(path)

        if not self._is_valid_83_name(dir_name):
            raise ValueError("Invalid 8.3 directory name")

        parent_entry = self._find_path(parent_path)
        if not parent_entry and parent_path != "/":
            raise ValueError(f"Parent directory not found: {parent_path}")

        if parent_entry and not parent_entry.is_dir:
            raise ValueError(f"Not a directory: {parent_path}")

        parent_cluster = 0 if parent_path == "/" else parent_entry.starting_cluster

        # Check if directory already exists
        for entry in self.list_directory(parent_path):
            if entry.name.upper() == dir_name.upper():
                if entry.is_dir:
                    return  # Directory already exists
                raise ValueError(f"File with same name exists: {path}")

        # Allocate a cluster for the new directory
        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            raise ValueError("No free clusters available")

        self._set_fat_entry(new_cluster, 0xFFF)  # Mark as end of chain

        # Zero out the new cluster
        cluster_offset = self.data_area_start + (new_cluster - 2) * self.cluster_size
        self._write_bytes(cluster_offset, b'\x00' * self.cluster_size)

        # Create . and .. entries
        now = datetime.datetime.now()
        dot_entry = self._create_directory_entry(".", True, new_cluster, 0, now)
        dotdot_entry = self._create_directory_entry("..", True, parent_cluster, 0, now)

        # Write . and .. entries to the new directory
        self._write_bytes(cluster_offset, dot_entry)
        self._write_bytes(cluster_offset + 32, dotdot_entry)

        # Create directory entry in parent
        dir_entry = self._create_directory_entry(dir_name, True, new_cluster, 0, now)

        # Find free directory entry slot in parent
        entry_offset = self._find_free_directory_entry(parent_cluster)
        if entry_offset is None:
            # Undo cluster allocation
            self._set_fat_entry(new_cluster, 0)
            raise ValueError("No space in parent directory")

        # Write directory entry to parent
        self._write_bytes(entry_offset, dir_entry)

        # Invalidate the cached allocated clusters
        self._cached_allocated_clusters = None

        self.disk.flush()

    def delete(self, path: str) -> None:
        if path == "/":
            raise ValueError("Cannot delete root directory")

        parent_path, name = self._split_path(path)
        parent_entry = self._find_path(parent_path)

        if not parent_entry and parent_path != "/":
            raise ValueError(f"Parent directory not found: {parent_path}")

        parent_cluster = 0 if parent_path == "/" else parent_entry.starting_cluster

        # Find the entry to delete
        entry_to_delete = None
        entry_offset = None

        if parent_cluster == 0:
            # Search in root directory
            root_dir_data = self._read_bytes(self.root_dir_start, self.boot_sector.root_entries * 32)
            for i in range(0, len(root_dir_data), 32):
                entry_data = root_dir_data[i:i+32]
                if entry_data[0] == 0:
                    break

                if entry_data[0] != 0xE5:  # Not a deleted entry
                    entry = self._parse_directory_entry(entry_data)
                    if entry and entry.name.upper() == name.upper():
                        entry_to_delete = entry
                        entry_offset = self.root_dir_start + i
                        break
        else:
            # Search in directory cluster chain
            cluster_chain = self._get_cluster_chain(parent_cluster)
            for cluster in cluster_chain:
                cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)

                for i in range(0, len(cluster_data), 32):
                    entry_data = cluster_data[i:i+32]
                    if entry_data[0] == 0:
                        break

                    if entry_data[0] != 0xE5:  # Not a deleted entry
                        entry = self._parse_directory_entry(entry_data)
                        if entry and entry.name.upper() == name.upper():
                            entry_to_delete = entry
                            entry_offset = cluster_offset + i
                            break

                if entry_to_delete:
                    break

        if not entry_to_delete or entry_offset is None:
            raise ValueError(f"Item not found: {path}")

        # If it's a directory, check if it's empty
        if entry_to_delete.is_dir:
            dir_entries = self._list_directory_by_cluster(entry_to_delete.starting_cluster)
            non_dot_entries = [e for e in dir_entries if e.name not in [".", ".."]]
            if non_dot_entries:
                raise ValueError(f"Directory not empty: {path}")

        # Mark entry as deleted
        self._write_bytes(entry_offset, b'\xE5' + self._read_bytes(entry_offset + 1, 31))

        # Free cluster chain
        if entry_to_delete.starting_cluster >= 2:
            self._free_cluster_chain(entry_to_delete.starting_cluster)

        # Invalidate the cached allocated clusters
        self._cached_allocated_clusters = None

        self.disk.flush()

    def get_allocated_clusters(self) -> List[int]:
        """Returns a list of allocated cluster numbers with caching for performance."""
        if not self.is_valid():
            return []

        # Use cached value if available
        if self._cached_allocated_clusters is not None:
            return self._cached_allocated_clusters

        allocated_clusters = []
        try:
            for cluster in range(2, self.num_clusters + 2):
                fat_entry = self._read_fat_entry(cluster)
                # If entry is not 0 (free) and not bad cluster marker
                if fat_entry != 0 and fat_entry < 0xFF0:
                    allocated_clusters.append(cluster)
                # Also include end-of-chain markers
                elif fat_entry >= 0xFF8 and fat_entry <= 0xFFF:
                    allocated_clusters.append(cluster)
        except Exception as e:
            print(f"Error getting allocated clusters: {e}")

        # Cache the result
        self._cached_allocated_clusters = allocated_clusters
        return allocated_clusters

    def get_free_space(self) -> Tuple[int, int]:
        """Returns (free_bytes, total_bytes) for the filesystem."""
        if not self.is_valid():
            return (0, 0)

        try:
            # Calculate total disk space
            total_bytes = self.boot_sector.total_sectors * self.boot_sector.bytes_per_sector

            # Count free clusters - use the allocated clusters for efficiency
            allocated_clusters = self.get_allocated_clusters()
            total_clusters = self.num_clusters
            free_clusters = total_clusters - len(allocated_clusters)

            free_bytes = free_clusters * self.cluster_size

            return (free_bytes, total_bytes)
        except Exception as e:
            print(f"Error calculating free space: {e}")
            return (0, total_bytes)

    def _find_path(self, path: str) -> Optional[FileInfo]:
        if path == "/" or path == "":
            return None  # Root directory has no entry

        parts = path.strip("/").split("/")

        # Start with the root directory
        current_cluster = 0  # 0 represents the root directory
        current_entry = None

        for part in parts:
            # Get entries directly based on the cluster
            if current_cluster == 0:
                entries = self._list_root_directory()
            else:
                entries = self._list_directory_by_cluster(current_cluster)

            found = False
            for entry in entries:
                if entry.name.upper() == part.upper():
                    if not entry.is_dir and part != parts[-1]:
                        return None  # Not a directory in the path
                    current_entry = entry
                    current_cluster = entry.starting_cluster
                    found = True
                    break

            if not found:
                return None

        return current_entry

    def _split_path(self, path: str) -> Tuple[str, str]:
        path = path.rstrip("/")
        if "/" not in path:
            return "/", path

        parent_path = path[:path.rindex("/")]
        if not parent_path:
            parent_path = "/"

        name = path[path.rindex("/")+1:]

        return parent_path, name

    def _read_bytes(self, offset: int, length: int) -> bytes:
        # Calculate sector addresses
        sector_size = self.boot_sector.bytes_per_sector
        start_sector = offset // sector_size
        end_sector = (offset + length - 1) // sector_size

        # Optimize for single sector reads
        if start_sector == end_sector:
            cylinder, head, sector = self._lba_to_chs(start_sector)
            sector_data = self.disk.read_sector(cylinder, head, sector)
            sector_offset = offset % sector_size
            return sector_data[sector_offset:sector_offset + length]

        # Multi-sector reading
        data = bytearray()
        current_offset = offset
        remaining_length = length

        # Read sectors in larger blocks when possible
        while remaining_length > 0:
            sector_num = current_offset // sector_size
            sector_offset = current_offset % sector_size

            # Determine how many contiguous sectors to read
            sectors_to_read = 1
            bytes_in_first_sector = sector_size - sector_offset

            if remaining_length > bytes_in_first_sector:
                # Calculate additional full sectors needed
                additional_sectors = (remaining_length - bytes_in_first_sector + sector_size - 1) // sector_size
                sectors_to_read += additional_sectors

            # Read one sector at a time - could be optimized for drivers that support multi-sector reads
            cylinder, head, sector = self._lba_to_chs(sector_num)
            sector_data = self.disk.read_sector(cylinder, head, sector)

            bytes_to_read = min(remaining_length, sector_size - sector_offset)
            data.extend(sector_data[sector_offset:sector_offset + bytes_to_read])

            current_offset += bytes_to_read
            remaining_length -= bytes_to_read

        return bytes(data)

    def _write_bytes(self, offset: int, data: bytes) -> None:
        if not data:
            return

        # Calculate sector addresses
        sector_size = self.boot_sector.bytes_per_sector
        start_sector = offset // sector_size
        end_sector = (offset + len(data) - 1) // sector_size

        # Optimize for single sector writes
        if start_sector == end_sector:
            sector_offset = offset % sector_size

            # If we're writing a partial sector, read the current sector first
            cylinder, head, sector = self._lba_to_chs(start_sector)
            sector_data = bytearray(self.disk.read_sector(cylinder, head, sector))

            # Update the sector data
            sector_data[sector_offset:sector_offset + len(data)] = data

            # Write the updated sector
            self.disk.write_sector(cylinder, head, sector, sector_data)
            return

        # Multi-sector writing
        current_offset = offset
        data_pos = 0

        while data_pos < len(data):
            sector_num = current_offset // sector_size
            sector_offset = current_offset % sector_size

            cylinder, head, sector = self._lba_to_chs(sector_num)

            # If we're writing a partial sector, read the current sector first
            sector_data = bytearray(self.disk.read_sector(cylinder, head, sector))

            # Calculate how much data to write to this sector
            bytes_to_write = min(len(data) - data_pos, sector_size - sector_offset)

            # Update the sector data
            sector_data[sector_offset:sector_offset + bytes_to_write] = data[data_pos:data_pos + bytes_to_write]

            # Write the updated sector
            self.disk.write_sector(cylinder, head, sector, sector_data)

            current_offset += bytes_to_write
            data_pos += bytes_to_write

    def _lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        sectors_per_track = self.boot_sector.sectors_per_track
        heads = self.boot_sector.num_heads

        sector = (lba % sectors_per_track) + 1
        temp = lba // sectors_per_track
        head = temp % heads
        cylinder = temp // heads

        return cylinder, head, sector

    def _read_fat_entry(self, cluster: int) -> int:
        """Read a FAT12 entry for the given cluster number"""
        if self.fat_type == "FAT12":
            # FAT12 entries take 1.5 bytes per cluster
            # For cluster N, the entry starts at byte position N*3/2
            byte_offset = (cluster * 3) // 2
            fat_offset = self.fat_start + byte_offset

            # Read 2 bytes (16 bits) - enough to get our 12-bit entry
            value_bytes = self._read_bytes(fat_offset, 2)

            # Extract the 12-bit value based on whether it's odd or even cluster
            value = struct.unpack("<H", value_bytes)[0]

            if cluster % 2 == 0:  # Even cluster - use low 12 bits
                fat_value = value & 0x0FFF
            else:  # Odd cluster - use high 12 bits
                fat_value = value >> 4

            return fat_value
        else:
            raise NotImplementedError("Only FAT12 is supported")

    def _set_fat_entry(self, cluster: int, value: int) -> None:
        if self.fat_type == "FAT12":
            value &= 0x0FFF  # Ensure 12-bit value

            for fat_num in range(self.boot_sector.num_fats):
                fat_offset = self.fat_start + (fat_num * self.boot_sector.sectors_per_fat * self.boot_sector.bytes_per_sector)
                offset = fat_offset + int(cluster * 1.5)

                if cluster % 2 == 0:
                    # Even cluster: update the low 12 bits
                    value_bytes = self._read_bytes(offset, 2)
                    current_value = struct.unpack('<H', value_bytes)[0]
                    new_value = (current_value & 0xF000) | value
                    self._write_bytes(offset, struct.pack('<H', new_value))
                else:
                    # Odd cluster: update the high 12 bits
                    value_bytes = self._read_bytes(offset, 2)
                    current_value = struct.unpack('<H', value_bytes)[0]
                    new_value = (current_value & 0x000F) | (value << 4)
                    self._write_bytes(offset, struct.pack('<H', new_value))
        else:
            # FAT16/FAT32 not implemented
            raise NotImplementedError("Only FAT12 is supported")

    def _get_cluster_chain(self, start_cluster: int) -> List[int]:
        """Get the list of clusters in a chain starting from start_cluster"""
        # Special case for root directory
        if start_cluster == 0:
            return []

        # Basic validation
        if start_cluster < 2:
            print(f"WARNING: Invalid starting cluster {start_cluster}")
            return []

        chain = []
        cluster = start_cluster

        # Follow the cluster chain
        max_length = min(1000, self.num_clusters)  # Reasonable limit
        while cluster >= 2 and cluster < 0xFF0 and len(chain) < max_length:
            chain.append(cluster)

            # Get next cluster
            next_cluster = self._read_fat_entry(cluster)

            # End of chain?
            if next_cluster >= 0xFF0:
                break

            # Detect circular chains
            if next_cluster in chain:
                print(f"WARNING: Circular reference detected at cluster {next_cluster}")
                break

            cluster = next_cluster

        return chain

    def _find_free_cluster(self) -> Optional[int]:
        for cluster in range(2, self.num_clusters + 2):
            if self._read_fat_entry(cluster) == 0:
                return cluster

        return None

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[List[int]]:
        if num_clusters <= 0:
            return []

        clusters = []
        prev_cluster = None

        for _ in range(num_clusters):
            cluster = self._find_free_cluster()
            if cluster is None:
                # Not enough free clusters, free what we've allocated so far
                for c in clusters:
                    self._set_fat_entry(c, 0)
                return None

            clusters.append(cluster)

            if prev_cluster is not None:
                self._set_fat_entry(prev_cluster, cluster)

            prev_cluster = cluster

        # Mark the last cluster as end of chain
        if prev_cluster is not None:
            self._set_fat_entry(prev_cluster, 0xFFF)

        return clusters

    def _free_cluster_chain(self, start_cluster: int) -> None:
        cluster = start_cluster

        while cluster >= 2 and cluster < 0xFF0:
            next_cluster = self._read_fat_entry(cluster)
            self._set_fat_entry(cluster, 0)  # Mark as free

            if next_cluster >= 0xFF0:
                break

            cluster = next_cluster

    def _read_cluster_chain(self, cluster_chain: List[int],
                          progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        result = bytearray()
        total_clusters = len(cluster_chain)

        for i, cluster in enumerate(cluster_chain):
            cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
            cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
            result.extend(cluster_data)

            if progress_callback and total_clusters > 0:
                progress_callback((i + 1) / total_clusters)

        return bytes(result)

    def _write_cluster_chain(self, cluster_chain: List[int], data: bytes,
                          progress_callback: Optional[Callable[[float], None]] = None) -> None:
        remaining = len(data)
        data_pos = 0
        total_clusters = len(cluster_chain)

        for i, cluster in enumerate(cluster_chain):
            cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size

            # Calculate how much data to write to this cluster
            chunk_size = min(remaining, self.cluster_size)
            chunk = data[data_pos:data_pos + chunk_size]

            # If chunk is smaller than cluster size, pad with zeros
            if chunk_size < self.cluster_size:
                chunk = chunk + bytes(self.cluster_size - chunk_size)

            # Write cluster data
            self._write_bytes(cluster_offset, chunk)

            data_pos += chunk_size
            remaining -= chunk_size

            if progress_callback and total_clusters > 0:
                progress_callback((i + 1) / total_clusters)

    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[int]:
        if dir_cluster == 0:
            # Root directory
            root_dir_data = self._read_bytes(self.root_dir_start, self.boot_sector.root_entries * 32)
            for i in range(0, len(root_dir_data), 32):
                if root_dir_data[i] == 0 or root_dir_data[i] == 0xE5:
                    return self.root_dir_start + i

            return None
        else:
            # Regular directory
            cluster_chain = self._get_cluster_chain(dir_cluster)
            for cluster in cluster_chain:
                cluster_offset = self.data_area_start + (cluster - 2) * self.cluster_size
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)

                for i in range(0, len(cluster_data), 32):
                    if i + 32 > len(cluster_data):
                        break

                    if cluster_data[i] == 0 or cluster_data[i] == 0xE5:
                        return cluster_offset + i

            # No free entry found, try to extend the directory
            last_cluster = cluster_chain[-1]
            new_cluster = self._find_free_cluster()

            if new_cluster is None:
                return None

            # Update FAT chain
            self._set_fat_entry(last_cluster, new_cluster)
            self._set_fat_entry(new_cluster, 0xFFF)

            # Clear new cluster
            cluster_offset = self.data_area_start + (new_cluster - 2) * self.cluster_size
            self._write_bytes(cluster_offset, bytes(self.cluster_size))

            return cluster_offset

    def _create_directory_entry(self, name: str, is_dir: bool,
                            starting_cluster: int, size: int, dt: datetime.datetime) -> bytes:
        entry = bytearray(32)

        # Handle special directory names
        if is_dir and name in [".", ".."]:
            name_part = name.ljust(8)
            ext_part = "   "
        else:
            # Split name into name and extension parts
            parts = name.upper().split('.')
            name_part = parts[0].ljust(8)
            ext_part = parts[1].ljust(3) if len(parts) > 1 else "   "

        # Attribute byte
        attr = 0x10 if is_dir else 0x00

        # Time and date
        time_val = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
        date_val = (((dt.year - 1980) & 0x7F) << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)

        # Fill in the entry fields
        entry[0:8] = name_part.encode('cp437')
        entry[8:11] = ext_part.encode('cp437')
        entry[11] = attr
        entry[12:22] = bytes(10)  # Reserved
        entry[22:24] = struct.pack('<H', time_val)
        entry[24:26] = struct.pack('<H', date_val)
        entry[26:28] = struct.pack('<H', starting_cluster)
        entry[28:32] = struct.pack('<I', size)

        return bytes(entry)

    def _is_valid_83_name(self, name: str) -> bool:
        if isinstance(name, str):
            name = name.split('/')[-1]  # Get just the filename from a path

        # Check against invalid characters
        invalid_chars = '"*/:<>?\\|+,;=[]'
        if any(c in invalid_chars for c in name):
            return False

        # Check name structure
        parts = name.split('.')
        if len(parts) > 2 or not parts[0]:
            return False

        # Check name and extension lengths
        if len(parts[0]) > 8 or (len(parts) == 2 and len(parts[1]) > 3):
            return False

        # Check against reserved names
        reserved = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                    "LPT1", "LPT2", "LPT3", "LPT4"]
        if parts[0].upper() in reserved:
            return False

        return True
