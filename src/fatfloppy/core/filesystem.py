# src/fatfloppy/core/filesystem.py
import re
import struct
import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .utils.logging_config import get_logger
from .disk import Disk

logger = get_logger("FATFilesystem") # More specific logger name

# FAT constants
FAT12_MAX_CLUSTERS = 4084 # Technically < 4085
FAT12_EOC = 0xFFF
FAT12_EOC_MIN = 0xFF8 # Standard minimum EOC value
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
    """Represents information about a file or directory entry."""
    name: str
    size: int
    is_dir: bool
    datetime: datetime.datetime
    attributes: str
    starting_cluster: int = 0


class BootSector:
    """Basic Boot Sector structure."""
    def __init__(self, sector_data: bytes):
        self.logger = get_logger(f"{self.__class__.__name__}")
        self.data = sector_data
        if len(sector_data) < 512:
             self.logger.warning(f"Boot sector data too short: {len(sector_data)} bytes")
             # Pad with zeros if needed for checks, though validity will likely fail
             self.data += bytes(512 - len(sector_data))
        self.logger.debug(f"BootSector initialized with {len(self.data)} bytes")

    def is_valid(self) -> bool:
        """Check for the 0xAA55 signature."""
        # Ensure data is long enough before unpacking
        if len(self.data) < 512:
            self.logger.warning("Cannot check signature, data too short.")
            return False
        signature = struct.unpack_from('<H', self.data, 0x1FE)[0]
        valid = signature == 0xAA55
        self.logger.debug(f"Boot sector signature (0xAA55): {'valid' if valid else f'invalid (found 0x{signature:04X})'}")
        return valid


class FATBootSector(BootSector):
    """FAT-specific Boot Sector with BPB (BIOS Parameter Block) parsing."""
    def __init__(self, sector_data: bytes):
        super().__init__(sector_data)
        self._parse_bpb()

    def _parse_bpb(self) -> None:
        """Parses the BIOS Parameter Block from the boot sector data."""
        self.logger.debug("Parsing BPB structure")
        try:
            # Standard BPB fields (FAT12/16/32 common)
            self.bytes_per_sector = struct.unpack_from('<H', self.data, 0x00B)[0]
            self.sectors_per_cluster = self.data[0x00D]
            self.reserved_sectors = struct.unpack_from('<H', self.data, 0x00E)[0]
            self.num_fats = self.data[0x010]
            self.root_entries = struct.unpack_from('<H', self.data, 0x011)[0] # Max root entries for FAT12/16
            self.total_sectors_16 = struct.unpack_from('<H', self.data, 0x013)[0] # Used if total_sectors_32 is 0
            self.media_descriptor = self.data[0x015]
            self.sectors_per_fat_16 = struct.unpack_from('<H', self.data, 0x016)[0] # Used for FAT12/16

            # DOS 3.0+ BPB fields
            self.sectors_per_track = struct.unpack_from('<H', self.data, 0x018)[0]
            self.num_heads = struct.unpack_from('<H', self.data, 0x01A)[0]
            self.hidden_sectors = struct.unpack_from('<I', self.data, 0x01C)[0]
            self.total_sectors_32 = struct.unpack_from('<I', self.data, 0x020)[0] # Used if total_sectors_16 is 0

            # Determine total sectors and sectors per FAT
            self.total_sectors = self.total_sectors_32 if self.total_sectors_16 == 0 else self.total_sectors_16
            self.sectors_per_fat = self.sectors_per_fat_16 # For FAT12/16

            # Extended BPB (FAT12/16)
            self.drive_number = self.data[0x024]
            # self.reserved1 = self.data[0x025] # Often current head or flags
            self.boot_signature = self.data[0x026] # Should be 0x28 or 0x29
            self.volume_id = struct.unpack_from('<I', self.data, 0x027)[0]

            # Fields depend on Boot Signature (0x28 or 0x29)
            label_offset = 0x02B
            fs_type_offset = 0x036
            label_len = 11
            fs_type_len = 8

            try:
                self.volume_label = self.data[label_offset : label_offset + label_len].decode('cp437').strip()
                self.fs_type_raw = self.data[fs_type_offset : fs_type_offset + fs_type_len]
                self.fs_type = self.fs_type_raw.decode('cp437').strip()
            except UnicodeDecodeError:
                 self.logger.warning("Could not decode volume label/FS type using cp437.")
                 self.volume_label = "INVALID"
                 self.fs_type = "INVALID"
            except Exception as e:
                self.logger.warning(f"Error parsing volume label or fs_type: {e}", exc_info=True)
                self.volume_label = "NO NAME"
                self.fs_type = "UNKNOWN" # Default, will be determined later

            self.logger.debug(f"BPB Parsed: Bytes/Sector={self.bytes_per_sector}, Sec/Cluster={self.sectors_per_cluster}")
            self.logger.debug(f"           Reserved Sec={self.reserved_sectors}, Num FATs={self.num_fats}, Root Entries={self.root_entries}")
            self.logger.debug(f"           Total Sec={self.total_sectors}, Sec/FAT={self.sectors_per_fat}")
            self.logger.debug(f"           Sec/Track={self.sectors_per_track}, Heads={self.num_heads}, Hidden Sec={self.hidden_sectors}")
            self.logger.debug(f"           Volume Label='{self.volume_label}', FS Type='{self.fs_type}' (raw: {self.fs_type_raw})")

        except struct.error as e:
            self.logger.error(f"Structure unpack error during BPB parsing: {e}", exc_info=True)
            # Set defaults to prevent crashes later, although is_valid() should fail
            self.bytes_per_sector = 0
            self.sectors_per_cluster = 0
            self.total_sectors = 0
            self.sectors_per_fat = 0
            self.num_heads = 0
            self.sectors_per_track = 0
            raise ValueError("Failed to parse critical BPB fields.") from e
        except IndexError as e:
            self.logger.error(f"Index error during BPB parsing (data likely too short): {e}", exc_info=True)
            self.bytes_per_sector = 0
            self.sectors_per_cluster = 0
            self.total_sectors = 0
            self.sectors_per_fat = 0
            self.num_heads = 0
            self.sectors_per_track = 0
            raise ValueError("Boot sector data too short for BPB parsing.") from e
        except Exception as e:
            self.logger.error(f"Unexpected error parsing BPB: {e}", exc_info=True)
            # Assign defaults cautiously
            self.bytes_per_sector = getattr(self, 'bytes_per_sector', 0)
            self.sectors_per_cluster = getattr(self, 'sectors_per_cluster', 0)
            self.total_sectors = getattr(self, 'total_sectors', 0)
            self.sectors_per_fat = getattr(self, 'sectors_per_fat', 0)
            self.num_heads = getattr(self, 'num_heads', 0)
            self.sectors_per_track = getattr(self, 'sectors_per_track', 0)
            raise # Re-raise the caught exception

    def is_valid(self) -> bool:
        """Validates the BPB parameters for plausible values."""
        if not super().is_valid():
            return False

        # Check critical values parsed
        if not hasattr(self, 'bytes_per_sector') or not hasattr(self, 'sectors_per_cluster') \
           or not hasattr(self, 'total_sectors') or not hasattr(self, 'sectors_per_fat'):
             self.logger.warning("BPB members missing, validation failed.")
             return False

        valid_bytes_per_sector = self.bytes_per_sector in [512, 1024, 2048, 4096] # Common values
        valid_sectors_per_cluster = self.sectors_per_cluster in [1, 2, 4, 8, 16, 32, 64, 128] and (self.sectors_per_cluster & (self.sectors_per_cluster - 1) == 0) # Power of 2
        valid_total_sectors = self.total_sectors > 0
        valid_sectors_per_fat = self.sectors_per_fat > 0
        valid_num_fats = self.num_fats in [1, 2] # Typically 1 or 2
        valid_reserved_sectors = self.reserved_sectors >= 1

        valid = (valid_bytes_per_sector and valid_sectors_per_cluster and
                 valid_total_sectors and valid_sectors_per_fat and
                 valid_num_fats and valid_reserved_sectors)

        self.logger.debug(f"BPB parameter validation: {'valid' if valid else 'invalid'}")
        if not valid:
            self.logger.warning(f"Invalid BPB values detected:")
            if not valid_bytes_per_sector: self.logger.warning(f"  - Bytes/Sector: {self.bytes_per_sector}")
            if not valid_sectors_per_cluster: self.logger.warning(f"  - Sectors/Cluster: {self.sectors_per_cluster}")
            if not valid_total_sectors: self.logger.warning(f"  - Total Sectors: {self.total_sectors}")
            if not valid_sectors_per_fat: self.logger.warning(f"  - Sectors/FAT: {self.sectors_per_fat}")
            if not valid_num_fats: self.logger.warning(f"  - Num FATs: {self.num_fats}")
            if not valid_reserved_sectors: self.logger.warning(f"  - Reserved Sectors: {self.reserved_sectors}")
        return valid

    def calculate_fat_type(self) -> str:
        """Calculates the FAT type based on cluster count."""
        if self.bytes_per_sector == 0 or self.sectors_per_cluster == 0:
            self.logger.error("Cannot calculate FAT type: Invalid BPB parameters (bytes/sector or sec/cluster is zero).")
            return "UNKNOWN"

        root_dir_bytes = self.root_entries * 32
        root_dir_sectors = (root_dir_bytes + self.bytes_per_sector - 1) // self.bytes_per_sector
        fat_sectors = self.num_fats * self.sectors_per_fat
        # Use hidden_sectors if it seems reasonable (e.g., not excessively large)
        # A large hidden_sectors value might indicate a partition table offset, not relevant here.
        first_data_sector = self.reserved_sectors + fat_sectors + root_dir_sectors
        if 0 < self.hidden_sectors < self.total_sectors / 2: # Heuristic for sanity check
             # If BPB hidden_sectors likely refers to sectors *before* the VBR
             # Adjust calculation if necessary. For simple floppy images, hidden_sectors is usually 0.
             # Let's assume hidden_sectors means sectors preceding *this* partition/volume
             # data_sectors = self.total_sectors - first_data_sector # Assuming total_sectors refers to the volume size
             pass # No adjustment typically needed for floppy BPBs

        data_sectors = self.total_sectors - first_data_sector

        if data_sectors <= 0:
            self.logger.warning(f"Calculated data sectors is zero or negative ({data_sectors}). Cannot determine FAT type.")
            return "UNKNOWN"

        # Handle potential division by zero if sectors_per_cluster is invalid
        if self.sectors_per_cluster == 0:
             self.logger.error("Cannot calculate FAT type: sectors_per_cluster is zero.")
             return "UNKNOWN"

        total_clusters = data_sectors // self.sectors_per_cluster

        if total_clusters <= FAT12_MAX_CLUSTERS:
            fat_type = "FAT12"
        elif total_clusters < 65525: # FAT16 max clusters
            fat_type = "FAT16"
        else:
            fat_type = "FAT32" # Not expected for this project, but for completeness

        self.logger.debug(f"Calculated FAT type: {fat_type} ({total_clusters} clusters, {data_sectors} data sectors)")
        return fat_type


