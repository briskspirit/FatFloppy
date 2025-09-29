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

# Directory block trailer layout (last 6 bytes of 512-byte dir block)
DIR_ZERO_OFFSET = 506              # 0x00
DIR_ENTRYLEN_OFFSET = 507          # usually 23
DIR_THIS_BLOCK_PTR_OFFSET = 508    # <H, little-endian>
DIR_NEXT_BLOCK_PTR_OFFSET = 510    # <H, little-endian>

# LBA (0-based) for the critical Label Identification Sector
HDOS_LABEL_SECTOR_LBA = 9  # Track 0, Sector 10
HDOS_RGT_SECTOR_LBA = 10  # Track 1, Sector 1 (RGT.SYS location)
HDOS_SYSTEM_FILES = {'RGT.SYS', 'GRT.SYS', 'DIRECT.SYS', 'HDOS.SYS'}  # Protected system files

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
    last_sector_index: int  # 1-based count of sectors used in the last group (0 means empty file)
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

        dir_start_block = struct.unpack_from("<H", data, 3)[0]
        grt_start_block = struct.unpack_from("<H", data, 5)[0]
        cluster_factor = data[7]
        volume_number = data[0]
        
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
        self._grt: Optional[bytearray] = None
        self._rgt: Optional[bytearray] = None
        self._dir_entries: Optional[List[HDOSDirectoryEntry]] = None
        self._init_completed = False
        self._cached_validity_score: Optional[int] = None
        self._data_base_lba_cache: Optional[int] = None
        self._num_groups_on_disk: int = 0

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
            
            self._initialize()
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
                self._data_base_lba_cache = None

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
        data_area_start_lba = self._data_base_lba()

        for _ in range(self.disk.physical_format.total_sectors):
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

        # Calculate the allocated size (sector-aligned)
        allocated_size = self._calculate_file_size(target_entry)
        file_data = file_data[:allocated_size]
        
        # Trim trailing null bytes only if this appears to be a text file
        # Check if the file is primarily text (printable ASCII + common whitespace)
        if len(file_data) > 0:
            # Sample up to first 512 bytes to determine if it's text
            sample_size = min(512, len(file_data))
            sample = file_data[:sample_size]
            
            # Count text-like bytes (printable ASCII + tab, newline, carriage return)
            text_bytes = sum(1 for b in sample if b in range(32, 127) or b in (9, 10, 13))
            text_ratio = text_bytes / sample_size
            
            # If >90% of sampled bytes are text-like, treat as text file and trim nulls
            if text_ratio > 0.9:
                while file_data and file_data[-1] == 0:
                    file_data.pop()
                logger.debug(f"Trimmed trailing nulls from text file: {filename}")
        
        return bytes(file_data)

    def _initialize(self) -> None:
        """Loads and parses the core HDOS filesystem structures from the disk."""
        if self._init_completed:
            return

        label_data = self._read_lba(HDOS_LABEL_SECTOR_LBA)
        self.label = HDOSLabelRecord.from_bytes(label_data)
        if not self.label.is_valid(self.disk.physical_format.total_sectors):
            raise ValueError(f"Parsed Label Record contains invalid pointers: {self.label}")

        logger.info(f"HDOS Label Record parsed: Title='{self.label.title}', Vol={self.label.volume_number}, "
                    f"DiskClusterFactor={self.label.cluster_factor}, DirStartLBA={self.label.dir_start_block}, "
                    f"GRTStartLBA={self.label.grt_start_block}")

        self._grt = bytearray(self._read_lba(self.label.grt_start_block))
        
        # Read the RGT (Reserved Group Table) for lock-out information
        try:
            self._rgt = bytearray(self._read_lba(HDOS_RGT_SECTOR_LBA))
            logger.debug(f"RGT loaded from LBA {HDOS_RGT_SECTOR_LBA}")
        except (IOError, ValueError) as e:
            logger.warning(f"Could not read RGT: {e}. Proceeding without lock-out map.")
            self._rgt = None

        # Calculate data base LBA *before* other calculations depend on it
        data_base = self._data_base_lba()

        # Calculate and guard the number of groups on the disk
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
        Checks if a group is locked out in the RGT (Reserved Group Table).
        
        Per HDOS spec:
        - RGT byte value 0x01 = usable
        - RGT byte value 0x00 or 0x80-0xFF (negative) = locked out
        """
        if not self._rgt or group_num < 1 or group_num >= len(self._rgt):
            return False
        
        rgt_value = self._rgt[group_num]
        # Locked out if: 0x00, or negative (0x80-0xFF)
        return rgt_value == 0x00 or rgt_value >= 0x80

    def _read_directory_chain(self) -> List[HDOSDirectoryEntry]:
        """
        Reads the directory, which is a linked-list of 512-byte blocks.
        """
        entries: List[HDOSDirectoryEntry] = []
        current_block_lba = self.label.dir_start_block
        
        for _ in range(20):
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

                entry = self._parse_single_dir_entry(entry_data)
                if entry:
                    entries.append(entry)
            
            if end_of_dir_found: break
            
            current_block_lba = struct.unpack_from("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET)[0]

        return entries

    def _calculate_file_size(self, entry: HDOSDirectoryEntry) -> int:
        """
        Calculates the allocated size of a file based on its directory entry.
        
        HDOS doesn't store exact file sizes, only which groups are allocated and
        how many sectors in the last group contain data.
        """
        if not self._grt or not self.label:
            return 0
        
        if entry.first_group == 0:
            return 0

        # Count groups in the file's chain
        group_count = 0
        current_group = entry.first_group

        for _ in range(len(self._grt)):
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

        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        
        # Calculate full groups (all except the last)
        if group_count > 1:
            full_groups_sectors = (group_count - 1) * sectors_per_group
        else:
            full_groups_sectors = 0
        
        # Add sectors from the last group
        last_group_sectors = entry.last_sector_index
        
        total_sectors = full_groups_sectors + last_group_sectors
        return total_sectors * HDOS_BYTES_PER_SECTOR

    def get_disk_map_layout(self) -> Dict[str, Any]:
        """
        Provides data for visualizing the disk layout.
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            return {}
        self._initialize()

        system_lbas = set(range(HDOS_SECTORS_PER_TRACK))  # Track 0
        
        sectors_per_group = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        data_area_start_lba = self._data_base_lba()
        
        # Calculate which group contains RGT
        rgt_lbas = set()
        if HDOS_RGT_SECTOR_LBA >= data_area_start_lba:
            rgt_group = ((HDOS_RGT_SECTOR_LBA - data_area_start_lba) // sectors_per_group) + 1
            rgt_group_start_lba = data_area_start_lba + (rgt_group - 1) * sectors_per_group
            for s_offset in range(sectors_per_group):
                rgt_lbas.add(rgt_group_start_lba + s_offset)
        
        # Calculate which group contains GRT
        grt_lbas = set()
        if self.label.grt_start_block >= data_area_start_lba:
            grt_group = ((self.label.grt_start_block - data_area_start_lba) // sectors_per_group) + 1
            grt_group_start_lba = data_area_start_lba + (grt_group - 1) * sectors_per_group
            for s_offset in range(sectors_per_group):
                grt_lbas.add(grt_group_start_lba + s_offset)
        
        # Calculate all directory LBAs (these span multiple groups potentially)
        directory_lbas = set()
        current_block_lba = self.label.dir_start_block
        for _ in range(20):
            if current_block_lba == 0: break
            directory_lbas.add(current_block_lba)
            directory_lbas.add(current_block_lba + 1)
            
            try:
                dir_data = self._read_lba(current_block_lba) + self._read_lba(current_block_lba + 1)
                next_lba = struct.unpack_from("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET)[0]
                if next_lba == current_block_lba: break
                current_block_lba = next_lba
            except (IOError, ValueError):
                break

        allocated_groups = set(self.get_allocated_units())

        def get_sector_type(lba: int) -> str:
            # Check in priority order: system track, then specific structures, then data area
            if lba in system_lbas:
                return "system"
            if lba in rgt_lbas:
                return "rgt"
            if lba in grt_lbas:
                return "grt"
            if lba in directory_lbas:
                return "directory"
            
            # For sectors in the data area, determine by group allocation
            if lba >= data_area_start_lba:
                group_num = ((lba - data_area_start_lba) // sectors_per_group) + 1
                if group_num in allocated_groups:
                    return "data_used"
                else:
                    return "data_free"
            return "unknown"

        legend_colors = {
            "System Track": "#A0A0A0",
            "RGT": "#FF6B6B",  # Red for RGT
            "Directory": "#FFFF00",  # Yellow
            "GRT": "#FFA500",  # Orange
            "Used Data": "#FF00FF",  # Magenta
            "Free Data": "#808080",  # Gray
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
            'legend': legend,
            'get_sector_type': get_sector_type,
            'allocation_unit_size_sectors': sectors_per_group,
            'type_color_map': type_map
        }

    def get_allocated_units(self) -> List[int]:
        """
        Returns a sorted list of allocated groups.
        
        In HDOS, the GRT free chain is the authoritative source for free groups.
        The RGT marks groups that are locked out (bad sectors, system reserved).
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
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
        
        logger.warning(f"DEBUG: GRT[0]={current_group}, num_groups={num_groups_on_disk}")
        logger.warning(f"DEBUG: First 30 GRT bytes: {list(self._grt[:30])}")
        
        # If GRT[0] is 0, the disk doesn't have a maintained free chain
        if current_group == 0:
            logger.warning("GRT[0]=0, no free chain - using fallback to scan file chains")
            
            # Collect all groups actually used by files
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
            
            # Find system groups (directory and GRT)
            data_base = self._data_base_lba()
            spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
            
            system_groups = set()
            # Directory groups
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
                except (IOError, ValueError, struct.error):
                    break
            
            # GRT group
            if self.label.grt_start_block >= data_base:
                grt_group = ((self.label.grt_start_block - data_base) // spg) + 1
                if 1 <= grt_group <= num_groups_on_disk:
                    system_groups.add(grt_group)
            
            # RGT locked-out groups (uses RGT instead of hardcoding Track 0)
            rgt_locked_groups = set()
            if self._rgt:
                for g in range(1, min(num_groups_on_disk + 1, len(self._rgt))):
                    if self._is_group_locked_out(g):
                        rgt_locked_groups.add(g)
            
            allocated_groups = file_groups | system_groups | rgt_locked_groups
            logger.warning(f"DEBUG: Fallback found {len(file_groups)} file groups, "
                        f"{len(system_groups)} system groups, {len(rgt_locked_groups)} RGT-locked groups")
            return sorted(list(allocated_groups))
        
        # Normal free chain traversal
        while current_group != 0 and 0 < current_group <= num_groups_on_disk:
            if current_group in visited:
                logger.warning(f"Circular reference in free chain at group {current_group}")
                break
            if current_group >= len(self._grt):
                logger.warning(f"Free chain group {current_group} beyond GRT bounds")
                break
                
            visited.add(current_group)
            free_groups.add(current_group)
            try:
                current_group = self._grt[current_group]
            except IndexError:
                break

        allocated_groups = all_groups - free_groups
        logger.warning(f"DEBUG: Normal traversal found {len(free_groups)} free groups")
        return sorted(list(allocated_groups))

    def get_free_space(self) -> Tuple[int, int]:
        """
        Calculates free space by counting free groups in the GRT.
        """
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
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
        
        # Ensure free group count is non-negative
        free_group_count = max(0, free_group_count)
        
        group_size_bytes = spg * HDOS_BYTES_PER_SECTOR
        free_bytes = free_group_count * group_size_bytes

        logger.debug(f"Free space: {free_group_count} groups × {group_size_bytes} bytes = {free_bytes} bytes")
        
        return free_bytes, total_bytes
    
    def _data_base_lba(self) -> int:
        """
        Returns LBA where group 1 starts. Per HDOS spec, this is LBA 2.
        """
        if self._data_base_lba_cache is not None:
            return self._data_base_lba_cache
        
        if not self.label:
            raise RuntimeError("_data_base_lba called before label was parsed.")
        
        self._data_base_lba_cache = 2
        return 2

    def delete(self, path: str) -> None:
        """Deletes a file by marking its directory entry and freeing its groups."""
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid.")
        self._initialize()

        filename = path.strip("/").upper()
        
        # Protect system files from deletion
        if filename in HDOS_SYSTEM_FILES:
            raise IOError(f"Cannot delete system file: {filename}")
        
        found_entry = None
        entry_dir_lba = 0
        entry_offset_in_block = 0
        
        # Find the file in the directory
        current_dir_lba = self.label.dir_start_block
        for _ in range(20):
            if current_dir_lba == 0: 
                break
            try:
                dir_data = self._read_lba(current_dir_lba) + self._read_lba(current_dir_lba + 1)
            except (IOError, ValueError) as e:
                logger.error(f"Could not read directory block at LBA {current_dir_lba}: {e}")
                break
                
            for i in range(DIR_ENTRIES_PER_BLOCK):
                offset = i * HDOS_DIR_ENTRY_SIZE
                entry = self._parse_single_dir_entry(dir_data[offset : offset + HDOS_DIR_ENTRY_SIZE])
                if entry and entry.get_filename().upper() == filename:
                    found_entry = entry
                    entry_dir_lba = current_dir_lba
                    entry_offset_in_block = offset
                    break
            if found_entry: 
                break
            try:
                current_dir_lba = struct.unpack_from("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET)[0]
            except struct.error:
                break
        
        if not found_entry:
            raise FileNotFoundError(f"File not found: {path}")

        # Free the file's groups by adding them to the free chain
        if found_entry.first_group != 0:
            # Find the last group in the file's chain
            last_in_chain = found_entry.first_group
            for _ in range(len(self._grt)):
                if last_in_chain >= len(self._grt):
                    logger.warning(f"File chain extends beyond GRT bounds at group {last_in_chain}")
                    break
                next_group = self._grt[last_in_chain]
                if next_group == 0:
                    break
                if next_group == last_in_chain:
                    logger.warning(f"Circular reference detected in file chain at group {last_in_chain}")
                    break
                last_in_chain = next_group

            # Check if we need to rebuild the free chain from scratch
            if self._grt[0] == 0:
                logger.info("Rebuilding free chain from scratch after deletion")
                
                # Get all currently allocated groups (before this deletion)
                current_allocated = set(self.get_allocated_units())
                
                # Remove the groups from the file we're deleting
                deleted_file_groups = set()
                current = found_entry.first_group
                for _ in range(len(self._grt)):
                    if current == 0 or current > self._num_groups_on_disk:
                        break
                    deleted_file_groups.add(current)
                    if current == found_entry.last_group:
                        break
                    if current >= len(self._grt):
                        break
                    current = self._grt[current]
                
                current_allocated -= deleted_file_groups
                
                # Build free chain from all non-allocated groups
                all_groups = set(range(1, self._num_groups_on_disk + 1))
                
                # Exclude RGT locked-out groups (instead of hardcoding Track 0)
                rgt_locked_groups = set()
                if self._rgt:
                    for g in range(1, min(self._num_groups_on_disk + 1, len(self._rgt))):
                        if self._is_group_locked_out(g):
                            rgt_locked_groups.add(g)
                    logger.info(f"Excluding {len(rgt_locked_groups)} RGT locked-out groups from free chain")
                
                # Free groups = all - allocated - locked
                free_groups = sorted(all_groups - current_allocated - rgt_locked_groups)
                
                # Link them into a chain
                for i in range(len(free_groups) - 1):
                    self._grt[free_groups[i]] = free_groups[i + 1]
                if free_groups:
                    self._grt[free_groups[-1]] = 0
                    self._grt[0] = free_groups[0]
                
                logger.info(f"Built free chain with {len(free_groups)} groups "
                        f"(excluded {len(rgt_locked_groups)} RGT-locked groups)")
            else:
                # Normal case: existing free chain, just prepend deleted groups
                old_free_start = self._grt[0]
                self._grt[last_in_chain] = old_free_start
                self._grt[0] = found_entry.first_group

            # Write the updated GRT
            try:
                self._write_lba(self.label.grt_start_block, self._grt)
            except (IOError, ValueError) as e:
                logger.error(f"Failed to write updated GRT: {e}")
                raise IOError("Failed to update GRT after file deletion") from e

        # Mark the directory entry as deleted
        try:
            dir_block_data = bytearray(self._read_lba(entry_dir_lba) + self._read_lba(entry_dir_lba + 1))
            dir_block_data[entry_offset_in_block] = 0xFF
            self._write_lba(entry_dir_lba, dir_block_data[:HDOS_BYTES_PER_SECTOR])
            self._write_lba(entry_dir_lba + 1, dir_block_data[HDOS_BYTES_PER_SECTOR:])
        except (IOError, ValueError) as e:
            logger.error(f"Failed to update directory after deleting {path}: {e}")
            raise IOError("Failed to update directory entry") from e

        self.disk.flush()
        
        # Clear all cached data to force re-initialization
        self._dir_entries = None
        self._grt = None
        self._rgt = None  # Also clear RGT cache
        self._init_completed = False
        self._data_base_lba_cache = None
        self._cached_validity_score = None
        
        logger.info(f"Deleted file '{path}'")

    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None:
        """Formats the disk with a blank HDOS filesystem."""
        if not isinstance(profile.filesystem_config, HDOSLabelRecord):
            raise ValueError("FormatProfile must contain an HDOSLabelRecord for formatting.")
        
        label_template = profile.filesystem_config
        if volume_label:
            label_template.title = volume_label

        pf = profile.physical_format
        spg = label_template.cluster_factor if label_template.cluster_factor > 0 else 1
        
        data_area_start_lba = 2 
        total_data_sectors = pf.total_sectors - data_area_start_lba
        num_groups_on_disk = total_data_sectors // spg
        
        # Calculate which groups are occupied by the directory
        dir_start_group = ((label_template.dir_start_block - data_area_start_lba) // spg) + 1
        dir_groups_needed = (DIR_BLOCK_SECTORS + spg - 1) // spg
        dir_reserved_groups = set(range(dir_start_group, dir_start_group + dir_groups_needed))
        
        # Calculate which group is occupied by the GRT itself (if in data area)
        grt_reserved_groups = set()
        if label_template.grt_start_block >= data_area_start_lba:
            grt_group = ((label_template.grt_start_block - data_area_start_lba) // spg) + 1
            if 1 <= grt_group <= num_groups_on_disk:
                grt_reserved_groups.add(grt_group)

        # Combine all reserved (allocated) groups
        reserved_groups = dir_reserved_groups | grt_reserved_groups

        # Build the GRT with a free chain excluding reserved groups
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

        # Create empty directory
        dir_data = bytearray(DIR_BLOCK_BYTES)
        dir_data[0] = 0xFE
        struct.pack_into("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET, 0)

        # Write all structures
        label_bytes = self._create_label_sector_bytes(label_template)
        self._write_lba(HDOS_LABEL_SECTOR_LBA, label_bytes)
        self._write_lba(label_template.grt_start_block, grt_data)
        self._write_lba(label_template.dir_start_block, dir_data[:HDOS_BYTES_PER_SECTOR])
        self._write_lba(label_template.dir_start_block + 1, dir_data[HDOS_BYTES_PER_SECTOR:])

        self.disk.flush()
        self._init_completed = False
        self._data_base_lba_cache = None
        
        logger.info(f"Disk formatted with profile '{profile.name}', reserved groups: {len(reserved_groups)}")

    def write_file(self, path: str, data: bytes) -> None:
        """Writes data to a new file on the disk."""
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem not valid.")
        
        try:
            self.delete(path)
        except FileNotFoundError:
            pass

        self._initialize()
        spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        
        num_sectors_needed = (len(data) + HDOS_BYTES_PER_SECTOR - 1) // HDOS_BYTES_PER_SECTOR
        num_groups_needed = (num_sectors_needed + spg - 1) // spg if num_sectors_needed > 0 else 0

        if num_groups_needed == 0:
             self._create_empty_file_entry(path)
             return

        allocated_groups = []
        current_group = self._grt[0]
        for _ in range(num_groups_needed):
            if current_group == 0:
                raise IOError("Not enough free space on disk.")
            allocated_groups.append(current_group)
            current_group = self._grt[current_group]
        
        self._grt[0] = current_group
        for i in range(len(allocated_groups) - 1):
            self._grt[allocated_groups[i]] = allocated_groups[i+1]
        self._grt[allocated_groups[-1]] = 0

        padded_data = data.ljust(num_sectors_needed * HDOS_BYTES_PER_SECTOR, b'\x00')
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

        self._create_and_write_dir_entry(path, allocated_groups, num_sectors_needed)

        self._write_lba(self.label.grt_start_block, self._grt)
        self.disk.flush()

        self._dir_entries = None
        self._grt = None
        self._init_completed = False
        self._data_base_lba_cache = None
        logger.info(f"Successfully wrote file '{path}' ({len(data)} bytes).")

    def _create_and_write_dir_entry(self, path: str, allocated_groups: List[int], num_sectors: int) -> None:
        """Finds a free slot and writes a new directory entry."""
        name, ext = (path.strip("/").upper().split('.') + [''])[:2]
        name_bytes = name.ljust(8).encode('ascii')
        ext_bytes = ext.ljust(3).encode('ascii')
        
        spg = self.label.cluster_factor if self.label.cluster_factor > 0 else 1
        lsi = (num_sectors - 1) % spg + 1 if num_sectors > 0 else 0

        now = datetime.datetime.now()
        date_packed = (((now.year - 1970) << 9) | (now.month << 5) | now.day)

        entry_bytes = bytearray(HDOS_DIR_ENTRY_SIZE)
        entry_bytes[0:8] = name_bytes
        entry_bytes[8:11] = ext_bytes
        entry_bytes[13] = spg
        entry_bytes[16] = allocated_groups[0] if allocated_groups else 0
        entry_bytes[17] = allocated_groups[-1] if allocated_groups else 0
        entry_bytes[18] = lsi
        struct.pack_into("<H", entry_bytes, 19, date_packed)
        struct.pack_into("<H", entry_bytes, 21, date_packed)

        current_dir_lba = self.label.dir_start_block
        for _ in range(20):
            if current_dir_lba == 0: raise IOError("No free directory space.")
            dir_data = bytearray(self._read_lba(current_dir_lba) + self._read_lba(current_dir_lba + 1))
            for i in range(DIR_ENTRIES_PER_BLOCK):
                offset = i * HDOS_DIR_ENTRY_SIZE
                if dir_data[offset] in (0x00, 0xFF, 0xFE):
                    dir_data[offset:offset+HDOS_DIR_ENTRY_SIZE] = entry_bytes
                    self._write_lba(current_dir_lba, dir_data[:HDOS_BYTES_PER_SECTOR])
                    self._write_lba(current_dir_lba + 1, dir_data[HDOS_BYTES_PER_SECTOR:])
                    return
            current_dir_lba = struct.unpack_from("<H", dir_data, DIR_NEXT_BLOCK_PTR_OFFSET)[0]
        
        raise IOError("Could not find a free directory entry slot.")

    def _parse_single_dir_entry(self, data: bytes) -> Optional[HDOSDirectoryEntry]:
        """Helper to parse a single 23-byte directory entry."""
        if not data or len(data) < HDOS_DIR_ENTRY_SIZE or data[0] in (0x00, 0xFF, 0xFE):
            return None
        
        name_bytes = data[0:8]
        name = "".join(chr(b & 0x7F) for b in name_bytes).strip('\x00').strip()
        if not name: return None
        ext = "".join(chr(b & 0x7F) for b in data[8:11]).strip('\x00').strip()

        return HDOSDirectoryEntry(
            raw_name=name_bytes, name=name, ext=ext,
            cluster_factor=data[13],
            first_group=data[16],
            last_group=data[17],
            last_sector_index=data[18],
            creation_date=self._parse_date(data[19:21]),
            modification_date=self._parse_date(data[21:23]),
        )

    # --- Boilerplate and helpers ---
    def _lba_to_ts(self, lba: int) -> Tuple[int, int]:
        spt = self.disk.physical_format.get_sectors_per_track(0, 0)
        return lba // spt, lba % spt

    def _read_lba(self, lba: int) -> bytes:
        c, h, s = self.disk.physical_format.lba_to_chs(lba)
        return self.disk.read_sector(c, h, s)

    def _write_lba(self, lba: int, data: bytes) -> None:
        c, h, s = self.disk.physical_format.lba_to_chs(lba)
        self.disk.write_sector(c, h, s, data)

    def _parse_date(self, date_bytes: bytes) -> datetime.datetime:
        word = struct.unpack_from("<H", date_bytes)[0]
        day = word & 0x1F
        month = (word >> 5) & 0x0F
        year = (word >> 9) + 1970
        try:
            return datetime.datetime(year, month, day)
        except (ValueError, TypeError):
            return HDOS_DEFAULT_DATETIME
            
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

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("HDOS does not support directories.")

    def delete_recursive(self, path: str) -> bool:
        try:
            self.delete(path)
            return True
        except (IOError, FileNotFoundError):
            return False
            
    def _create_label_sector_bytes(self, label: HDOSLabelRecord) -> bytes:
        sector = bytearray(HDOS_BYTES_PER_SECTOR)
        struct.pack_into("<H", sector, 3, label.dir_start_block)
        struct.pack_into("<H", sector, 5, label.grt_start_block)
        sector[7] = label.cluster_factor
        sector[0] = label.volume_number
        
        title_bytes = label.title.encode('ascii', 'ignore')
        sector[17:17+len(title_bytes)] = title_bytes
        return bytes(sector)
        
    def _create_empty_file_entry(self, path: str) -> None:
        self._create_and_write_dir_entry(path, [], 0)
        self.disk.flush()
        logger.info(f"Successfully wrote empty file '{path}'.")
