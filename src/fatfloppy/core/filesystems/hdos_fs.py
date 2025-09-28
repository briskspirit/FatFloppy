"""
This module provides a read-only filesystem implementation for the Heathkit
Disk Operating System (HDOS). It is designed to parse HDOS disk images,
list directories, and read files based on the "HDOS Disk File Handling" article.
"""
import struct
import datetime
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any

from .fs_base import Filesystem, FileInfo
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger
from ..disk import Disk
from ..physical_format import PhysicalFormat, TrackFormat

# --- Constants based on the HDOS article and structure ---
HDOS_BYTES_PER_SECTOR = 256
HDOS_SECTORS_PER_TRACK = 10
HDOS_TRACKS = 40
HDOS_DIR_ENTRY_SIZE = 23
HDOS_DEFAULT_DATETIME = datetime.datetime(1970, 1, 1)

# A directory "cluster block" is a fixed 512-byte structure.
DIR_BLOCK_SECTORS = 2
DIR_BLOCK_BYTES = DIR_BLOCK_SECTORS * HDOS_BYTES_PER_SECTOR
DIR_ENTRIES_PER_BLOCK = 22  # 22 entries * 23 bytes = 506 bytes, plus trailer.
DIR_NEXT_BLOCK_PTR_OFFSET = 510  # Offset within the 512-byte block.

# LBA (0-based) for the critical Label Identification Sector
HDOS_LABEL_SECTOR_LBA = 9  # Track 0, Sector 10

logger = get_logger("HDOSFilesystem")


@dataclass
class HDOSDirectoryEntry:
    """Represents a single 23-byte HDOS directory entry."""
    raw_name: bytes
    name: str
    ext: str
    cluster_factor: int  # Sectors per group for THIS file's read operations
    first_group: int
    last_group: int
    last_sector_index: int  # 0-based index of last sector in last group
    creation_date: datetime.datetime
    modification_date: datetime.datetime

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
    cluster_factor: int  # Defines the directory block size (e.g., 2 sectors)
    dir_start_block: int  # LBA of the first directory sector
    grt_start_block: int  # LBA of the Group Reservation Table sector

    @classmethod
    def from_bytes(cls, data: bytes) -> 'HDOSLabelRecord':
        """
        Parses a 256-byte sector based on the precise documented layout.
        """
        if len(data) < HDOS_BYTES_PER_SECTOR:
            raise ValueError(f"Label sector data must be {HDOS_BYTES_PER_SECTOR} bytes.")

        # Offsets based on the detailed manx-docs.org table.
        # This differs from the original implementation's interpretation.
        dir_start_block = struct.unpack_from("<H", data, 3)[0]
        grt_start_block = struct.unpack_from("<H", data, 5)[0]
        cluster_factor = data[7]
        volume_number = data[0]
        
        # The label is a 60-byte field starting at offset 0x11 (17).
        title = data[17:77].decode('ascii', errors='ignore').strip('\x00').strip()

        return cls(title, volume_number, cluster_factor, dir_start_block, grt_start_block)

    def is_valid(self, max_blocks: int) -> bool:
        """
        Performs a basic sanity check on the parsed label record values.
        A cluster_factor of 0 is valid and implies 1 sector per group.
        """
        return (0 <= self.cluster_factor <= 16 and
                HDOS_SECTORS_PER_TRACK <= self.dir_start_block < max_blocks and
                HDOS_SECTORS_PER_TRACK <= self.grt_start_block < max_blocks)