class Filesystem:
    """Abstract base class for filesystem implementations."""
    def __init__(self, disk: Disk):
        self.logger = get_logger(self.__class__.__name__)
        self.disk = disk
        self.logger.debug("Filesystem base class initialized")

    def is_valid(self) -> bool:
        raise NotImplementedError("Subclasses must implement is_valid")

    def list_directory(self, path: str) -> List[FileInfo]:
        raise NotImplementedError("Subclasses must implement list_directory")

    def read_file(self, path: str) -> bytes:
        raise NotImplementedError("Subclasses must implement read_file")

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError("Subclasses must implement write_file")

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("Subclasses must implement create_directory")

    def delete(self, path: str) -> None:
        raise NotImplementedError("Subclasses must implement delete")

    def get_allocated_clusters(self) -> List[int]:
         raise NotImplementedError("Subclasses must implement get_allocated_clusters")

    def get_free_space(self) -> Tuple[int, int]:
         raise NotImplementedError("Subclasses must implement get_free_space")


class FATFilesystem(Filesystem):
    """Implementation for FAT12 filesystem."""
    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.logger = get_logger(self.__class__.__name__)
        self._init_completed = False
        self.boot_sector: Optional[FATBootSector] = None
        self._cached_allocated_clusters: Optional[List[int]] = None
        self.fat_cache: Optional[bytearray] = None # In-memory cache for the primary FAT
        self.fat_dirty: bool = False # Track if FAT cache has been modified

        self._load_boot_sector()
        if self.boot_sector and self.boot_sector.is_valid():
            try:
                self._initialize_filesystem_parameters()
                # Pre-load FAT cache for efficiency
                self._load_fat_cache()
                self._init_completed = True
                self.logger.info(f"Valid {self.fat_type} filesystem initialized")
            except Exception as e:
                self.logger.error(f"Failed to initialize filesystem parameters: {e}", exc_info=True)
                self._init_completed = False # Ensure it's marked as not completed
        else:
            self.logger.warning("Invalid or unrecognized filesystem based on boot sector")

    def is_valid(self) -> bool:
        """Checks if the filesystem appears to be a valid FAT volume."""
        valid = self._init_completed
        self.logger.debug(f"Filesystem validation check: {'valid' if valid else 'invalid'}")
        return valid

    def list_directory(self, path: str = "/") -> List[FileInfo]:
        """Lists the contents of a directory."""
        if not self.is_valid():
            self.logger.warning("Cannot list directory: Invalid or uninitialized filesystem")
            return []

        self.logger.debug(f"Listing directory: {path}")
        path = self._normalize_path(path)

        if path == "/":
            results = self._list_directory_by_cluster(0) # 0 represents root for FAT12/16
        else:
            dir_entry_info = self._find_path(path)
            if not dir_entry_info or not dir_entry_info.is_dir:
                self.logger.warning(f"Directory not found or not a directory: {path}")
                return []
            results = self._list_directory_by_cluster(dir_entry_info.starting_cluster)

        # Filter out LFN entries, volume label, ., and .. before returning
        filtered_results = [
            entry for entry in results
            if entry.name not in [".", ".."] and not (entry.attributes and "LFN" in entry.attributes)
               and not (entry.attributes and "VOL" in entry.attributes)
        ]
        self.logger.debug(f"Found {len(filtered_results)} user items in directory {path}")
        return filtered_results

    def read_file(self, path: str) -> bytes:
        """Reads the contents of a file."""
        if not self.is_valid():
            error_msg = f"Cannot read file {path}: Invalid or uninitialized filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Reading file: {path}")
        path = self._normalize_path(path)
        file_entry_info = self._find_path(path)

        if not file_entry_info:
            error_msg = f"File not found: {path}"
            self.logger.error(error_msg)
            raise FileNotFoundError(error_msg) # Use FileNotFoundError
        if file_entry_info.is_dir:
             error_msg = f"Path is a directory, not a file: {path}"
             self.logger.error(error_msg)
             raise IsADirectoryError(error_msg) # Use IsADirectoryError

        if file_entry_info.size == 0:
             self.logger.debug(f"File {path} has size 0, returning empty content")
             return b''

        # Cluster 0 or 1 are invalid for file data
        if file_entry_info.starting_cluster < 2:
            self.logger.warning(f"File {path} has invalid starting cluster {file_entry_info.starting_cluster}, but size > 0. Returning empty content.")
            return b''

        try:
            cluster_chain = self._get_cluster_chain(file_entry_info.starting_cluster)
            if not cluster_chain:
                 self.logger.warning(f"File {path} has starting cluster {file_entry_info.starting_cluster} but no cluster chain found. Returning empty content.")
                 return b''

            self.logger.debug(f"File {path} uses {len(cluster_chain)} clusters.")
            file_data = self._read_cluster_chain_data(cluster_chain)

            # Truncate to the actual file size specified in the directory entry
            result = file_data[:file_entry_info.size]
            self.logger.info(f"Read {len(result)} bytes from file {path}")
            return result
        except Exception as e:
            self.logger.error(f"Error reading file {path}: {e}", exc_info=True)
            raise IOError(f"Failed to read file {path}") from e

    def write_file(self, path: str, data: bytes) -> None:
        """Writes data to a file, overwriting if it exists."""
        if not self.is_valid():
            error_msg = f"Cannot write file {path}: Invalid or uninitialized filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        path = self._normalize_path(path)
        self.logger.debug(f"Attempting to write {len(data)} bytes to file: {path}")
        parent_path, file_name = self._split_path(path)

        if not self._is_valid_83_filename(file_name):
            error_msg = f"Invalid 8.3 filename: '{file_name}'"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Find parent directory cluster
        parent_dir_cluster = self._get_directory_cluster(parent_path)

        # Check if file exists and delete it first
        try:
             existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, file_name)
             if existing_entry:
                  if existing_entry.is_dir:
                       raise IsADirectoryError(f"Cannot overwrite directory with file: {path}")
                  self.logger.info(f"File '{file_name}' exists in '{parent_path}', deleting it first.")
                  self.delete(path) # Use self.delete to handle freeing clusters etc.
        except FileNotFoundError:
             self.logger.debug(f"File '{file_name}' does not exist in '{parent_path}'. Good.")
             pass # File doesn't exist, proceed

        # Calculate clusters needed (handle 0-byte files needing 1 cluster for entry, 0 for data)
        # FAT requires a starting cluster in the dir entry even for 0-byte files if created this way.
        # Let's allocate 0 clusters for 0-byte files for simplicity, and handle the starting_cluster=0 case.
        num_clusters_needed = 0
        start_cluster_for_entry = 0 # Default for 0-byte file
        if len(data) > 0:
            num_clusters_needed = (len(data) + self.cluster_size - 1) // self.cluster_size
            self.logger.debug(f"Allocating {num_clusters_needed} clusters for file '{path}' data")
            clusters = self._allocate_cluster_chain(num_clusters_needed)
            if not clusters:
                error_msg = "Not enough free space on disk"
                self.logger.error(error_msg)
                raise IOError(error_msg) # Use IOError for disk space issues
            start_cluster_for_entry = clusters[0]
            self.logger.debug(f"Data will start at cluster {start_cluster_for_entry}")

            # Write data to clusters
            self.logger.debug(f"Writing data to allocated clusters")
            self._write_cluster_chain_data(clusters, data)
        else:
             self.logger.debug("File is 0 bytes, no clusters needed for data.")
             clusters = []


        # Find free directory entry slot
        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            self.logger.error(f"No space in directory '{parent_path}' to create entry for '{file_name}'")
            # Free allocated clusters if we couldn't create the entry
            if clusters:
                self.logger.warning(f"Freeing {len(clusters)} allocated clusters due to directory full error.")
                self._free_cluster_chain(clusters[0]) # Free the chain we just allocated
                self._commit_fat() # Commit the freeing operation
            raise IOError(f"No space in directory {parent_path}")

        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)

        # Create and write directory entry
        now = datetime.datetime.now()
        self.logger.debug(f"Creating directory entry for '{file_name}'")
        entry_bytes = self._create_directory_entry_bytes(
            name=file_name,
            is_dir=False,
            starting_cluster=start_cluster_for_entry,
            size=len(data),
            dt=now
        )

        self.logger.debug(f"Writing directory entry at offset {entry_disk_offset}")
        self._write_bytes(entry_disk_offset, entry_bytes)

        # Commit FAT changes (if any clusters were allocated/freed)
        self._commit_fat()
        self.disk.flush() # Flush underlying disk driver buffer
        self.logger.info(f"Successfully wrote file '{path}' ({len(data)} bytes in {len(clusters)} clusters)")


    def create_directory(self, path: str) -> None:
        """Creates a new directory."""
        if not self.is_valid():
            error_msg = f"Cannot create directory {path}: Invalid or uninitialized filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        path = self._normalize_path(path)
        self.logger.debug(f"Creating directory: {path}")
        parent_path, dir_name = self._split_path(path)

        if not self._is_valid_83_filename(dir_name, allow_extension=False):
             error_msg = f"Invalid 8.3 directory name: '{dir_name}'"
             self.logger.error(error_msg)
             raise ValueError(error_msg)

        # Find parent directory cluster
        parent_dir_cluster = self._get_directory_cluster(parent_path)

        # Check if name already exists
        try:
             existing_entry, _ = self._find_entry_in_directory(parent_dir_cluster, dir_name)
             if existing_entry:
                  if existing_entry.is_dir:
                       self.logger.info(f"Directory '{path}' already exists.")
                       return # Nothing to do
                  else:
                       raise FileExistsError(f"A file with the name '{dir_name}' already exists in '{parent_path}'")
        except FileNotFoundError:
             pass # Name is available

        # Allocate a cluster for the new directory's contents
        self.logger.debug("Allocating cluster for new directory contents")
        new_cluster = self._find_free_cluster()
        if new_cluster is None:
            error_msg = "No free clusters available to create directory"
            self.logger.error(error_msg)
            raise IOError(error_msg)

        self.logger.debug(f"Allocated cluster {new_cluster} for directory '{dir_name}'")
        # Mark cluster as allocated and end-of-chain in FAT cache
        self._set_fat_entry_cached(new_cluster, FAT12_EOC)

        # Initialize the new directory cluster (zero it out)
        cluster_offset = self._cluster_to_offset(new_cluster)
        self.logger.debug(f"Initializing directory cluster {new_cluster} at offset {cluster_offset}")
        self._write_bytes(cluster_offset, b'\x00' * self.cluster_size)

        # Create '.' and '..' entries within the new directory's cluster
        now = datetime.datetime.now()
        self.logger.debug("Creating '.' and '..' entries in new directory")
        dot_entry = self._create_directory_entry_bytes(".", True, new_cluster, 0, now)
        # Parent cluster 0 means root directory for '..' in FAT12/16
        dotdot_entry = self._create_directory_entry_bytes("..", True, parent_dir_cluster if parent_dir_cluster > 0 else 0, 0, now)

        self.logger.debug("Writing '.' and '..' entries")
        self._write_bytes(cluster_offset, dot_entry)
        self._write_bytes(cluster_offset + 32, dotdot_entry)

        # Find free slot in parent directory for the new directory's entry
        entry_location = self._find_free_directory_entry(parent_dir_cluster)
        if entry_location is None:
            self.logger.error(f"No space in parent directory '{parent_path}' for new directory '{dir_name}'")
            # Free the cluster we allocated for the directory contents
            self.logger.warning(f"Freeing allocated cluster {new_cluster} due to parent directory full.")
            self._set_fat_entry_cached(new_cluster, 0) # Free in cache
            self._commit_fat() # Write FAT changes
            raise IOError(f"No space in parent directory {parent_path}")

        parent_entry_dir_cluster, parent_entry_offset_in_cluster = entry_location
        parent_entry_disk_offset = self._get_offset_for_directory_entry(parent_entry_dir_cluster, parent_entry_offset_in_cluster)

        # Create the actual directory entry in the parent directory
        self.logger.debug(f"Creating directory entry for '{dir_name}' in parent directory")
        dir_entry_bytes = self._create_directory_entry_bytes(dir_name, True, new_cluster, 0, now)

        self.logger.debug(f"Writing directory entry at offset {parent_entry_disk_offset}")
        self._write_bytes(parent_entry_disk_offset, dir_entry_bytes)

        # Commit FAT changes and flush disk
        self._commit_fat()
        self.disk.flush()
        self.logger.info(f"Successfully created directory '{path}' using cluster {new_cluster}")


    def delete(self, path: str) -> None:
        """Deletes a file or an empty directory."""
        if not self.is_valid():
            error_msg = f"Cannot delete {path}: Invalid or uninitialized filesystem"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        path = self._normalize_path(path)
        if path == "/":
            error_msg = "Cannot delete root directory"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Attempting to delete item: {path}")
        parent_path, name = self._split_path(path)

        # Find parent directory cluster
        parent_dir_cluster = self._get_directory_cluster(parent_path)

        # Find the entry in the parent directory
        entry_to_delete, entry_location = self._find_entry_in_directory(parent_dir_cluster, name)
        # find_entry_in_directory raises FileNotFoundError if not found

        dir_cluster_num, entry_offset_in_cluster = entry_location
        entry_disk_offset = self._get_offset_for_directory_entry(dir_cluster_num, entry_offset_in_cluster)

        self.logger.debug(f"Found item to delete: '{entry_to_delete.name}', is_dir={entry_to_delete.is_dir}, "
                        f"cluster={entry_to_delete.starting_cluster}, at offset {entry_disk_offset}")

        # If it's a directory, check if it's empty (contains only '.' and '..')
        if entry_to_delete.is_dir:
            self.logger.debug(f"Checking if directory '{path}' is empty")
            dir_contents = self._list_directory_by_cluster(entry_to_delete.starting_cluster)
            # Correctly filter for non '.'/'..' entries, including hidden/system files
            non_dot_entries = [
                e for e in dir_contents if e.name not in ['.', '..']
            ]
            if non_dot_entries:
                error_msg = f"Directory not empty: {path} (contains {len(non_dot_entries)} other items)"
                self.logger.error(error_msg)
                # Optionally list first few items:
                # self.logger.error(f"  Example items: {[e.name for e in non_dot_entries[:3]]}")
                raise OSError(error_msg) # Use OSError for "Directory not empty"
            self.logger.debug("Directory is empty, proceeding with deletion.")

        # Mark the directory entry as deleted (first byte = 0xE5)
        self.logger.debug(f"Marking entry as deleted at offset {entry_disk_offset}")
        self._write_bytes(entry_disk_offset, bytes([ENTRY_DELETED])) # Write only the first byte

        # Free the cluster chain associated with the file/directory (if any)
        if entry_to_delete.starting_cluster >= 2:
            self.logger.debug(f"Freeing cluster chain starting at {entry_to_delete.starting_cluster}")
            self._free_cluster_chain(entry_to_delete.starting_cluster)
        else:
             self.logger.debug("No data clusters to free (starting cluster < 2).")

        # Commit FAT changes and flush disk
        self._commit_fat()
        self.disk.flush()

        # Invalidate cluster cache as allocation changed
        self._cached_allocated_clusters = None

        self.logger.info(f"Successfully deleted '{path}'")


    def get_allocated_clusters(self) -> List[int]:
        """Returns a list of cluster numbers that are currently allocated."""
        if not self.is_valid():
            self.logger.warning("Cannot get allocated clusters: Invalid or uninitialized filesystem")
            return []

        # Use cache if available and FAT is not dirty
        if self._cached_allocated_clusters is not None and not self.fat_dirty:
            self.logger.debug(f"Returning cached allocated clusters ({len(self._cached_allocated_clusters)} clusters)")
            return self._cached_allocated_clusters

        if self.fat_cache is None:
             self.logger.warning("Cannot get allocated clusters: FAT cache not loaded.")
             return []

        allocated_clusters = []
        try:
            self.logger.debug(f"Scanning FAT cache for allocated clusters (range 2 to {self.num_clusters + 1})")
            # Iterate clusters 2 through N+1 (inclusive)
            for cluster in range(2, self.num_clusters + 2):
                # Read directly from the cache for speed
                fat_entry_value = self._read_fat_entry_cached(cluster, load_if_missing=False) # Don't reload here
                # Check if cluster is not free (0)
                if fat_entry_value != 0:
                    allocated_clusters.append(cluster)

            self.logger.info(f"Found {len(allocated_clusters)} allocated clusters by scanning FAT cache")
            self._cached_allocated_clusters = allocated_clusters
            self.logger.debug("Updated allocated clusters cache.")
            return allocated_clusters
        except Exception as e:
            self.logger.error(f"Error scanning FAT cache for allocated clusters: {e}", exc_info=True)
            self._cached_allocated_clusters = None # Invalidate cache on error
            return []


    def get_free_space(self) -> Tuple[int, int]:
        """Returns tuple of (free_bytes, total_data_bytes)."""
        if not self.is_valid():
            self.logger.warning("Cannot get free space: Invalid or uninitialized filesystem")
            return (0, 0)

        try:
            # Total user-addressable space is based on number of clusters
            total_data_bytes = self.num_clusters * self.cluster_size
            self.logger.debug(f"Total data clusters: {self.num_clusters}, Cluster size: {self.cluster_size} bytes")
            self.logger.debug(f"Total theoretical data space: {total_data_bytes} bytes")

            # Get count of free clusters (total - allocated)
            allocated_count = len(self.get_allocated_clusters()) # Uses caching
            free_clusters = self.num_clusters - allocated_count
            if free_clusters < 0:
                 self.logger.warning(f"Calculated free clusters is negative ({free_clusters}). Clamping to 0.")
                 free_clusters = 0

            free_bytes = free_clusters * self.cluster_size

            percent_free = (free_bytes / total_data_bytes * 100) if total_data_bytes > 0 else 0
            self.logger.info(f"Free space: {free_bytes} bytes ({free_bytes/1024:.1f}KB) / {total_data_bytes/1024:.1f}KB ({percent_free:.1f}%) - {free_clusters} free clusters")

            return (free_bytes, total_data_bytes)
        except Exception as e:
            self.logger.error(f"Error calculating free space: {e}", exc_info=True)
            # Try to return total based on geometry if calculation failed
            total_geom_bytes = 0
            if self.disk and self.disk.geometry:
                 total_geom_bytes = self.disk.geometry.total_bytes
            return (0, total_geom_bytes)


    # --- Internal Helper Methods ---

    def _normalize_path(self, path: str) -> str:
        """Normalizes a path string (e.g., remove trailing slash, ensure leading slash)."""
        if not path:
             return "/"
        path = path.replace("\\", "/") # Convert backslashes
        if len(path) > 1:
            path = path.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _split_path(self, path: str) -> Tuple[str, str]:
        """Splits a normalized path into parent path and final component."""
        path = self._normalize_path(path)
        if path == "/":
             return "/", "" # Root has no parent name component

        last_slash_index = path.rfind('/')
        if last_slash_index == 0: # Item in root dir, e.g., /FILE.TXT
            parent_path = "/"
            name = path[1:]
        else: # Item in subdir, e.g., /DIR/FILE.TXT
            parent_path = path[:last_slash_index]
            name = path[last_slash_index + 1:]

        self.logger.debug(f"Split path '{path}' -> parent='{parent_path}', name='{name}'")
        return parent_path, name

    def _load_boot_sector(self) -> None:
        """Reads and parses the boot sector."""
        try:
            self.logger.debug("Reading boot sector (Sector 0)")
            # Always read sector 0 (C=0, H=0, S=1 for CHS)
            # Use direct read if available for potential speedup on images
            if hasattr(self.disk.driver, 'read_bytes_direct'):
                self.logger.debug("Using driver's direct read method for boot sector")
                # Read more than 512 if possible, in case of larger sector sizes, but parse first 512
                boot_sector_data = self.disk.driver.read_bytes_direct(0, 512) # Read at least 512
            else:
                self.logger.debug("Using disk's sector read method for boot sector")
                boot_sector_data = self.disk.read_sector(0, 0, 1)

            if not boot_sector_data:
                 self.logger.error("Failed to read boot sector: No data returned.")
                 self.boot_sector = None
                 return

            self.boot_sector = FATBootSector(boot_sector_data)
            self.logger.debug("Boot sector read and parsed.")

        except FileNotFoundError: # Handles case where disk.read_sector fails cleanly
             self.logger.error(f"Could not read boot sector (C=0, H=0, S=1). Disk might be unreadable or unformatted.")
             self.boot_sector = None
        except Exception as e:
            self.logger.error(f"Error reading or parsing boot sector: {e}", exc_info=True)
            self.boot_sector = None

    def _initialize_filesystem_parameters(self) -> None:
        """Calculates filesystem layout based on BPB values."""
        if not self.boot_sector or not self.boot_sector.is_valid():
            raise ValueError("Cannot initialize filesystem parameters: Invalid Boot Sector / BPB")

        self.logger.debug("Initializing filesystem parameters from BPB")
        bpb = self.boot_sector

        # Validate essential parameters again before using them
        if bpb.bytes_per_sector == 0 or bpb.sectors_per_cluster == 0 or bpb.num_fats == 0:
            raise ValueError("Invalid BPB parameters (zero values) prevent initialization.")

        self.cluster_size = bpb.sectors_per_cluster * bpb.bytes_per_sector
        self.logger.debug(f"Cluster size: {self.cluster_size} bytes")

        # Calculate FAT start offset
        # hidden_sectors typically refers to sectors *before* the VBR in partitioned media.
        # For floppies, this is usually 0. If non-zero, it might indicate the LBA of the VBR itself.
        # Assuming VBR is at LBA 0 for standard floppy images unless hidden_sectors is meaningful.
        # A large value might indicate a partition table structure, which we ignore here.
        vbr_lba = 0
        if 0 < bpb.hidden_sectors < bpb.total_sectors / 2: # Heuristic for sanity
            self.logger.info(f"Using hidden_sectors ({bpb.hidden_sectors}) as VBR LBA offset.")
            vbr_lba = bpb.hidden_sectors
        elif bpb.hidden_sectors > 0:
            self.logger.warning(f"Ignoring unreasonable hidden_sectors value: {bpb.hidden_sectors}")

        self.fat_start_offset = (vbr_lba + bpb.reserved_sectors) * bpb.bytes_per_sector
        self.logger.debug(f"FAT area starts at byte offset: {self.fat_start_offset}")

        # Calculate Root Directory start offset (for FAT12/16)
        self.fat_size_bytes = bpb.sectors_per_fat * bpb.bytes_per_sector
        self.root_dir_start_offset = self.fat_start_offset + (bpb.num_fats * self.fat_size_bytes)
        self.logger.debug(f"Root directory starts at byte offset: {self.root_dir_start_offset}")

        # Calculate Root Directory size (for FAT12/16)
        self.root_dir_bytes = bpb.root_entries * 32
        self.root_dir_sectors = (self.root_dir_bytes + bpb.bytes_per_sector - 1) // bpb.bytes_per_sector
        self.logger.debug(f"Root directory size: {self.root_dir_bytes} bytes ({self.root_dir_sectors} sectors)")

        # Calculate Data Area start offset
        self.data_area_start_offset = self.root_dir_start_offset + self.root_dir_bytes
        self.logger.debug(f"Data area (cluster #2) starts at byte offset: {self.data_area_start_offset}")

        # Calculate total number of clusters
        first_data_sector_lba = (self.data_area_start_offset + bpb.bytes_per_sector -1) // bpb.bytes_per_sector
        # Note: BPB total_sectors includes *all* sectors (boot, FATs, root, data)
        total_data_sectors = bpb.total_sectors - first_data_sector_lba
        if total_data_sectors < 0:
             self.logger.error(f"Calculated negative data sectors ({total_data_sectors}). BPB likely corrupt.")
             total_data_sectors = 0 # Avoid division errors

        self.num_clusters = total_data_sectors // bpb.sectors_per_cluster
        self.logger.debug(f"Total data sectors: {total_data_sectors}")
        self.logger.debug(f"Total clusters: {self.num_clusters}")

        # Determine FAT type
        self.fat_type = bpb.calculate_fat_type()
        if self.fat_type != "FAT12":
             # This implementation primarily targets FAT12
             self.logger.warning(f"Detected FAT type is {self.fat_type}, but this class focuses on FAT12.")
             # Raise error if definitely not FAT12, maybe? For now, just warn.
             # raise ValueError(f"Unsupported FAT type: {self.fat_type}")


        # Basic sanity check on calculated offsets
        if self.data_area_start_offset > bpb.total_sectors * bpb.bytes_per_sector:
             raise ValueError("Calculated data area offset exceeds total disk size based on BPB.")

        self._init_completed = True
        self.logger.debug("Filesystem parameters initialized successfully.")

    def _cluster_to_offset(self, cluster: int) -> int:
        """Converts a cluster number (2 or higher) to its byte offset on the disk."""
        if cluster < 2:
            raise ValueError(f"Cannot calculate offset for invalid cluster number: {cluster}")
        offset = self.data_area_start_offset + (cluster - 2) * self.cluster_size
        # self.logger.debug(f"Cluster {cluster} maps to offset {offset}")
        return offset

    def _get_directory_cluster(self, dir_path: str) -> int:
        """Finds the starting cluster number for a given directory path."""
        dir_path = self._normalize_path(dir_path)
        if dir_path == "/":
            return 0 # Root directory represented by cluster 0 for FAT12/16

        dir_entry_info = self._find_path(dir_path)
        if not dir_entry_info:
            raise FileNotFoundError(f"Directory path not found: {dir_path}")
        if not dir_entry_info.is_dir:
            raise NotADirectoryError(f"Path is not a directory: {dir_path}")

        self.logger.debug(f"Directory '{dir_path}' starts at cluster {dir_entry_info.starting_cluster}")
        return dir_entry_info.starting_cluster

    def _list_directory_by_cluster(self, cluster: int) -> List[FileInfo]:
        """Reads and parses directory entries starting from a given cluster (0 for root)."""
        entries = []
        self.logger.debug(f"Reading directory entries from cluster {cluster}")

        if cluster == 0: # Root Directory (FAT12/16)
            if self.root_dir_bytes == 0:
                 self.logger.warning("Root directory has 0 size according to BPB.")
                 return []
            try:
                dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
                self.logger.debug(f"Read {len(dir_data)} bytes for root directory")
                entries = self._parse_directory_data(dir_data)
            except Exception as e:
                self.logger.error(f"Error reading root directory data: {e}", exc_info=True)
                return []
        elif cluster >= 2: # Subdirectory
            try:
                cluster_chain = self._get_cluster_chain(cluster)
                if not cluster_chain:
                     self.logger.warning(f"Directory cluster chain for start cluster {cluster} is empty or invalid.")
                     return []
                self.logger.debug(f"Directory cluster chain: {cluster_chain}")
                dir_data = self._read_cluster_chain_data(cluster_chain)
                self.logger.debug(f"Read {len(dir_data)} bytes for directory in cluster {cluster}")
                entries = self._parse_directory_data(dir_data)
            except Exception as e:
                self.logger.error(f"Error reading directory data from cluster {cluster}: {e}", exc_info=True)
                return []
        else:
            self.logger.error(f"Invalid cluster number for directory listing: {cluster}")
            return []

        self.logger.debug(f"Parsed {len(entries)} entries from cluster {cluster}")
        return entries

    def _parse_directory_data(self, dir_data: bytes) -> List[FileInfo]:
        """Parses a raw block of directory data into FileInfo objects."""
        entries = []
        for i in range(0, len(dir_data), 32):
            entry_data = dir_data[i:i+32]
            if len(entry_data) < 32:
                 self.logger.warning(f"Short directory entry data at offset {i}, stopping parse.")
                 break # Stop processing if entry data is short

            first_byte = entry_data[0]

            if first_byte == ENTRY_UNUSED: # 0x00: End of directory marker
                # self.logger.debug(f"Found end of directory marker (0x00) at index {i}")
                break
            if first_byte == ENTRY_DELETED: # 0xE5: Deleted entry
                # self.logger.debug(f"Skipping deleted entry (0xE5) at index {i}")
                continue

            entry = self._parse_single_directory_entry(entry_data)
            if entry:
                entries.append(entry)

        return entries

    def _parse_single_directory_entry(self, entry_data: bytes) -> Optional[FileInfo]:
        """Parses a 32-byte directory entry."""
        try:
            attributes = entry_data[11]

            # Skip Long File Name (LFN) entries and Volume Labels
            if attributes & ATTR_LONG_NAME == ATTR_LONG_NAME:
                 # self.logger.debug("Skipping LFN entry")
                 return None
            if attributes & ATTR_VOLUME_ID:
                 # Parse as volume label if needed, otherwise skip
                 try:
                      vol_name = entry_data[0:11].decode('cp437').strip()
                      # self.logger.debug(f"Found volume label entry: '{vol_name}'")
                      # We could return a special FileInfo type here if needed
                      # For now, just return None as it's not a user file/dir
                      return FileInfo(name=vol_name, size=0, is_dir=False, datetime=datetime.datetime.min, attributes="VOL", starting_cluster=0)
                 except Exception:
                      self.logger.warning("Could not parse volume label entry.")
                 return None # Skip volume label for file listing


            # Parse 8.3 filename
            base_name = entry_data[0:8].decode('cp437').rstrip()
            extension = entry_data[8:11].decode('cp437').rstrip()

            # Handle special first byte mapping for deleted files (0xE5 -> 'E')
            # and the 0x05 mapping for Japanese systems ('?' -> 0xE5)
            if entry_data[0] == 0x05:
                 base_name = '\xE5' + base_name[1:]
                 self.logger.debug("Handled 0x05 first byte in filename")

            # Construct full name
            full_name = base_name
            if extension:
                full_name = f"{base_name}.{extension}"

            # Check for invalid characters (more thoroughly)
            # Allow '.' in name only for '.' and '..' entries
            if full_name not in [".", ".."] and not self._is_valid_83_filename(full_name, allow_dots=False):
                 self.logger.warning(f"Parsed entry has invalid 8.3 name: '{full_name}'. Skipping.")
                 # Optionally attempt recovery or log hex dump of name
                 return None

            is_dir = bool(attributes & ATTR_DIRECTORY)
            size = struct.unpack('<I', entry_data[28:32])[0]
            starting_cluster = struct.unpack('<H', entry_data[26:28])[0]

            # Sanity check: directories should have size 0
            if is_dir and size != 0:
                self.logger.warning(f"Directory entry '{full_name}' has non-zero size ({size}). Setting size to 0.")
                size = 0
             # Sanity check: files/dirs (except root's '..') need cluster >= 2 if not empty
            if size > 0 and starting_cluster < 2:
                 self.logger.warning(f"Entry '{full_name}' (size {size}) has invalid starting cluster {starting_cluster}. Treating as invalid.")
                 # Decide how to handle: skip, return empty file, etc. Let's skip.
                 return None
            if is_dir and starting_cluster < 2 and full_name not in [".", ".."]:
                 self.logger.warning(f"Directory entry '{full_name}' has invalid starting cluster {starting_cluster}. Skipping.")
                 return None

            # Parse date and time
            time_val = struct.unpack('<H', entry_data[22:24])[0]
            date_val = struct.unpack('<H', entry_data[24:26])[0]
            dt = self._parse_fat_datetime(date_val, time_val)

            # Parse attributes to string
            attr_list = []
            if attributes & ATTR_READ_ONLY: attr_list.append("R")
            if attributes & ATTR_HIDDEN:    attr_list.append("H")
            if attributes & ATTR_SYSTEM:    attr_list.append("S")
            if attributes & ATTR_ARCHIVE:   attr_list.append("A")
            # Indicate directory for clarity, even though is_dir is separate
            if is_dir: attr_list.append("D")

            attr_str = "".join(attr_list) if attr_list else "-" # Compact string

            entry = FileInfo(
                name=full_name,
                size=size, # Already corrected for dirs
                is_dir=is_dir,
                datetime=dt,
                attributes=attr_str,
                starting_cluster=starting_cluster
            )
            # self.logger.debug(f"Parsed entry: {entry}")
            return entry

        except UnicodeDecodeError:
             self.logger.warning(f"Failed to decode 8.3 name using cp437: {entry_data[0:11].hex()}")
             return None
        except struct.error as e:
            self.logger.error(f"Struct error parsing directory entry: {e}", exc_info=True)
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error parsing directory entry: {e}", exc_info=True)
            return None

    def _parse_fat_datetime(self, date_val: int, time_val: int) -> datetime.datetime:
        """Converts FAT date and time words into a datetime object."""
        try:
            # Time: HHHHHMMMMMMSSSSS (hours, minutes, seconds/2)
            secs = (time_val & 0x1F) * 2
            mins = (time_val >> 5) & 0x3F
            hour = (time_val >> 11) & 0x1F

            # Date: YYYYYYYMMMMDDDDD (year offset from 1980, month, day)
            day = date_val & 0x1F
            month = (date_val >> 5) & 0x0F
            year = 1980 + ((date_val >> 9) & 0x7F)

            # Basic validation
            if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= mins <= 59 and 0 <= secs <= 59):
                 raise ValueError("Invalid date/time components")

            return datetime.datetime(year, month, day, hour, mins, secs)
        except ValueError as e:
            # Log the invalid components for debugging
            self.logger.warning(f"Invalid FAT date/time value (Y:{year} M:{month} D:{day} H:{hour} m:{mins} S:{secs}). Error: {e}. Using epoch.")
            # Return a default value (e.g., Unix epoch or DOS epoch)
            return datetime.datetime(1980, 1, 1, 0, 0, 0)


    def _find_path(self, path: str) -> Optional[FileInfo]:
        """Finds the FileInfo for a given path, traversing directories."""
        path = self._normalize_path(path)
        self.logger.debug(f"Finding path: {path}")
        if path == "/":
            # Root directory doesn't have a single FileInfo entry in its parent
            # Represent it with a special case if needed, or None for finding items *within* it.
            # Let's return a synthetic entry for the root itself.
            # Note: Root dir size/date are not typically stored directly in FAT12/16 BPB
            # Get root dir datetime from Volume Label entry if available? Complex. Use default.
             root_dt = datetime.datetime(1980, 1, 1)
             # Attempt to find volume label to potentially get datetime?
             try:
                root_contents = self._list_directory_by_cluster(0)
                for item in root_contents:
                    if item.attributes == "VOL":
                        root_dt = item.datetime
                        break
             except Exception:
                 pass # Ignore errors finding volume label date
             return FileInfo(name="/", size=0, is_dir=True, datetime=root_dt, attributes="D", starting_cluster=0)

        parts = path.strip("/").split("/")
        self.logger.debug(f"Path components: {parts}")

        current_cluster = 0 # Start search from root directory
        current_entry_info = None

        for i, part_name in enumerate(parts):
            is_last_part = (i == len(parts) - 1)
            self.logger.debug(f"Searching for '{part_name}' in cluster {current_cluster}")
            entries_in_current = self._list_directory_by_cluster(current_cluster)

            found_part = False
            for entry in entries_in_current:
                # Case-insensitive comparison for FAT
                if entry.name.upper() == part_name.upper():
                    # Check if a non-last part of the path is a file
                    if not is_last_part and not entry.is_dir:
                        self.logger.error(f"Path component '{part_name}' in '{path}' is a file, not a directory.")
                        raise NotADirectoryError(f"Path component '{part_name}' is not a directory")

                    current_entry_info = entry
                    current_cluster = entry.starting_cluster
                    self.logger.debug(f"Found '{part_name}' -> leads to cluster {current_cluster}")
                    found_part = True
                    break # Stop searching this directory level

            if not found_part:
                self.logger.warning(f"Path component '{part_name}' not found in path '{path}'")
                raise FileNotFoundError(f"Path not found: {path}")

        # If loop completes, current_entry_info holds the info for the final path component
        self.logger.debug(f"Successfully found path '{path}': {current_entry_info}")
        return current_entry_info

    # --- FAT Cache and Handling ---

    def _load_fat_cache(self) -> bool:
        """Reads the primary FAT into the memory cache."""
        if self.fat_cache is not None:
            self.logger.debug("FAT cache already loaded.")
            return True
        if not self._init_completed or not self.boot_sector:
             self.logger.error("Cannot load FAT cache: Filesystem not initialized.")
             return False

        try:
            self.logger.debug(f"Reading primary FAT ({self.fat_size_bytes} bytes) from offset {self.fat_start_offset}")
            # Read only the primary FAT (FAT #0)
            self.fat_cache = bytearray(self._read_bytes(self.fat_start_offset, self.fat_size_bytes))
            self.fat_dirty = False # Cache is clean after loading
            self.logger.info(f"Loaded {len(self.fat_cache)} bytes into FAT cache (FAT #1)")
            return True
        except Exception as e:
            self.logger.error(f"Error reading FAT sectors into cache: {e}", exc_info=True)
            self.fat_cache = None # Ensure cache is None on error
            self.fat_dirty = False
            return False

    def _write_fat_sectors(self) -> None:
        """Writes the (potentially dirty) FAT cache back to all FAT copies on disk."""
        if self.fat_cache is None:
            self.logger.warning("FAT cache is not loaded, cannot write back.")
            return
        if not self.fat_dirty:
            self.logger.debug("FAT cache not modified, skipping write.")
            return
        if not self._init_completed or not self.boot_sector:
             self.logger.error("Cannot write FAT cache: Filesystem not initialized.")
             return

        try:
            fat_size_bytes = len(self.fat_cache)
            self.logger.debug(f"Writing FAT cache ({fat_size_bytes} bytes) to {self.boot_sector.num_fats} FATs on disk")
            for fat_num in range(self.boot_sector.num_fats):
                fat_offset = self.fat_start_offset + (fat_num * fat_size_bytes)
                self.logger.debug(f"Writing cache to FAT #{fat_num + 1} at offset {fat_offset}")
                self._write_bytes(fat_offset, self.fat_cache)
                self.logger.info(f"Wrote {fat_size_bytes} bytes from cache to FAT #{fat_num + 1}")

            # Mark cache as clean *after* successful write
            self.fat_dirty = False
            self.logger.debug("FAT cache marked as clean.")
            # Invalidate cluster list cache after writing FAT
            self._cached_allocated_clusters = None
            self.logger.debug("Invalidated allocated clusters cache.")

        except Exception as e:
            self.logger.error(f"Error writing FAT sectors from cache: {e}", exc_info=True)
            # Keep cache marked as dirty if write fails? Yes.
            raise IOError("Failed to write FAT cache to disk.") from e

    def _commit_fat(self) -> None:
        """Writes the FAT cache to disk if it's dirty."""
        self.logger.debug("Commit FAT requested.")
        self._write_fat_sectors() # This function now checks the dirty flag

    def _read_fat_entry_cached(self, cluster: int, load_if_missing: bool = True) -> int:
        """Reads a FAT12 entry from the in-memory cache."""
        if self.fat_cache is None:
            if load_if_missing:
                 self.logger.debug(f"FAT cache miss reading cluster {cluster}, loading FAT sectors.")
                 if not self._load_fat_cache():
                      self.logger.error(f"Cannot read FAT entry for cluster {cluster}: Cache load failed.")
                      raise IOError("Failed to load FAT cache for reading entry")
            else:
                 self.logger.error(f"Cannot read FAT entry for cluster {cluster}: Cache not loaded and load_if_missing is False.")
                 raise ValueError("FAT Cache not loaded")


        # Check cluster bounds (clusters 0 and 1 are reserved, N+2 is max)
        if not (0 <= cluster < self.num_clusters + 2):
             self.logger.warning(f"Attempted to read FAT entry for out-of-bounds cluster: {cluster} (max: {self.num_clusters + 1})")
             return FAT12_BAD_CLUSTER # Return bad cluster marker? Or raise error?

        # self.logger.debug(f"Reading FAT entry from memory for cluster {cluster}")
        if self.fat_type == "FAT12":
            byte_offset = int(cluster * 1.5)
            # Check cache bounds before reading
            if byte_offset + 1 >= len(self.fat_cache):
                 self.logger.error(f"FAT cache bounds error reading cluster {cluster} (offset {byte_offset}, cache size {len(self.fat_cache)})")
                 return FAT12_BAD_CLUSTER # Indicate an error

            # Read 2 bytes directly from cache
            try:
                 value = struct.unpack_from('<H', self.fat_cache, byte_offset)[0]
            except struct.error:
                 self.logger.error(f"Struct unpack error reading cluster {cluster} from cache at offset {byte_offset}")
                 return FAT12_BAD_CLUSTER

            if cluster % 2 == 0: # Even cluster
                fat_value = value & 0x0FFF
            else: # Odd cluster
                fat_value = value >> 4
            # self.logger.debug(f"Memory FAT entry for cluster {cluster} = 0x{fat_value:03X}")
            return fat_value
        else:
            # Add FAT16/FAT32 logic here if needed later
            raise NotImplementedError(f"Reading FAT entries for {self.fat_type} is not implemented")

    def _set_fat_entry_cached(self, cluster: int, value: int) -> None:
        """Sets a FAT12 entry in the in-memory cache and marks cache as dirty."""
        if self.fat_cache is None:
             # Try loading if not present
             self.logger.debug(f"FAT cache miss setting cluster {cluster}, loading FAT sectors.")
             if not self._load_fat_cache():
                  self.logger.error(f"Cannot set FAT entry for cluster {cluster}: Cache load failed.")
                  raise IOError("Failed to load FAT cache for setting entry")

        # Check cluster bounds (only allow setting for valid data clusters 2 to N+1)
        if not (2 <= cluster < self.num_clusters + 2):
             self.logger.error(f"Attempted to set FAT entry for invalid or reserved cluster: {cluster}")
             raise ValueError(f"Cannot set FAT entry for cluster {cluster}")

        self.logger.debug(f"Setting FAT entry in cache for cluster {cluster} to 0x{value:03X}")
        if self.fat_type == "FAT12":
            fat_value_to_set = value & 0x0FFF # Mask to 12 bits
            byte_offset = int(cluster * 1.5)

            # Check cache bounds before reading/writing
            if byte_offset + 1 >= len(self.fat_cache):
                 self.logger.error(f"FAT cache bounds error writing cluster {cluster} (offset {byte_offset}, cache size {len(self.fat_cache)})")
                 raise IndexError("FAT cache offset out of bounds")

            try:
                 # Read current 2 bytes from cache
                 current_value = struct.unpack_from('<H', self.fat_cache, byte_offset)[0]
            except struct.error:
                 self.logger.error(f"Struct unpack error reading cache at offset {byte_offset} before setting cluster {cluster}")
                 raise IOError("Failed read cache before FAT entry set")


            if cluster % 2 == 0: # Even cluster: affects lower 12 bits
                new_value = (current_value & 0xF000) | fat_value_to_set
            else: # Odd cluster: affects upper 12 bits (shifted)
                new_value = (current_value & 0x000F) | (fat_value_to_set << 4)

            # Write the modified 2 bytes back to cache
            try:
                 struct.pack_into('<H', self.fat_cache, byte_offset, new_value)
            except struct.error:
                 self.logger.error(f"Struct pack error writing cache at offset {byte_offset} for cluster {cluster}")
                 raise IOError("Failed write cache during FAT entry set")


            # Mark cache as dirty if the value actually changed
            if new_value != current_value:
                 # self.logger.debug(f"Updated memory FAT entry at offset {byte_offset} from 0x{current_value:04X} to 0x{new_value:04X}")
                 self.fat_dirty = True
                 # Invalidate cluster cache if allocation status changed (free -> non-free or vice-versa)
                 old_fat_value = (current_value & 0x0FFF) if (cluster % 2 == 0) else (current_value >> 4)
                 if (old_fat_value == 0 and fat_value_to_set != 0) or \
                    (old_fat_value != 0 and fat_value_to_set == 0):
                      self.logger.debug("Allocation status changed, invalidating _cached_allocated_clusters.")
                      self._cached_allocated_clusters = None
            # else:
            #     self.logger.debug(f"FAT entry for cluster {cluster} already had the target value, cache remains clean.")

        else:
            raise NotImplementedError(f"Setting FAT entries for {self.fat_type} is not implemented")

    # --- Cluster Chain Management ---

    def _get_cluster_chain(self, start_cluster: int) -> List[int]:
        """Gets the list of clusters in a chain, reading from FAT cache."""
        self.logger.debug(f"Getting cluster chain starting from cluster {start_cluster}")
        if start_cluster < 2: # Clusters 0 and 1 are reserved/invalid start points
            self.logger.warning(f"Invalid starting cluster for chain: {start_cluster}")
            return []

        if self.fat_cache is None and not self._load_fat_cache():
             self.logger.error("Cannot get cluster chain: FAT cache failed to load.")
             return []

        chain = []
        current_cluster = start_cluster
        # Protect against corrupt FATs leading to infinite loops
        max_chain_length = self.num_clusters + 1 # Allow chain to be full size + 1 for safety

        while len(chain) < max_chain_length:
            if not (2 <= current_cluster < self.num_clusters + 2):
                 self.logger.warning(f"Cluster chain broken: Invalid cluster number {current_cluster} encountered (started at {start_cluster}).")
                 break # Invalid cluster number

            # Check for loops *before* adding, except for the very start
            if current_cluster in chain:
                 self.logger.warning(f"Cluster chain loop detected: Cluster {current_cluster} repeated (started at {start_cluster}). Chain: {chain}")
                 break # Found a loop

            chain.append(current_cluster)

            try:
                 next_cluster = self._read_fat_entry_cached(current_cluster)
            except Exception as e:
                 self.logger.error(f"Error reading FAT entry for cluster {current_cluster} in chain: {e}")
                 break # Abort chain traversal on error

            # self.logger.debug(f"Chain step: {current_cluster} -> Next=0x{next_cluster:03X}")

            # Check for End Of Chain (EOC) marker
            if next_cluster >= FAT12_EOC_MIN and next_cluster <= FAT12_EOC:
                # self.logger.debug(f"End of chain marker (0x{next_cluster:03X}) reached at cluster {current_cluster}")
                break
            # Check for bad cluster marker
            elif next_cluster == FAT12_BAD_CLUSTER:
                self.logger.warning(f"Cluster chain broken: Bad cluster marker (0x{next_cluster:03X}) encountered after cluster {current_cluster}.")
                break
            # Check for free cluster marker (broken chain)
            elif next_cluster == 0:
                 self.logger.warning(f"Cluster chain broken: Free cluster (0) encountered after cluster {current_cluster}.")
                 break
            # Check for invalid next cluster (e.g., 1)
            elif next_cluster < 2:
                 self.logger.warning(f"Cluster chain broken: Invalid cluster value {next_cluster} encountered after cluster {current_cluster}.")
                 break


            current_cluster = next_cluster # Move to the next cluster

        if len(chain) >= max_chain_length:
             self.logger.warning(f"Cluster chain starting at {start_cluster} exceeded maximum length ({max_chain_length}). Truncating. Possible FAT corruption.")

        self.logger.debug(f"Cluster chain for start {start_cluster} contains {len(chain)} clusters.")
        return chain


    def _find_free_cluster(self) -> Optional[int]:
        """Finds the first available free cluster (value 0) in the FAT cache."""
        # self.logger.debug("Searching for a free cluster using FAT cache")
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Cannot find free cluster: FAT cache failed to load.")
            return None

        # Start searching from cluster 2 upwards
        for cluster in range(2, self.num_clusters + 2):
            try:
                # Read directly from the cache
                if self._read_fat_entry_cached(cluster, load_if_missing=False) == 0:
                    self.logger.debug(f"Found free cluster: {cluster}")
                    return cluster
            except ValueError: # Handle case where FAT cache isn't loaded yet
                if self._load_fat_cache():
                     # Retry reading after load
                     if self._read_fat_entry_cached(cluster, load_if_missing=False) == 0:
                          self.logger.debug(f"Found free cluster after loading cache: {cluster}")
                          return cluster
                else:
                     # If load failed, we can't continue searching
                     return None
            except Exception as e:
                 self.logger.error(f"Error reading FAT cache while searching for free cluster ({cluster}): {e}")
                 return None # Stop searching on error

        self.logger.warning("No free clusters found on the volume.")
        return None

    def _allocate_cluster_chain(self, num_clusters: int) -> Optional[List[int]]:
        """Allocates a chain of free clusters in the FAT cache."""
        if num_clusters <= 0:
            self.logger.debug("Allocation requested 0 clusters, returning empty list.")
            return []

        self.logger.debug(f"Attempting to allocate a chain of {num_clusters} clusters")
        if self.fat_cache is None and not self._load_fat_cache():
            self.logger.error("Cannot allocate clusters: FAT cache failed to load.")
            return None

        allocated_clusters = []
        last_allocated = None

        try:
            for i in range(num_clusters):
                free_cluster = self._find_free_cluster()
                if free_cluster is None:
                    # Allocation failed, need to roll back changes made in the cache
                    self.logger.error(f"Failed to allocate cluster {i+1} of {num_clusters}. Rolling back.")
                    if allocated_clusters:
                        self.logger.warning(f"Freeing {len(allocated_clusters)} clusters already marked in cache due to failure.")
                        # Free the chain we started building *in the cache only*
                        current = allocated_clusters[0]
                        while current is not None and current != 0 and current < FAT12_EOC_MIN:
                             next_c = self._read_fat_entry_cached(current)
                             self._set_fat_entry_cached(current, 0) # Mark as free in cache
                             if current in allocated_clusters: # Safety check
                                 allocated_clusters.remove(current)
                             if not (2 <= next_c < self.num_clusters + 2): break # Stop if next is invalid/EOC
                             current = next_c
                        # Ensure last one is also freed if loop terminated early
                        for c in allocated_clusters: self._set_fat_entry_cached(c, 0)

                    return None # Indicate failure

                # Mark the found cluster as allocated (temporarily as EOC) in the cache
                # It will be linked by the next iteration or stay EOC if it's the last one.
                self._set_fat_entry_cached(free_cluster, FAT12_EOC)
                allocated_clusters.append(free_cluster)
                # self.logger.debug(f"Allocated cluster {free_cluster} ({i+1}/{num_clusters})")

                # Link the *previous* cluster to this *newly allocated* one
                if last_allocated is not None:
                    # self.logger.debug(f"Linking cluster {last_allocated} -> {free_cluster} in cache")
                    self._set_fat_entry_cached(last_allocated, free_cluster)

                last_allocated = free_cluster

            # The last cluster remains marked as EOC from the loop.
            self.logger.info(f"Successfully allocated chain of {len(allocated_clusters)} clusters in cache: {allocated_clusters}")
            # FAT cache is now dirty and ready to be committed by the calling function.
            return allocated_clusters

        except Exception as e:
             # Catch potential errors during FAT access within the loop
             self.logger.error(f"Error during cluster allocation: {e}", exc_info=True)
             # Attempt rollback similar to the 'no free cluster' case
             if allocated_clusters:
                  self.logger.warning(f"Freeing {len(allocated_clusters)} clusters in cache due to exception.")
                  current = allocated_clusters[0]
                  while current is not None and current != 0 and current < FAT12_EOC_MIN:
                        next_c = self._read_fat_entry_cached(current)
                        self._set_fat_entry_cached(current, 0)
                        if current in allocated_clusters: allocated_clusters.remove(current)
                        if not (2 <= next_c < self.num_clusters + 2): break
                        current = next_c
                  for c in allocated_clusters: self._set_fat_entry_cached(c, 0)
             return None


    def _free_cluster_chain(self, start_cluster: int) -> None:
        """Marks a chain of clusters as free (0) in the FAT cache."""
        if start_cluster < 2:
             self.logger.warning(f"Attempted to free invalid start cluster {start_cluster}. Ignoring.")
             return

        self.logger.debug(f"Freeing cluster chain starting at cluster {start_cluster} in cache")
        if self.fat_cache is None and not self._load_fat_cache():
             self.logger.error("Cannot free cluster chain: FAT cache failed to load.")
             # Or should this raise an error? If called from delete, maybe log and continue.
             return

        current_cluster = start_cluster
        freed_count = 0
        max_iterations = self.num_clusters + 1 # Safety limit

        try:
            while freed_count < max_iterations:
                if not (2 <= current_cluster < self.num_clusters + 2):
                    self.logger.warning(f"Stopping free operation: encountered invalid cluster {current_cluster} in chain from {start_cluster}.")
                    break

                # Read the *next* cluster BEFORE freeing the current one
                next_cluster = self._read_fat_entry_cached(current_cluster)

                # Mark the current cluster as free in the cache
                # self.logger.debug(f"Marking cluster {current_cluster} as free (was -> 0x{next_cluster:03X})")
                self._set_fat_entry_cached(current_cluster, 0)
                freed_count += 1

                # Check if the next cluster indicates end of chain or invalid state
                if next_cluster == 0:
                     self.logger.warning(f"Chain unexpectedly pointed to free cluster (0) after cluster {current_cluster}. Stopping free.")
                     break
                if next_cluster < 2:
                    self.logger.warning(f"Chain unexpectedly pointed to invalid cluster ({next_cluster}) after cluster {current_cluster}. Stopping free.")
                    break
                if next_cluster >= FAT12_EOC_MIN and next_cluster <= FAT12_EOC:
                    # self.logger.debug("Reached end of chain marker.")
                    break
                if next_cluster == FAT12_BAD_CLUSTER:
                     self.logger.warning(f"Encountered bad cluster marker (0xFF7) after cluster {current_cluster}. Stopping free.")
                     break

                current_cluster = next_cluster # Move to the next

            if freed_count >= max_iterations:
                 self.logger.warning(f"Cluster chain freeing exceeded maximum iterations ({max_iterations}), possibly looped FAT. Freed {freed_count} clusters.")

            self.logger.info(f"Marked {freed_count} clusters as free in cache, starting from {start_cluster}.")
            # FAT cache is now dirty and ready to be committed by the calling function.

        except Exception as e:
             self.logger.error(f"Error during cluster chain freeing: {e}", exc_info=True)
             # State of FAT cache might be inconsistent here.


    # --- Data Read/Write ---

    def _read_bytes(self, offset: int, length: int) -> bytes:
        """Reads a sequence of bytes from the disk, potentially spanning sectors, using optimized read."""
        if length <= 0:
             return b''
        if not self.boot_sector or self.boot_sector.bytes_per_sector == 0:
             raise ValueError("Cannot read bytes: Invalid boot sector or bytes_per_sector is zero.")
        if not self.disk: # Added check for disk
             raise ValueError("Cannot read bytes: Disk object not available.")

        sector_size = self.boot_sector.bytes_per_sector
        start_lba = offset // sector_size
        end_lba = (offset + length - 1) // sector_size
        num_sectors = end_lba - start_lba + 1

        self.logger.debug(f"Reading {length} bytes from offset {offset} (LBA {start_lba} to {end_lba}, {num_sectors} sectors)")

        try:
            # Calculate CHS for the starting sector using the Disk object
            start_cyl, start_head, start_sec = self.disk.lba_to_chs(start_lba)

            # Read all necessary sectors at once using disk.read_sectors
            self.logger.debug(f"Calling disk.read_sectors: C={start_cyl} H={start_head} S={start_sec}, Num={num_sectors}")
            all_data_read = self.disk.read_sectors(start_cyl, start_head, start_sec, num_sectors)
            self.logger.debug(f"disk.read_sectors returned {len(all_data_read)} bytes")

            if len(all_data_read) < num_sectors * sector_size:
                 self.logger.warning(f"Read fewer bytes ({len(all_data_read)}) than expected ({num_sectors * sector_size}). Padding with zeros.")
                 all_data_read += bytes(num_sectors * sector_size - len(all_data_read))

            # Calculate the offset within the first sector read
            start_offset_in_read_data = offset % sector_size

            # Extract the requested slice
            result = all_data_read[start_offset_in_read_data : start_offset_in_read_data + length]
            self.logger.debug(f"Extracted {len(result)} bytes for requested range.")
            return result

        except ValueError as e: # Catch potential LBA/CHS conversion errors
            self.logger.error(f"Error calculating CHS or reading sectors: {e}", exc_info=True)
            raise IOError(f"Failed to read data at offset {offset}") from e
        except IndexError as e: # Catch potential slicing errors if read was short
             self.logger.error(f"Error slicing read data (read likely short): {e}", exc_info=True)
             # Return what we could slice, potentially less than requested
             # Re-calculate safe end point
             safe_end = min(start_offset_in_read_data + length, len(all_data_read))
             return all_data_read[start_offset_in_read_data : safe_end]
        except Exception as e:
            self.logger.error(f"Unexpected error reading bytes from offset {offset}: {e}", exc_info=True)
            raise IOError(f"Failed to read data at offset {offset}") from e


    def _write_bytes(self, offset: int, data: bytes) -> None:
        """Writes a sequence of bytes to the disk, handling partial sector writes."""
        if not data:
            self.logger.debug("Write requested with 0 bytes, nothing to do.")
            return
        if not self.boot_sector or self.boot_sector.bytes_per_sector == 0:
             raise ValueError("Cannot write bytes: Invalid boot sector or bytes_per_sector is zero.")
        if not self.disk: # Added check for disk
             raise ValueError("Cannot write bytes: Disk object not available.")

        sector_size = self.boot_sector.bytes_per_sector
        length = len(data)
        start_lba = offset // sector_size
        end_lba = (offset + length - 1) // sector_size

        self.logger.debug(f"Writing {length} bytes to offset {offset} (LBA {start_lba} to {end_lba})")

        # --- Simpler approach: Iterate and write sector by sector ---
        current_offset = offset
        data_written = 0
        try:
            while data_written < length:
                lba = current_offset // sector_size
                offset_in_sector = current_offset % sector_size
                bytes_to_write_this_sector = min(length - data_written, sector_size - offset_in_sector)

                # Calculate CHS using the Disk object
                cyl, head, sec = self.disk.lba_to_chs(lba) # CHANGED HERE
                # self.logger.debug(f"Processing write for LBA {lba} (C:{cyl} H:{head} S:{sec})")

                # If the write is partial within this sector, need read-modify-write
                if offset_in_sector > 0 or bytes_to_write_this_sector < sector_size:
                    # self.logger.debug(f"Partial write for LBA {lba}: offset={offset_in_sector}, len={bytes_to_write_this_sector}")
                    try:
                         sector_data = bytearray(self.disk.read_sector(cyl, head, sec))
                         # Check read length
                         if len(sector_data) != sector_size:
                              self.logger.warning(f"Read for modify returned {len(sector_data)} bytes, expected {sector_size}. Padding.")
                              sector_data.extend(bytes(sector_size - len(sector_data)))
                    except Exception as read_err:
                         # If read fails, maybe disk is unformatted? Create blank sector.
                         self.logger.warning(f"Read failed before partial write to LBA {lba}, using blank sector: {read_err}")
                         sector_data = bytearray(sector_size) # Zero-filled

                    # Modify the portion
                    data_chunk = data[data_written : data_written + bytes_to_write_this_sector]
                    sector_data[offset_in_sector : offset_in_sector + bytes_to_write_this_sector] = data_chunk
                    # Write the whole modified sector back
                    self.disk.write_sector(cyl, head, sec, bytes(sector_data))
                else:
                    # Write the full sector directly
                    # self.logger.debug(f"Full sector write for LBA {lba}")
                    data_chunk = data[data_written : data_written + sector_size]
                    self.disk.write_sector(cyl, head, sec, data_chunk)

                data_written += bytes_to_write_this_sector
                current_offset += bytes_to_write_this_sector

            self.logger.debug(f"Completed writing {length} bytes.")

        except Exception as e:
            self.logger.error(f"Error writing bytes starting at offset {offset}: {e}", exc_info=True)
            raise IOError(f"Failed to write data at offset {offset}") from e

    def _read_cluster_chain_data(self, cluster_chain: List[int]) -> bytes:
        """Reads the data content of a cluster chain."""
        if not cluster_chain:
            return b''

        self.logger.debug(f"Reading data from cluster chain with {len(cluster_chain)} clusters")
        result = bytearray()
        total_clusters = len(cluster_chain)

        for i, cluster in enumerate(cluster_chain):
            try:
                cluster_offset = self._cluster_to_offset(cluster)
                # self.logger.debug(f"Reading cluster {cluster} at offset {cluster_offset}")
                # Read the whole cluster using the optimized _read_bytes
                cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
                if len(cluster_data) < self.cluster_size:
                     self.logger.warning(f"Read short data for cluster {cluster} ({len(cluster_data)} bytes), padding.")
                     cluster_data += bytes(self.cluster_size - len(cluster_data))
                result.extend(cluster_data)
            except ValueError as e: # Catch offset errors
                self.logger.error(f"Error getting offset or reading cluster {cluster}: {e}")
                raise IOError(f"Failed to read data for cluster {cluster}") from e
            except Exception as e:
                self.logger.error(f"Unexpected error reading cluster {cluster} data: {e}", exc_info=True)
                raise IOError(f"Failed to read data for cluster {cluster}") from e


        self.logger.debug(f"Read total {len(result)} bytes from cluster chain")
        return bytes(result)

    def _write_cluster_chain_data(self, cluster_chain: List[int], data: bytes) -> None:
        """Writes data across a cluster chain."""
        if not cluster_chain and len(data) > 0:
             raise ValueError("Cannot write data: Cluster chain is empty but data is present.")
        if not cluster_chain and len(data) == 0:
             self.logger.debug("No clusters and no data to write.")
             return # Nothing to do

        self.logger.debug(f"Writing {len(data)} bytes to cluster chain: {cluster_chain}")
        data_pos = 0
        total_clusters = len(cluster_chain)

        for i, cluster in enumerate(cluster_chain):
            try:
                cluster_offset = self._cluster_to_offset(cluster)
                # self.logger.debug(f"Writing to cluster {cluster} at offset {cluster_offset}")

                bytes_remaining_in_data = len(data) - data_pos
                chunk_size = min(bytes_remaining_in_data, self.cluster_size)
                chunk = data[data_pos : data_pos + chunk_size]

                # Pad the chunk if it's the last part and doesn't fill the cluster
                if chunk_size < self.cluster_size:
                    # self.logger.debug(f"Padding last chunk from {chunk_size} to {self.cluster_size} bytes")
                    padded_chunk = chunk + bytes(self.cluster_size - chunk_size)
                    self._write_bytes(cluster_offset, padded_chunk)
                else:
                     self._write_bytes(cluster_offset, chunk)
                data_pos += chunk_size
                # Stop if all data is written (e.g., if chain was longer than needed)
                if data_pos >= len(data):
                    break
            except ValueError as e: # Catch offset errors
                self.logger.error(f"Error getting offset or writing cluster {cluster}: {e}")
                raise IOError(f"Failed to write data for cluster {cluster}") from e
            except Exception as e:
                self.logger.error(f"Unexpected error writing cluster {cluster} data: {e}", exc_info=True)
                raise IOError(f"Failed to write data for cluster {cluster}") from e


        if data_pos < len(data):
            self.logger.warning(f"Finished writing to cluster chain, but only {data_pos} of {len(data)} bytes were written. Chain too short?")
        else:
            self.logger.debug(f"Completed writing {data_pos} bytes to cluster chain")

    # --- Directory Entry Management ---

    def _find_entry_in_directory(self, dir_cluster: int, name_to_find: str) -> Tuple[FileInfo, Tuple[int, int]]:
        """
        Finds a specific entry by name within a directory cluster chain.
        Returns (FileInfo, (dir_cluster_num, entry_offset_in_cluster))
        Raises FileNotFoundError if not found.
        """
        self.logger.debug(f"Searching for entry '{name_to_find}' in directory cluster {dir_cluster}")
        name_upper = name_to_find.upper()

        if dir_cluster == 0: # Root directory
            if self.root_dir_bytes == 0: raise FileNotFoundError(f"Entry '{name_to_find}' not found in empty root.")
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            for i in range(0, len(dir_data), 32):
                entry_data = dir_data[i:i+32]
                if len(entry_data) < 32: break
                if entry_data[0] == ENTRY_UNUSED: break
                if entry_data[0] == ENTRY_DELETED: continue
                entry = self._parse_single_directory_entry(entry_data)
                if entry and entry.name.upper() == name_upper:
                    self.logger.debug(f"Found '{name_to_find}' in root at offset {i}")
                    return entry, (0, i) # Return cluster 0 and offset
            raise FileNotFoundError(f"Entry '{name_to_find}' not found in root directory.")

        elif dir_cluster >= 2: # Subdirectory
             cluster_chain = self._get_cluster_chain(dir_cluster)
             if not cluster_chain: raise FileNotFoundError(f"Directory cluster {dir_cluster} invalid, cannot find '{name_to_find}'.")

             for current_c in cluster_chain:
                  cluster_offset = self._cluster_to_offset(current_c)
                  cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
                  for i in range(0, len(cluster_data), 32):
                       entry_data = cluster_data[i:i+32]
                       if len(entry_data) < 32: break
                       if entry_data[0] == ENTRY_UNUSED: break
                       if entry_data[0] == ENTRY_DELETED: continue
                       entry = self._parse_single_directory_entry(entry_data)
                       if entry and entry.name.upper() == name_upper:
                            self.logger.debug(f"Found '{name_to_find}' in cluster {current_c} at offset {i}")
                            return entry, (current_c, i) # Return cluster and offset within cluster

             raise FileNotFoundError(f"Entry '{name_to_find}' not found in directory starting at cluster {dir_cluster}.")
        else:
             raise ValueError(f"Invalid directory cluster number: {dir_cluster}")


    def _find_free_directory_entry(self, dir_cluster: int) -> Optional[Tuple[int, int]]:
        """
        Finds the first free (0x00 or 0xE5) slot in a directory cluster chain.
        Returns (dir_cluster_num, offset_in_cluster) or None if full and cannot extend.
        Extends the directory by one cluster if necessary and possible.
        """
        self.logger.debug(f"Searching for free directory entry slot in cluster {dir_cluster}")

        if dir_cluster == 0: # Root directory (FAT12/16) - cannot be extended
            if self.root_dir_bytes == 0:
                 self.logger.warning("Root directory has 0 size, cannot add entries.")
                 return None
            dir_data = self._read_bytes(self.root_dir_start_offset, self.root_dir_bytes)
            for i in range(0, len(dir_data), 32):
                entry_data = dir_data[i:i+32]
                if len(entry_data) < 32: break # Should not happen if root_dir_bytes is multiple of 32
                first_byte = entry_data[0]
                if first_byte == ENTRY_UNUSED or first_byte == ENTRY_DELETED:
                    self.logger.debug(f"Found free slot in root directory at offset {i}")
                    return (0, i) # Return cluster 0 and offset
            self.logger.warning("Root directory is full.")
            return None # Root directory is full

        elif dir_cluster >= 2: # Subdirectory - can be extended
             cluster_chain = self._get_cluster_chain(dir_cluster)
             if not cluster_chain:
                  self.logger.error(f"Cannot find free entry: Invalid directory cluster chain starting at {dir_cluster}.")
                  return None

             last_cluster_in_chain = -1
             for current_c in cluster_chain:
                  last_cluster_in_chain = current_c
                  cluster_offset = self._cluster_to_offset(current_c)
                  cluster_data = self._read_bytes(cluster_offset, self.cluster_size)
                  for i in range(0, len(cluster_data), 32):
                       entry_data = cluster_data[i:i+32]
                       if len(entry_data) < 32: break # Should not happen
                       first_byte = entry_data[0]
                       if first_byte == ENTRY_UNUSED or first_byte == ENTRY_DELETED:
                            self.logger.debug(f"Found free slot in cluster {current_c} at offset {i}")
                            return (current_c, i) # Return cluster and offset within cluster

             # If we finished looping through all clusters and found no free slot, try to extend
             self.logger.info(f"Directory chain starting at {dir_cluster} is full. Attempting to extend.")
             if last_cluster_in_chain == -1: # Should not happen if chain was valid
                  self.logger.error("Could not determine last cluster in chain for extension.")
                  return None

             # Find a new free cluster
             new_cluster = self._find_free_cluster()
             if new_cluster is None:
                 self.logger.warning("Cannot extend directory: no free clusters available.")
                 return None

             # Link the last cluster of the directory to the new cluster
             self.logger.info(f"Extending directory chain: Linking cluster {last_cluster_in_chain} -> {new_cluster}")
             self._set_fat_entry_cached(last_cluster_in_chain, new_cluster)
             # Mark the new cluster as the end of the chain
             self._set_fat_entry_cached(new_cluster, FAT12_EOC)

             # Initialize the new cluster with 0x00 bytes
             new_cluster_offset = self._cluster_to_offset(new_cluster)
             self.logger.debug(f"Initializing new directory cluster {new_cluster} at offset {new_cluster_offset}")
             self._write_bytes(new_cluster_offset, bytes([ENTRY_UNUSED]) * self.cluster_size) # Fill with 0x00

             # The first entry in the new cluster is now the free slot
             self.logger.debug(f"Returning first slot in newly allocated cluster {new_cluster} (offset 0)")
             return (new_cluster, 0) # Return new cluster and offset 0
        else:
             raise ValueError(f"Invalid directory cluster number: {dir_cluster}")

    def _get_offset_for_directory_entry(self, dir_cluster_num: int, entry_offset_in_cluster: int) -> int:
         """Calculates the absolute disk offset for a directory entry."""
         if dir_cluster_num == 0: # Root directory
              return self.root_dir_start_offset + entry_offset_in_cluster
         elif dir_cluster_num >= 2: # Subdirectory
              cluster_start_offset = self._cluster_to_offset(dir_cluster_num)
              return cluster_start_offset + entry_offset_in_cluster
         else:
              raise ValueError(f"Invalid cluster number for directory entry offset: {dir_cluster_num}")

    def _format_83_filename(self, name: str) -> bytes:
        """Formats a validated 8.3 filename into the 11-byte directory entry format."""
        # Assumes name is already validated by _is_valid_83_filename
        parts = name.upper().split('.', 1)
        base_name = parts[0]
        extension = parts[1] if len(parts) > 1 else ""

        # Handle special first byte case for Japanese systems (0xE5 -> 0x05) if needed
        # if base_name.startswith('\xE5'): base_name = '\x05' + base_name[1:]

        formatted_name = base_name.ljust(8).encode('cp437')
        formatted_ext = extension.ljust(3).encode('cp437')

        return formatted_name + formatted_ext

    def _create_directory_entry_bytes(self, name: str, is_dir: bool,
                                    starting_cluster: int, size: int, dt: datetime.datetime) -> bytes:
        """Creates the 32-byte data for a directory entry."""
        self.logger.debug(f"Creating directory entry bytes: name='{name}', is_dir={is_dir}, "
                        f"cluster={starting_cluster}, size={size}")
        entry = bytearray(32) # Initialize with zeros

        # Format filename (handle . and .. separately for padding)
        if name in [".", ".."]:
            fname_bytes = name.ljust(8).encode('cp437') + b'   '
        else:
            fname_bytes = self._format_83_filename(name)
        entry[0:11] = fname_bytes

        # Set attributes
        attr = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE # Default to Archive bit set
        entry[11] = attr

        # Reserved (NT VFAT, ignore for basic FAT12)
        entry[12] = 0 # Reserved
        # Creation time tenths, time, date (optional, set to 0)
        entry[13:22] = bytes(9) # CrtTimeTenth, CrtTime, CrtDate, LstAccDate, FstClusHI (zero for FAT12)

        # Last write time and date
        time_val = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
        date_val = (((dt.year - 1980) & 0x7F) << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)
        # self.logger.debug(f"DateTime {dt} -> time=0x{time_val:04X}, date=0x{date_val:04X}")
        entry[22:24] = struct.pack('<H', time_val) # WrtTime
        entry[24:26] = struct.pack('<H', date_val) # WrtDate

        # Starting cluster (low word for FAT12)
        entry[26:28] = struct.pack('<H', starting_cluster & 0xFFFF)

        # File size
        entry[28:32] = struct.pack('<I', size)

        self.logger.debug(f"Created directory entry bytes: {bytes(entry).hex()}")
        return bytes(entry)

    _invalid_83_chars_pattern = re.compile(r'[\\/:*?"<>|\s+]') # Includes space, +, ,, ;, =, [, ]
    _reserved_names = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "LPT1", "LPT2", "LPT3"}

    def _is_valid_83_filename(self, name: str, allow_dots=True, allow_extension=True) -> bool:
        """Validates a single filename component according to 8.3 rules."""
        # self.logger.debug(f"Validating 8.3 name component: '{name}'")

        if not name or name[0] == ' ':
            self.logger.warning(f"Invalid 8.3 name: Cannot be empty or start with space ('{name}')")
            return False

        # Check original name for trailing dot BEFORE splitting
        if name.endswith('.') and name not in ['.', '..']:
            self.logger.warning(f"Invalid 8.3 name: Ends with a dot ('{name}')")
            return False

        # Handle special "." and ".." cases
        if name in [".", ".."]:
            return allow_dots

        # Check for invalid characters
        if self._invalid_83_chars_pattern.search(name):
            invalid_found = {c for c in name if self._invalid_83_chars_pattern.search(c)}
            self.logger.warning(f"Invalid 8.3 name: Contains invalid characters {invalid_found} in '{name}'")
            return False

        # Check for control characters
        if any(0 < ord(c) < 32 for c in name):
             self.logger.warning(f"Invalid 8.3 name: Contains control characters in '{name}'")
             return False

        # Split into base and extension
        parts = name.split('.', 1)
        base = parts[0]
        ext = parts[1] if len(parts) > 1 else ""

        if not base: # Check if base is empty (e.g., ".TXT")
            self.logger.warning(f"Invalid 8.3 name: Base name cannot be empty ('{name}')")
            return False

        # Check lengths
        if len(base) > 8:
            self.logger.warning(f"Invalid 8.3 name: Base name '{base}' too long (> 8 chars) in '{name}'")
            return False
        if len(ext) > 3:
            self.logger.warning(f"Invalid 8.3 name: Extension '{ext}' too long (> 3 chars) in '{name}'")
            return False

        # Disallow extension if requested (for directory names)
        if ext and not allow_extension:
             self.logger.warning(f"Invalid directory name: Cannot have an extension ('{name}')")
             return False

        # Check for reserved names (case-insensitive)
        if base.upper() in self._reserved_names:
            self.logger.warning(f"Invalid 8.3 name: Reserved device name '{base.upper()}' used in '{name}'")
            return False

        # Check for trailing spaces or dots in base or extension (strictly invalid)
        if base.endswith(' ') or base.endswith('.') or ext.endswith(' ') or ext.endswith('.'):
             self.logger.warning(f"Invalid 8.3 name: Base or extension ends with space or dot in '{name}'")
             return False

        # self.logger.debug(f"Valid 8.3 name component: '{name}'")
        return True