class HDOSFilesystem(Filesystem):
    """
    Provides a read-only interface to an HDOS filesystem on a disk image.
    """
    VALIDITY_THRESHOLD = 95

    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.label: Optional[HDOSLabelRecord] = None
        self._grt: Optional[bytes] = None
        self._dir_entries: Optional[List[HDOSDirectoryEntry]] = None
        self._init_completed = False
        self._cached_validity_score: Optional[int] = None

    def get_validity_score(self) -> int:
        """
        Scores the likelihood that the disk contains a valid HDOS filesystem.
        """
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        if not self.disk:
            return 0
        original_pf = self.disk.physical_format
        
        try:
            # Temporarily apply the canonical HDOS geometry for the check.
            canonical_pf = FormatProfile(
                name="hdos_canonical", description="",
                physical_format=PhysicalFormat(
                    cylinders=HDOS_TRACKS, heads=1, rpm=300, heads_inverted=False,
                    bytes_per_sector=HDOS_BYTES_PER_SECTOR,
                    track_formats=[TrackFormat(0, 39, 0, 0, HDOS_SECTORS_PER_TRACK, "FM", 250, 1, id_start=1)]
                ),
                filesystem_type="HDOS"
            ).physical_format
            self.disk.set_geometry(canonical_pf)
            
            self._initialize() # This will raise an error if the label is invalid.
            self._cached_validity_score = 100
        except (ValueError, IOError, struct.error) as e:
            logger.debug(f"HDOS validation failed: {e}")
            self._cached_validity_score = 0
        finally:
            if original_pf:
                self.disk.set_geometry(original_pf)
            if self._cached_validity_score == 0:
                self._init_completed = False
                self.label = None

        return self._cached_validity_score
    
    @property
    def allocation_unit_size(self) -> int:
        """
        Returns the size of a single allocation unit (group) in bytes.
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return 0
        self._initialize()
        if not self.label:
            return 0
        
        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        return sectors_per_group * HDOS_BYTES_PER_SECTOR

    def list_directory(self, path: str) -> List[FileInfo]:
        """Lists all files in the root directory."""
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as HDOS.")
        if path != "/":
            raise NotImplementedError("HDOS does not support subdirectories.")
        self._initialize()
        return [FileInfo(
            name=e.get_filename(),
            size=self._calculate_file_size(e),
            is_dir=False,
            datetime=e.modification_date,
            attributes="-")
            for e in self._dir_entries]

    def read_file(self, path: str) -> bytes:
        """Reads the complete content of a specified file."""
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as HDOS.")

        self._initialize()
        filename = path.strip("/").upper()
        target_entry = next((e for e in self._dir_entries if e.get_filename().upper() == filename), None)

        if not target_entry:
            raise FileNotFoundError(f"File not found: {path}")

        file_data = bytearray()
        current_group = target_entry.first_group

        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        data_area_start_lba = 2

        for _ in range(self.disk.physical_format.total_sectors):  # Safety break
            if current_group == 0:
                break
            if not (0 < current_group < len(self._grt)):
                raise IOError(f"Corrupt file chain: group {current_group} is out of GRT bounds.")

            start_lba_of_group = data_area_start_lba + ((current_group - 1) * sectors_per_group)

            sectors_to_read = sectors_per_group
            if current_group == target_entry.last_group:
                sectors_to_read = target_entry.last_sector_index

            for i in range(sectors_to_read):
                try:
                    file_data.extend(self._read_lba(start_lba_of_group + i))
                except (ValueError, IOError) as e:
                    logger.error(f"Failed to read LBA {start_lba_of_group + i} for file {path}: {e}")
                    raise IOError(f"Failed reading LBA {start_lba_of_group + i}") from e

            if current_group == target_entry.last_group:
                break
            current_group = self._grt[current_group]

        # --- Heuristic Trimming Logic ---
        if not file_data:
            return b''

        # 1. Explicitly exclude known binary file types from any trimming.
        BINARY_EXTENSIONS = frozenset({'SYS', 'DVD', 'ABS', 'REL'})
        if target_entry.ext.upper() in BINARY_EXTENSIONS:
            return bytes(file_data)

        # 2. For other files, analyze content to see if it's likely text.
        non_null_data = [byte for byte in file_data if byte != 0x00]

        if not non_null_data:  # File was entirely zeros
            return b''

        # Define binary indicators as control characters other than common whitespace.
        binary_indicators = frozenset(
            list(range(0x01, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)) +
            list(range(0x7F, 0x100))
        )
        
        binary_count = sum(1 for byte in non_null_data if byte in binary_indicators)

        # If the ratio of binary indicators in the actual content is very low,
        # we can safely assume it's a text file and strip the padding.
        if (binary_count / len(non_null_data)) < 0.05:
            return bytes(file_data).rstrip(b'\x00')
        else:
            # Otherwise, return the data unmodified.
            return bytes(file_data)

    def _initialize(self) -> None:
        """Loads and parses the core HDOS filesystem structures from the disk."""
        if self._init_completed:
            return

        label_data = self._read_lba(HDOS_LABEL_SECTOR_LBA)
        self.label = HDOSLabelRecord.from_bytes(label_data)
        if not self.label.is_valid(self.disk.physical_format.total_sectors):
            raise ValueError(f"Parsed Label Record contains invalid pointers: {self.label}")

        self._grt = self._read_lba(self.label.grt_start_block)
        self._dir_entries = self._read_directory_chain()

        self._init_completed = True
        logger.info(f"HDOS filesystem initialized: '{self.label.title}' V:{self.label.volume_number}")

    def _read_directory_chain(self) -> List[HDOSDirectoryEntry]:
        """
        Reads the directory, which is a linked-list of 512-byte blocks.
        This process is independent of the GRT.
        """
        entries: List[HDOSDirectoryEntry] = []
        current_block_lba = self.label.dir_start_block
        
        for _ in range(20): # Safety counter for directory blocks
            if current_block_lba == 0: break

            try:
                sector1 = self._read_lba(current_block_lba)
                sector2 = self._read_lba(current_block_lba + 1)
                dir_data = sector1 + sector2
            except (IOError, ValueError) as e:
                logger.error(f"Could not read full 512-byte directory block at LBA {current_block_lba}: {e}")
                break
            
            end_of_dir_found = False
            for i in range(DIR_ENTRIES_PER_BLOCK):
                offset = i * HDOS_DIR_ENTRY_SIZE
                entry_data = dir_data[offset : offset + HDOS_DIR_ENTRY_SIZE]

                first_byte = entry_data[0]
                if first_byte == 0xFE:
                    end_of_dir_found = True
                    break
                if first_byte in [0x00, 0xFF]: continue

                name_bytes = entry_data[0:8]
                
                # CRITICAL FIX: Strip trailing spaces in addition to nulls, as HDOS
                # uses space-padding for short names and extensions.
                name = "".join(chr(b & 0x7F) for b in name_bytes).strip('\x00').strip()
                if not name: continue
                ext = "".join(chr(b & 0x7F) for b in entry_data[8:11]).strip('\x00').strip()

                entries.append(
                    HDOSDirectoryEntry(
                        raw_name=name_bytes, name=name, ext=ext,
                        cluster_factor=entry_data[13],
                        first_group=entry_data[16],
                        last_group=entry_data[17],
                        last_sector_index=entry_data[18],
                        creation_date=self._parse_date(entry_data[19:21]),
                        modification_date=self._parse_date(entry_data[21:23]),
                    )
                )
            
            if end_of_dir_found: break
            
            current_block_lba = struct.unpack_from("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET)[0]

        return entries

    def _calculate_file_size(self, entry: HDOSDirectoryEntry) -> int:
        """Calculates the exact size of a file by traversing its group chain via the GRT."""
        if not self._grt or not self.label:
            return 0

        group_count = 0
        current_group = entry.first_group

        for _ in range(len(self._grt)):  # Safety break
            if current_group == 0:
                break
            if not (0 < current_group < len(self._grt)):
                logger.warning(f"File '{entry.get_filename()}' has corrupt chain at group {current_group}.")
                return 0

            group_count += 1
            if current_group == entry.last_group:
                break
            current_group = self._grt[current_group]

        if group_count == 0:
            return 0

        # The size logic must mirror the read logic exactly.
        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        full_groups_size = (group_count - 1) * sectors_per_group * HDOS_BYTES_PER_SECTOR
        
        # The last group's size is determined by the 1-based Last Sector Index.
        last_group_size = entry.last_sector_index * HDOS_BYTES_PER_SECTOR
        return full_groups_size + last_group_size

    def _lba_to_ts(self, lba: int) -> Tuple[int, int]:
        """Converts a 0-based LBA to a 0-based (track, sector_idx) tuple."""
        spt = self.disk.physical_format.get_sectors_per_track(0, 0)
        return lba // spt, lba % spt

    def _read_lba(self, lba: int) -> bytes:
        """Reads a sector using a 0-based logical block address."""
        c, h, s = self.disk.physical_format.lba_to_chs(lba)
        return self.disk.read_sector(c, h, s)

    def _parse_date(self, date_bytes: bytes) -> datetime.datetime:
        """Decodes a 2-byte HDOS packed date into a datetime object."""
        word = struct.unpack_from("<H", date_bytes)[0]
        day = word & 0x1F
        month = (word >> 5) & 0x0F
        year = (word >> 9) + 1970
        try:
            return datetime.datetime(year, month, day)
        except (ValueError, TypeError):
            return HDOS_DEFAULT_DATETIME

    # --- Boilerplate for Read-Only Filesystem and Info Display ---
    def get_specific_config(self) -> Optional[HDOSLabelRecord]:
        self._initialize()
        return self.label

    def get_display_info(self) -> Dict[str, str]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return {"Error": "HDOS not detected or not valid."}
        self._initialize()
        if not self.label: return {"Error": "HDOS label could not be read."}
        return {
            "Filesystem Type": "HDOS", "Volume Title": self.label.title,
            "Volume Number": str(self.label.volume_number),
            "Dir Cluster Factor": str(self.label.cluster_factor),
            "Directory Start LBA": str(self.label.dir_start_block),
            "GRT Start LBA": str(self.label.grt_start_block)
        }
    def get_disk_map_layout(self) -> Dict[str, Any]:
        """
        Provides data for visualizing the disk layout.

        Returns:
            A dictionary containing legend information and a callback function
            to determine the type of each sector on the disk.
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return {}
        self._initialize()

        # Pre-calculate the locations of special areas
        system_lbas = set(range(HDOS_SECTORS_PER_TRACK))  # Track 0 is reserved
        grt_lba = {self.label.grt_start_block}
        
        directory_lbas = set()
        current_block_lba = self.label.dir_start_block
        for _ in range(20):  # Safety counter
            if current_block_lba == 0: break
            directory_lbas.add(current_block_lba)
            directory_lbas.add(current_block_lba + 1)
            
            try:
                dir_data = self._read_lba(current_block_lba) + self._read_lba(current_block_lba + 1)
                next_lba = struct.unpack_from("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET)[0]
                if next_lba == current_block_lba: break # Self-reference loop
                current_block_lba = next_lba
            except (IOError, ValueError):
                break

        allocated_groups = set(self.get_allocated_units())
        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        data_area_start_lba = 2

        def get_sector_type(lba: int) -> str:
            if lba in grt_lba:
                return "grt"
            if lba in directory_lbas:
                return "directory"
            if lba in system_lbas:
                return "system"
            
            if lba >= data_area_start_lba:
                group_num = ((lba - data_area_start_lba) // sectors_per_group) + 1
                if group_num in allocated_groups:
                    return "data_used"
                else:
                    return "data_free"
            return "unknown"

        legend_colors = {
            "System Track": "#A0A0A0", "Directory": "#FFFF00", "GRT": "#FFA500",
            "Used Data": "#FF00FF", "Free Data": "#808080",
        }
        legend = [
            ("System Track", legend_colors["System Track"]),
            ("Directory", legend_colors["Directory"]),
            ("GRT", legend_colors["GRT"]),
            ("Used Data", legend_colors["Used Data"]),
            ("Free Data", legend_colors["Free Data"]),
        ]
        type_map = {
            "system": legend_colors["System Track"], "directory": legend_colors["Directory"],
            "grt": legend_colors["GRT"], "data_used": legend_colors["Used Data"],
            "data_free": legend_colors["Free Data"], "unknown": "#008B8B",
        }

        return {
            'legend': legend,
            'get_sector_type': get_sector_type,
            'allocation_unit_size_sectors': sectors_per_group,
            'type_color_map': type_map
        }

    def get_allocated_units(self) -> List[int]:
        """
        Returns a sorted list of all allocated group numbers.
        This is determined by finding all groups NOT in the free chain.
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return []
        self._initialize()
        if not self._grt:
            return []

        # The GRT size defines the total number of groups on the disk.
        total_groups = len(self._grt) - 1
        all_possible_groups = set(range(1, total_groups + 1))

        # Trace the free chain, starting from the pointer at GRT[0].
        free_groups = set()
        current_group = self._grt[0]
        for _ in range(total_groups + 1): # Safety break
            if current_group == 0:
                break
            if not (0 < current_group < len(self._grt)):
                logger.warning(f"Corrupt free chain at group {current_group}.")
                break
            free_groups.add(current_group)
            current_group = self._grt[current_group]

        allocated_groups = all_possible_groups - free_groups
        return sorted(list(allocated_groups))

    def get_free_space(self) -> Tuple[int, int]:
        """
        Calculates the free and total data space on the disk.
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return 0, 0
        self._initialize()
        if not self._grt or not self.label or not self.disk.physical_format:
            return 0, 0

        # Correctly calculate total allocatable space from disk geometry.
        # The user data area starts at LBA 2, so the first two sectors are reserved.
        data_area_start_lba = 2
        total_data_sectors = self.disk.physical_format.total_sectors - data_area_start_lba
        total_bytes = total_data_sectors * HDOS_BYTES_PER_SECTOR

        # Calculate free space by counting groups in the free chain.
        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        group_size_bytes = sectors_per_group * HDOS_BYTES_PER_SECTOR

        free_group_count = 0
        current_group = self._grt[0]
        # Use the GRT size as a safe upper limit for the loop.
        for _ in range(len(self._grt)):
            if current_group == 0:
                break
            if not (0 < current_group < len(self._grt)):
                logger.warning(f"Corrupt free chain detected at group {current_group}. Free space may be inaccurate.")
                break
            free_group_count += 1
            current_group = self._grt[current_group]

        free_bytes = free_group_count * group_size_bytes

        # Sanity check: free space cannot exceed total space.
        if free_bytes > total_bytes:
            logger.warning(f"Calculated free space ({free_bytes}) exceeds total allocatable space ({total_bytes}). "
                           "Clamping value.")
            free_bytes = total_bytes

        return free_bytes, total_bytes
    

    def create_directory(self, path: str) -> None: raise NotImplementedError("HDOS is read-only.")
    def delete(self, path: str) -> None: raise NotImplementedError("HDOS is read-only.")
    def delete_recursive(self, path: str) -> bool: raise NotImplementedError("HDOS is read-only.")
    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None: raise NotImplementedError("HDOS is read-only.")
    def write_file(self, path: str, data: bytes) -> None: raise NotImplementedError("HDOS is read-only.")
