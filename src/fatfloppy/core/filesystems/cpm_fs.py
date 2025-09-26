# src/fatfloppy/core/filesystems/cpm_fs.py
import struct
import datetime
from dataclasses import dataclass, field
from collections import defaultdict
from typing import List, Optional, Tuple, Dict, Any, Set

from .fs_base import Filesystem, FileInfo
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger
from ..disk import Disk

logger = get_logger("CPMFilesystem")

# Standard CP/M Constants
CPM_SECTOR_SIZE = 128 # Typically, but DPB can specify others for logical mapping
CPM_DIRECTORY_ENTRIES_PER_SECTOR = CPM_SECTOR_SIZE // 32 # 4 entries per 128-byte sector
CPM_EXTENT_SIZE = 16 * 1024 # 16KB per extent (usually)
CPM_BLOCK_SIZE_DEFAULT = 1024 # Smallest allocation unit, can vary by DPB

# File attributes (typically stored in Ftype byte of directory entry)
CPM_ATTR_RO = 0x80  # Read-Only (bit 7 of ftype)
CPM_ATTR_SYS = 0x40 # System file (bit 6 of ftype)
# Other bits (5-0) in ftype are usually for user-defined types or unused

@dataclass
class CPMDiskParameterBlock:
    spt: int = 0          # Sectors Per Track (total logical 128-byte sectors on a track)
    bsh: int = 0          # Block SHift factor (log2(data allocation block size / 128))
    blm: int = 0          # BLock Mask (2^BSH - 1)
    exm: int = 0          # EXtent Mask (0 for 16k extents, 1 for 32k, etc.)
    dsm: int = 0          # DiSk Max allocation block number (total blocks - 1)
    drm: int = 0          # DiRectory Max entry number (total directory entries - 1)
    al0: int = 0          # ALlocation bitmap byte 0
    al1: int = 0          # ALlocation bitmap byte 1
    cks: int = 0          # ChecKSUM vector size (number of 32-byte dir entries for checksum)
                          # 0 means directory is not checksummed.
    off: int = 0          # OFFset, number of reserved tracks

    @property
    def block_size(self) -> int:
        return CPM_SECTOR_SIZE * (2**self.bsh)

    @property
    def directory_blocks(self) -> int:
        # Calculate how many allocation blocks are used by the directory
        # Each directory entry is 32 bytes.
        # (DRM+1) is total number of entries.
        # Total directory bytes = (DRM+1) * 32
        # Blocks = Total directory bytes / block_size (ceiling division)
        if self.block_size == 0: return 0
        return ((self.drm + 1) * 32 + self.block_size - 1) // self.block_size

    @property
    def max_file_size(self) -> int:
        num_pointers_per_extent = 16 if self.dsm <= 255 else 8 # 1-byte vs 2-byte block pointers
        return (self.exm + 1) * num_pointers_per_extent * self.block_size


@dataclass
class CPMDirectoryEntry:
    user: int = 0
    name: str = ""
    ext: str = ""
    ex: int = 0   # Extent number, low byte
    s1: int = 0   # Reserved / System use
    xh: int = 0   # Extent number, high byte (CP/M 3+) or S2 for CP/M 2.2
    rc: int = 0   # Record Count (number of 128-byte records in this extent)
    blks: List[int] = field(default_factory=list) # Allocation block pointers (16 for 1-byte, 8 for 2-byte)
    attributes_raw: Dict[str, int] = field(default_factory=dict) # Store raw attribute bits

    def get_filename(self) -> str:
        name = ''.join(chr(ord(c) & 0x7F) for c in self.name).strip()
        ext = ''.join(chr(ord(c) & 0x7F) for c in self.ext).strip()
        return f"{name}.{ext}"

    def is_deleted(self) -> bool:
        return self.user == 0xE5

    def get_attributes(self) -> str:
        attr_str_parts = [f"U{self.user}"]
        if self.attributes_raw.get('t1', 0) & 0x80: attr_str_parts.append("R") # Read-Only
        if self.attributes_raw.get('t2', 0) & 0x80: attr_str_parts.append("S") # System
        if self.attributes_raw.get('t3', 0) & 0x80: attr_str_parts.append("A") # Archive
        return "-".join(attr_str_parts)


class CPMFilesystem(Filesystem):
    VALIDITY_THRESHOLD = 50  # Score above which the filesystem is considered usable

    def __init__(self, disk: Disk):
        super().__init__(disk)
        self.dpb: Optional[CPMDiskParameterBlock] = None
        self._init_completed = False
        self._cached_directory: Optional[List[CPMDirectoryEntry]] = None
        self._cached_allocation_map: Optional[Set[int]] = None
        self._cached_validity_score: Optional[int] = None

        if self.disk and self.disk.physical_format:
            if hasattr(self.disk.physical_format, '_associated_filesystem_config'):
                fs_config = getattr(self.disk.physical_format, '_associated_filesystem_config')
                if isinstance(fs_config, CPMDiskParameterBlock):
                    self.dpb = fs_config
                    self.logger.info("Initialized DPB from disk's physical_format associated config.")

            if self.dpb:
                try:
                    self._initialize_parameters()
                    self._init_completed = True
                    self.logger.info("CP/M Filesystem initialized successfully with provided DPB.")
                except ValueError as e:
                    self.logger.error(f"CP/M Initialization failed with provided DPB: {e}")
            else:
                # Try to derive DPB if not provided
                if self._try_derive_dpb():
                    try:
                        self._initialize_parameters()
                        self._init_completed = True
                        self.logger.info("CP/M Filesystem initialized with derived DPB based on physical format.")
                    except ValueError as e:
                        self.logger.error(f"CP/M Initialization failed with derived DPB: {e}")
                else:
                    self.logger.warning("CP/M Filesystem initialized without a DPB. get_validity_score() will be required to confirm format.")
        else:
            self.logger.warning("CP/M Filesystem initialized without disk or physical format.")

    @property
    def allocation_unit_size(self) -> int:
        if self.dpb:
            return self.dpb.block_size
        return 0

    def _get_logical_spt(self, cpm_track_num: int) -> int:
        if not self.disk or not self.disk.physical_format or not self.dpb:
            raise ValueError("Disk, physical_format, or DPB not available for SPT calculation.")
        
        phys_cyl, phys_head = self._cpm_track_to_chs_coords(cpm_track_num)

        if phys_cyl >= self.disk.physical_format.cylinders:
            self.logger.warning(f"Track number {cpm_track_num} (phys cyl {phys_cyl}) exceeds max physical cylinder {self.disk.physical_format.cylinders-1}. Falling back to DPB.spt")
            return self.dpb.spt

        phys_spt = self.disk.physical_format.get_sectors_per_track(phys_cyl, phys_head)
        phys_bps = self.disk.physical_format.get_bytes_per_sector(phys_cyl, phys_head)
        
        if phys_bps < CPM_SECTOR_SIZE:
             self.logger.error(f"Track {cpm_track_num}: physical BPS ({phys_bps}) < logical BPS ({CPM_SECTOR_SIZE}). Not supported.")
             return 0
        
        return phys_spt * (phys_bps // CPM_SECTOR_SIZE)

    def _initialize_parameters(self):
        if not self.dpb:
            raise ValueError("DPB not set for initialization.")
        # Additional initialization if needed
        self._ensure_sector_translation_tables()

    def _cpm_track_to_chs_coords(self, cpm_track: int) -> Tuple[int, int]:
        if not self.disk.physical_format:
            raise ValueError("Physical format not available.")
        heads = self.disk.physical_format.heads
        cylinder = cpm_track // heads
        head = cpm_track % heads
        return cylinder, head

    def _read_logical_sector(self, cpm_track: int, logical_sector_on_track: int) -> bytes:
        if not self.dpb or not self.disk or not self.disk.physical_format:
            raise ValueError("DPB, disk, or physical format not available.")
        if logical_sector_on_track >= self._get_logical_spt(cpm_track):
            raise ValueError(f"Logical sector {logical_sector_on_track} exceeds SPT for CP/M track {cpm_track}.")

        phys_cyl, phys_head = self._cpm_track_to_chs_coords(cpm_track)
        tf = self.disk.physical_format.get_track_format(phys_cyl, phys_head)
        phys_bps = tf.bytes_per_sector
        log_per_phys = phys_bps // CPM_SECTOR_SIZE
        phys_index = logical_sector_on_track // log_per_phys
        offset_in_phys = (logical_sector_on_track % log_per_phys) * CPM_SECTOR_SIZE

        # Get the physical sector ID for this logical index
        order = tf.sector_translation_table if tf.sector_translation_table else self.disk.physical_format._build_physical_sector_order(tf)
        if phys_index >= len(order):
            raise ValueError(f"Physical index {phys_index} exceeds order length {len(order)}.")
        phys_sector_id = order[phys_index]

        # Read the physical sector
        phys_sector_data = self.disk.read_sector(phys_cyl, phys_head, phys_sector_id)
        if len(phys_sector_data) != phys_bps:
            raise IOError(f"Read physical sector size {len(phys_sector_data)} != expected {phys_bps}.")

        # Extract the logical sector data
        return phys_sector_data[offset_in_phys:offset_in_phys + CPM_SECTOR_SIZE]

    def _write_logical_sector(self, cpm_track: int, logical_sector_on_track: int, data: bytes) -> None:
        if not self.dpb or not self.disk or not self.disk.physical_format:
            raise ValueError("DPB, disk, or physical format not available.")
        if len(data) != CPM_SECTOR_SIZE:
            raise ValueError(f"Data size {len(data)} != CPM_SECTOR_SIZE {CPM_SECTOR_SIZE}.")
        if logical_sector_on_track >= self._get_logical_spt(cpm_track):
            raise ValueError(f"Logical sector {logical_sector_on_track} exceeds SPT for CP/M track {cpm_track}.")

        phys_cyl, phys_head = self._cpm_track_to_chs_coords(cpm_track)
        tf = self.disk.physical_format.get_track_format(phys_cyl, phys_head)
        phys_bps = tf.bytes_per_sector
        log_per_phys = phys_bps // CPM_SECTOR_SIZE
        phys_index = logical_sector_on_track // log_per_phys
        offset_in_phys = (logical_sector_on_track % log_per_phys) * CPM_SECTOR_SIZE

        order = tf.sector_translation_table if tf.sector_translation_table else self.disk.physical_format._build_physical_sector_order(tf)
        if phys_index >= len(order):
            raise ValueError(f"Physical index {phys_index} exceeds order length {len(order)}.")
        phys_sector_id = order[phys_index]

        # Read current physical sector, update the portion, write back
        phys_sector_data = self.disk.read_sector(phys_cyl, phys_head, phys_sector_id)
        if len(phys_sector_data) != phys_bps:
            raise IOError(f"Read physical sector size {len(phys_sector_data)} != expected {phys_bps}.")
        phys_sector_data = bytearray(phys_sector_data)
        phys_sector_data[offset_in_phys:offset_in_phys + CPM_SECTOR_SIZE] = data
        self.disk.write_sector(phys_cyl, phys_head, phys_sector_id, bytes(phys_sector_data))

    def _parse_directory_entry(self, entry_bytes: bytes) -> Optional[CPMDirectoryEntry]:
        if len(entry_bytes) < 32: return None

        user = entry_bytes[0]
        name_bytes = bytes(b & 0x7F for b in entry_bytes[1:9])
        ext_bytes = bytes(b & 0x7F for b in entry_bytes[9:12])
        raw_attrs = {'t1': entry_bytes[9], 't2': entry_bytes[10], 't3': entry_bytes[11]}
        name = name_bytes.decode('ascii', errors='replace').strip()
        ext = ext_bytes.decode('ascii', errors='replace').strip()
        
        ex, s1, xh_s2, rc = entry_bytes[12], entry_bytes[13], entry_bytes[14], entry_bytes[15]

        block_pointers = []
        if self.dpb.dsm > 255:
            for j in range(8):
                if 16 + j * 2 + 1 < len(entry_bytes):
                    block_pointers.append(struct.unpack_from("<H", entry_bytes, 16 + j * 2)[0])
                else: block_pointers.append(0)
        else:
            block_pointers.extend(entry_bytes[16:32])
        
        return CPMDirectoryEntry(user, name, ext, ex, s1, xh_s2, rc, block_pointers, attributes_raw=raw_attrs)


    def _read_directory_raw(self) -> List[CPMDirectoryEntry]:
        if not self._init_completed or not self.dpb:
            self.logger.error("Filesystem or DPB not initialized for directory read.")
            return []
        if self._cached_directory is not None:
            return self._cached_directory

        raw_dir_entries = []
        dir_logical_sectors_count = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        
        current_cpm_track_idx = self.dpb.off
        current_logical_128byte_sector_on_cpm_track_idx = 0

        for _ in range(dir_logical_sectors_count):
            try:
                sector_data = self._read_logical_sector(current_cpm_track_idx, current_logical_128byte_sector_on_cpm_track_idx)
            except Exception as e:
                self.logger.error(f"Failed to read directory sector at CP/M Track:{current_cpm_track_idx} Log.Sec:{current_logical_128byte_sector_on_cpm_track_idx}: {e}")
                self._cached_directory = None 
                return [] 

            for i in range(CPM_DIRECTORY_ENTRIES_PER_SECTOR):
                entry_bytes = sector_data[i*32:(i+1)*32]
                entry = self._parse_directory_entry(entry_bytes)
                if entry:
                    raw_dir_entries.append(entry)
                if len(raw_dir_entries) > self.dpb.drm: 
                    break
            if len(raw_dir_entries) > self.dpb.drm:
                break
            
            current_logical_128byte_sector_on_cpm_track_idx += 1
            logical_spt_for_current_track = self._get_logical_spt(current_cpm_track_idx)
            if logical_spt_for_current_track > 0 and current_logical_128byte_sector_on_cpm_track_idx >= logical_spt_for_current_track:
                current_logical_128byte_sector_on_cpm_track_idx = 0
                current_cpm_track_idx += 1
        
        self._cached_directory = raw_dir_entries
        return raw_dir_entries

    def _group_extents(self, raw_entries: List[CPMDirectoryEntry]) -> Dict[Tuple[int, str], List[CPMDirectoryEntry]]:
        files = {}
        for entry in raw_entries:
            if entry.is_deleted():
                continue
            key = (entry.user, entry.get_filename().upper())
            if key not in files:
                files[key] = []
            files[key].append(entry)
        for key in files:
            files[key].sort(key=lambda e: (e.ex | (e.xh << 8))) 
        return files
    
    def _read_directory_entries(self) -> List[CPMDirectoryEntry]:
        """Read all CP/M directory entries from the disk."""
        if not self.dpb or not self.disk or not self.disk.physical_format:
            raise ValueError("DPB, disk, or physical format not available.")

        entries = []
        dir_logical_sectors = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        current_cpm_track = self.dpb.off
        logical_sector_idx = 0

        for _ in range(dir_logical_sectors):
            # Stop if we would read past the max number of entries
            if len(entries) > self.dpb.drm:
                break
            try:
                sector_data = self._read_logical_sector(current_cpm_track, logical_sector_idx)
                
                # CRITICAL FIX: The break on an all-E5 sector was removed.
                # On a formatted disk, this would cause the read to stop immediately,
                # incorrectly reporting zero available directory slots.

                for i in range(CPM_DIRECTORY_ENTRIES_PER_SECTOR):
                    offset = i * 32
                    if offset + 32 > len(sector_data):
                        self.logger.warning(f"Invalid sector data size at CP/M track {current_cpm_track}, logical sector {logical_sector_idx}")
                        break
                    
                    if len(entries) > self.dpb.drm:
                        break

                    entry_data = sector_data[offset:offset + 32]
                    if len(entry_data) != 32:
                        continue

                    user = entry_data[0]
                    name = entry_data[1:9].decode('ascii', errors='replace').strip()
                    ext = entry_data[9:12].decode('ascii', errors='replace').strip()
                    ex = entry_data[12]
                    s1 = entry_data[13]
                    xh = entry_data[14]
                    rc = entry_data[15]
                    if self.dpb.dsm <= 255:
                        blks = list(entry_data[16:32])
                    else:
                        blks = [struct.unpack('<H', entry_data[i:i+2])[0] for i in range(16, 32, 2)]
                    blks = [b for b in blks if b != 0]
                    attributes_raw = {
                        't1': entry_data[9] & 0xFF,
                        't2': entry_data[10] & 0xFF,
                        't3': entry_data[11] & 0xFF,
                    }

                    entry = CPMDirectoryEntry(
                        user=user, name=name, ext=ext, ex=ex, s1=s1, xh=xh, rc=rc,
                        blks=blks, attributes_raw=attributes_raw
                    )
                    entries.append(entry)
            except Exception as e:
                self.logger.error(f"Failed to read directory sector at CP/M track {current_cpm_track}, logical sector {logical_sector_idx}: {e}")
                break

            logical_spt = self._get_logical_spt(current_cpm_track)
            if logical_spt > 0:
                logical_sector_idx += 1
                if logical_sector_idx >= logical_spt:
                    logical_sector_idx = 0
                    current_cpm_track += 1
            else: # Should not happen, but as a safeguard
                break

        return entries
    
    def get_validity_score(self) -> int:
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        score = 0
        if not self.disk or not self.disk.physical_format or not self.dpb:
            self.logger.debug("Score: 0 (No disk, physical format, or DPB for validation.)")
            return 0

        try:
            self._initialize_parameters()
            score += 5  # Small bonus for having a DPB to test against.

            # Perform a raw check on the first directory sector for plausibility
            first_dir_sector_data = self._read_logical_sector(self.dpb.off, 0)

            # Case 1: Freshly formatted disk (all 0xE5)
            if all(b == 0xE5 for b in first_dir_sector_data):
                score += 75  # High confidence
                final_score = min(100, score)
                self.logger.info(f"CP/M validation score (empty formatted): {final_score}")
                self._cached_validity_score = final_score
                return final_score

            # Case 2: Check structure of first few entries against garbage/other filesystems
            plausible_entries = 0
            entries_to_check = min(len(first_dir_sector_data) // 32, 4)
            if entries_to_check == 0:
                self.logger.debug("Score: 0 (First directory sector is too small or unreadable)")
                return 0

            for i in range(entries_to_check):
                entry_bytes = first_dir_sector_data[i*32 : (i+1)*32]
                user_num = entry_bytes[0]

                if user_num == 0xE5: # Deleted is plausible
                    plausible_entries += 1
                    continue
                
                if 0 <= user_num <= 15:
                    name_and_ext = entry_bytes[1:12]
                    
                    # An active entry should not have a fully zeroed-out name/ext
                    if all(b == 0 for b in name_and_ext):
                        continue # Not plausible

                    # All characters in name/ext must be valid 7-bit printable ASCII
                    # when the high bit is ignored.
                    is_valid_chars = all(0x20 <= (b & 0x7F) <= 0x7E for b in name_and_ext)
                    if is_valid_chars:
                        plausible_entries += 1
                # Any other user_num is not plausible for an active/deleted entry
            
            # This is a strong gate: if the first few entries don't look right, it's not CP/M.
            if plausible_entries < 1: # Require at least one plausible entry
                self.logger.debug(f"Score: 0 (Found {plausible_entries}/{entries_to_check} plausible entries on raw check)")
                self._cached_validity_score = 0
                return 0

            # If raw check passes, proceed with full parsing and scoring
            score += 40
            
            entries = self._read_directory_entries()
            active_entries = [e for e in entries if not e.is_deleted() and 0 <= e.user <= 15]
            if active_entries:
                score += 20
            
            # Only add the DPB self-consistency bonus if we have other evidence.
            if score > 50:
                dir_blocks = self.dpb.directory_blocks
                if 0 < dir_blocks <= 16:
                    al0_bits = bin(self.dpb.al0)[2:].zfill(8)
                    al1_bits = bin(self.dpb.al1)[2:].zfill(8)
                    dir_bits_str = (al0_bits + al1_bits)[:dir_blocks]
                    
                    if dir_bits_str.count('1') == dir_blocks:
                        score += 25

            final_score = min(100, int(score))
            self.logger.info(f"CP/M validation score: {final_score}")
            self._cached_validity_score = final_score
            return final_score
        except Exception as e:
            self.logger.debug(f"get_validity_score check failed with exception: {e}")
            self._cached_validity_score = 0
            return 0

    def list_directory(self, path: str) -> List[FileInfo]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as CP/M.")
        if path != '/':
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
            # Sort by the full extent number (xh:ex)
            group.sort(key=lambda e: (e.xh << 8) | e.ex)
            
            # This simple sum is correct now that the DPB is fixed
            total_rc = sum(e.rc for e in group)
            size = total_rc * CPM_SECTOR_SIZE
            
            attr = group[0].get_attributes() if group else "-"
            files.append(FileInfo(name=full_name, size=size, is_dir=False, datetime=datetime.datetime(1978, 1, 1), attributes=attr, starting_cluster=0, extra_data=group))

        return files

    def _read_block(self, block_num: int) -> bytes:
        if block_num == 0:
            return b''  # Skip invalid block
        if not self.dpb: raise ValueError("DPB not set.")

        logical_sectors_per_block = self.dpb.block_size // CPM_SECTOR_SIZE
        data = bytearray()
        
        # Use the robust block_to_track_sector to find the starting point
        current_cpm_track, logical_sector_on_track = self._block_to_track_sector(block_num)

        for _ in range(logical_sectors_per_block):
            try:
                data.extend(self._read_logical_sector(current_cpm_track, logical_sector_on_track))
            except ValueError:
                # If we run off the end of the disk, pad with zeros
                self.logger.warning(f"Read for block {block_num} went past valid disk sectors. Padding with nulls.")
                data.extend(bytes(CPM_SECTOR_SIZE))

            logical_sector_on_track += 1
            spt = self._get_logical_spt(current_cpm_track)
            if spt > 0 and logical_sector_on_track >= spt:
                logical_sector_on_track = 0
                current_cpm_track += 1
        return bytes(data)

    def read_file(self, path: str) -> bytes:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as CP/M.")
        
        path = path.lstrip('/')
        specified_user: Optional[int] = None
        filename: str

        if ':' in path:
            try:
                user_part, file_part = path.split(':', 1)
                if user_part.startswith('U') and user_part[1:].isdigit():
                    specified_user = int(user_part[1:])
                    filename = file_part
                else:
                    filename = path 
            except ValueError:
                filename = path 
        else:
            filename = path
        
        if '.' not in filename:
            raise ValueError(f"Invalid filename format: {filename}")

        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()

        # Group all extents for the requested file
        all_file_groups = self._group_extents(self._cached_directory)
        matching_groups = []
        search_filename_key = filename.upper()
        for (user, file_key), group in all_file_groups.items():
            if file_key == search_filename_key and (specified_user is None or user == specified_user):
                matching_groups.append(group)

        if not matching_groups:
            raise FileNotFoundError(f"File {path} not found")
        if len(matching_groups) > 1:
            self.logger.warning(f"File '{filename}' exists for multiple users; reading from lowest user number.")
            matching_groups.sort(key=lambda g: g[0].user)
        
        group = matching_groups[0]
        
        data = b''
        _name_part, ext_part = filename.split('.', 1)
        text_exts = ['ASM', 'PRN', 'BAS', 'TXT', 'DOC', 'HEX']
        is_text = ext_part.upper() in text_exts
        for entry in group:
            logical_records = entry.rc
            extent_data = b''
            for block in entry.blks:
                if block != 0:
                    block_data = self._read_block(block)
                    extent_data += block_data
            used_data = extent_data[:logical_records * CPM_SECTOR_SIZE]
            if is_text:
                used_data = bytes(b & 0x7F for b in used_data)
            data += used_data
        
        return data.rstrip(b'\x1A')

    def _try_derive_dpb(self) -> bool:
        pf = self.disk.physical_format
        if pf.cylinders != 77 or pf.heads != 1 or pf.rpm != 360:
            return False
        tfs = pf.track_formats
        if len(tfs) == 1:
            tf = tfs[0]
            if tf.encoding == "FM" and tf.rate in (250, 300, 500) and tf.sectors_per_track == 26 and tf.bytes_per_sector == 128:
                self.dpb = CPMDiskParameterBlock(
                    spt=26, bsh=3, blm=7, exm=0, dsm=242, drm=63, al0=0xC0, al1=0x00, cks=0, off=2
                )
                return True
        elif len(tfs) == 2:
            tf0 = tfs[0]
            tf1 = tfs[1]
            if (tf0.track_start == 0 and tf0.track_end == 0 and tf0.encoding == "FM" and tf0.rate in (250, 300, 500) and
                tf0.sectors_per_track == 26 and tf0.bytes_per_sector == 128 and
                tf1.track_start == 1 and tf1.track_end == 76 and tf1.encoding == "MFM" and tf1.rate == 500 and
                tf1.sectors_per_track == 26 and tf1.bytes_per_sector == 256):
                self.dpb = CPMDiskParameterBlock(
                    spt=52, bsh=4, blm=15, exm=1, dsm=242, drm=63, al0=0xC0, al1=0x00, cks=0, off=2
                )
                return True
        return False

    def _parse_cpm_path(self, path: str) -> Tuple[int, str]:
        path_to_parse = path.upper()
        
        if path_to_parse.startswith("/"):
            path_to_parse = path_to_parse[1:]

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
                    filename_part = path_to_parse 
            else:
                filename_part = path_to_parse
        
        name_parts = filename_part.split('.', 1)
        base = name_parts[0][:8] 
        ext = name_parts[1][:3] if len(name_parts) > 1 else ""
        
        parsed_filename = base
        if ext:
            parsed_filename += "." + ext
        
        if not parsed_filename:
             raise ValueError(f"Empty filename derived from path '{path}'")
        if len(parsed_filename) > 12:
             self.logger.warning(f"Parsed filename '{parsed_filename}' from '{path}' is longer than typical 8.3.")

        return user, parsed_filename.strip()

    def _block_to_track_sector(self, block_num: int) -> Tuple[int, int]:
        if not self.dpb: raise ValueError("DPB not set.")
        if self.dpb.block_size == 0: raise ValueError("DPB.block_size is zero.")
        
        logical_128byte_sectors_per_alloc_block = self.dpb.block_size // CPM_SECTOR_SIZE
        
        # The data area (including the directory blocks) starts immediately after the reserved tracks.
        # Block 0 is the first block right after the reserved tracks.
        # The previous implementation incorrectly added a directory offset, causing all writes to be misplaced.
        start_logical_128byte_sector_for_block_global = (block_num * logical_128byte_sectors_per_alloc_block)
        
        current_track = self.dpb.off
        sectors_remaining = start_logical_128byte_sector_for_block_global
        
        while True:
            spt_for_current_track = self._get_logical_spt(current_track)
            if spt_for_current_track == 0:
                raise ValueError(f"Logical SPT for track {current_track} is zero, cannot map block.")
            if sectors_remaining < spt_for_current_track:
                return current_track, sectors_remaining
            sectors_remaining -= spt_for_current_track
            current_track += 1

    def write_file(self, path: str, data: bytes) -> None:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem not valid")

        # 1. Preparation & Path Parsing
        user, parsed_filename = self._parse_cpm_path(path)
        base_name, ext_name = (parsed_filename.split('.', 1) + [''])[:2]
        
        try:
            self.delete(path)
        except FileNotFoundError:
            pass 

        # 2. Resource Calculation based on the simple 16KB extent logic
        if not self.dpb: raise ValueError("DPB not set.")
        block_size = self.dpb.block_size
        if block_size == 0: raise IOError("Block size is zero, cannot write file.")

        RECORDS_PER_EXTENT = 128
        
        num_records_total = (len(data) + CPM_SECTOR_SIZE - 1) // CPM_SECTOR_SIZE if data else 0
        num_dir_entries_needed = (num_records_total + RECORDS_PER_EXTENT - 1) // RECORDS_PER_EXTENT if data else 0
        num_blocks_needed = (len(data) + block_size - 1) // block_size if data else 0

        self.logger.info(f"Writing '{path}': {len(data)} bytes, needs {num_blocks_needed} blocks, {num_dir_entries_needed} dir entries.")

        # 3. Resource Allocation
        self._cached_directory = self._read_directory_entries()
        self._load_allocation_map()

        free_blocks = sorted(list(set(range(self.dpb.dsm + 1)) - self._cached_allocation_map))
        if len(free_blocks) < num_blocks_needed:
            raise IOError(f"Not enough free space. Required: {num_blocks_needed}, Available: {len(free_blocks)}.")
        blocks_to_use = free_blocks[:num_blocks_needed]

        free_dir_slots = [i for i, e in enumerate(self._cached_directory) if e.is_deleted()]
        if len(free_dir_slots) < num_dir_entries_needed:
            raise IOError(f"Directory is full. Required: {num_dir_entries_needed}, Available: {len(free_dir_slots)}.")
        dir_slots_to_use = free_dir_slots[:num_dir_entries_needed]
        
        # 4. Create and Write Directory Entries
        records_rem = num_records_total
        blocks_consumed = 0
        sectors_to_modify = defaultdict(bytearray)

        for i in range(num_dir_entries_needed):
            rc = min(records_rem, RECORDS_PER_EXTENT)
            records_rem -= rc
            
            bytes_in_this_extent = rc * CPM_SECTOR_SIZE
            blocks_for_this_extent = (bytes_in_this_extent + block_size - 1) // block_size
            extent_blocks = blocks_to_use[blocks_consumed : blocks_consumed + blocks_for_this_extent]
            blocks_consumed += blocks_for_this_extent
            
            ex = i

            new_entry = CPMDirectoryEntry(
                user=user, name=base_name, ext=ext_name,
                ex=ex, s1=0, xh=0, rc=rc,
                blks=extent_blocks, attributes_raw={}
            )
            entry_bytes = self._format_entry_to_bytes(new_entry)
            
            cpm_track, log_sec, offset = self._map_dir_entry_index_to_location(dir_slots_to_use[i])
            key = (cpm_track, log_sec)

            if key not in sectors_to_modify:
                sectors_to_modify[key] = bytearray(self._read_logical_sector(cpm_track, log_sec))
            sectors_to_modify[key][offset : offset+32] = entry_bytes

        for (cpm_track, log_sec), mod_data in sectors_to_modify.items():
            self._write_logical_sector(cpm_track, log_sec, bytes(mod_data))

        # 5. Write File Data
        data_to_write = bytearray(data)
        if len(data_to_write) > 0 and len(data_to_write) % CPM_SECTOR_SIZE != 0:
            padding_needed = CPM_SECTOR_SIZE - (len(data_to_write) % CPM_SECTOR_SIZE)
            data_to_write.extend([0x1A] * padding_needed)

        for i, block_num in enumerate(blocks_to_use):
            chunk = data_to_write[i * block_size : (i + 1) * block_size]
            self._write_block(block_num, bytes(chunk))
        
        self.logger.info(f"Successfully wrote file '{path}'.")
        self._cached_directory = None
        self._cached_allocation_map = None
        self.disk.flush()

    def create_directory(self, path: str) -> None:
        self.logger.warning("CP/M 2.2 does not support traditional directory creation via this method.")
        raise NotImplementedError("CP/M create_directory not applicable in the standard sense.")

    def delete(self, path: str) -> None:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD: raise IOError("Filesystem not valid")
        
        user, parsed_filename = self._parse_cpm_path(path)

        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()

        indices_to_delete = []
        for i, entry in enumerate(self._cached_directory):
            if not entry.is_deleted() and entry.user == user and entry.get_filename().upper() == parsed_filename.upper():
                indices_to_delete.append(i)

        if not indices_to_delete:
            raise FileNotFoundError(f"File '{path}' not found.")

        sectors_to_modify = defaultdict(bytearray)
        for index in indices_to_delete:
            cpm_track, log_sec, offset = self._map_dir_entry_index_to_location(index)
            key = (cpm_track, log_sec)
            
            if key not in sectors_to_modify:
                sectors_to_modify[key] = bytearray(self._read_logical_sector(cpm_track, log_sec))

            sectors_to_modify[key][offset] = 0xE5

        for (cpm_track, log_sec), data in sectors_to_modify.items():
            self._write_logical_sector(cpm_track, log_sec, bytes(data))

        self.logger.info(f"Deleted file '{path}' by marking {len(indices_to_delete)} directory entries.")
        self._cached_directory = None
        self._cached_allocation_map = None
        self.disk.flush()

    def delete_recursive(self, path: str) -> bool:
        try:
            self.delete(path)
            return True
        except Exception:
            return False

    def get_allocated_units(self) -> List[int]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD or not self.dpb: return []
        if self._cached_allocation_map is None:
            self._load_allocation_map()
        
        return sorted(list(self._cached_allocation_map)) if self._cached_allocation_map else []
    
    def _map_dir_entry_index_to_location(self, index: int) -> Tuple[int, int, int]:
        """Maps a flat directory entry index to its on-disk location."""
        if not self.dpb: raise ValueError("DPB not set.")
        
        logical_dir_sector_idx = index // CPM_DIRECTORY_ENTRIES_PER_SECTOR
        offset_in_sector = (index % CPM_DIRECTORY_ENTRIES_PER_SECTOR) * 32

        current_cpm_track = self.dpb.off
        logical_sectors_left = logical_dir_sector_idx

        while True:
            spt = self._get_logical_spt(current_cpm_track)
            if spt == 0:
                raise IOError(f"Cannot map directory entry: SPT for CP/M track {current_cpm_track} is zero.")
            if logical_sectors_left < spt:
                return current_cpm_track, logical_sectors_left, offset_in_sector
            logical_sectors_left -= spt
            current_cpm_track += 1

    def _write_block(self, block_num: int, data: bytes) -> None:
        if not self.dpb: raise ValueError("DPB not available for write_block.")
        if block_num <= 0 or block_num > self.dpb.dsm:
            raise ValueError(f"Invalid block number {block_num} for writing.")
        
        expected_size = self.dpb.block_size
        if len(data) > expected_size:
            data = data[:expected_size]
        elif len(data) < expected_size:
            data = data.ljust(expected_size, b'\x00') # Pad with nulls

        logical_sectors_per_block = expected_size // CPM_SECTOR_SIZE
        start_cpm_track, start_logical_sector_on_track = self._block_to_track_sector(block_num)

        current_cpm_track = start_cpm_track
        current_logical_sector = start_logical_sector_on_track

        for i in range(logical_sectors_per_block):
            sector_data = data[i * CPM_SECTOR_SIZE : (i + 1) * CPM_SECTOR_SIZE]
            self._write_logical_sector(current_cpm_track, current_logical_sector, sector_data)
            
            current_logical_sector += 1
            spt = self._get_logical_spt(current_cpm_track)
            if spt > 0 and current_logical_sector >= spt:
                current_logical_sector = 0
                current_cpm_track += 1

    def _format_entry_to_bytes(self, entry: CPMDirectoryEntry) -> bytes:
        if not self.dpb: raise ValueError("DPB not available.")
        
        entry_bytes = bytearray(32)
        entry_bytes[0] = entry.user
        
        name_padded = entry.name.upper().ljust(8, ' ')
        ext_padded = entry.ext.upper().ljust(3, ' ')
        
        entry_bytes[1:9] = name_padded.encode('ascii')
        entry_bytes[9:12] = ext_padded.encode('ascii')

        if 't1' in entry.attributes_raw: entry_bytes[9] |= (entry.attributes_raw.get('t1', 0) & 0x80)
        if 't2' in entry.attributes_raw: entry_bytes[10] |= (entry.attributes_raw.get('t2', 0) & 0x80)
        if 't3' in entry.attributes_raw: entry_bytes[11] |= (entry.attributes_raw.get('t3', 0) & 0x80)

        entry_bytes[12] = entry.ex
        entry_bytes[13] = entry.s1
        entry_bytes[14] = entry.xh
        entry_bytes[15] = entry.rc

        if self.dpb.dsm > 255: # 2-byte block pointers
            for i, block_num in enumerate(entry.blks):
                if i < 8: struct.pack_into("<H", entry_bytes, 16 + i * 2, block_num)
        else: # 1-byte block pointers
            for i, block_num in enumerate(entry.blks):
                if i < 16: entry_bytes[16 + i] = block_num
        
        return bytes(entry_bytes)

    def _load_allocation_map(self):
        if self.get_validity_score() < self.VALIDITY_THRESHOLD or not self.dpb: return
        
        self.logger.debug("Building CP/M allocation map by scanning directory entries...")
        raw_entries = self._read_directory_raw()
        if raw_entries is None : 
             self._cached_allocation_map = set()
             return

        used_blocks = set()
        for i in range(self.dpb.directory_blocks):
            used_blocks.add(i)

        for entry in raw_entries:
            if entry.is_deleted():
                continue
            for block_num in entry.blks:
                if block_num > 0 and block_num <= self.dpb.dsm: 
                    used_blocks.add(block_num)
        self._cached_allocation_map = used_blocks
        self.logger.debug(f"Built allocation map with {len(used_blocks)} used blocks.")


    def get_free_space(self) -> Tuple[int, int]:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD or not self.dpb: return 0, 0
        
        total_alloc_blocks_on_disk = self.dpb.dsm + 1 
        total_data_bytes_possible = total_alloc_blocks_on_disk * self.dpb.block_size
        
        allocated_block_count = len(self.get_allocated_units()) 
        
        free_blocks = total_alloc_blocks_on_disk - allocated_block_count
        free_bytes = free_blocks * self.dpb.block_size
        
        return free_bytes, total_data_bytes_possible

    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None:
        if not isinstance(profile.filesystem_config, CPMDiskParameterBlock):
            raise ValueError("FormatProfile for CP/M must contain a CPMDiskParameterBlock.")
        
        original_dpb = self.dpb
        self.dpb = profile.filesystem_config
        try:
            self._initialize_parameters() 
        except ValueError as e:
            self.dpb = original_dpb 
            raise ValueError(f"Failed to re-initialize parameters with new DPB for format: {e}") from e


        self.logger.info(f"Formatting disk with CP/M profile: {profile.name}")

        num_reserved_cpm_tracks = self.dpb.off

        self.logger.info(f"Clearing {num_reserved_cpm_tracks} reserved CP/M tracks...")

        for i in range(num_reserved_cpm_tracks):
            phys_cyl, phys_head = self._cpm_track_to_chs_coords(i)
            spt = self.disk.physical_format.get_sectors_per_track(phys_cyl, phys_head)
            bps = self.disk.physical_format.get_bytes_per_sector(phys_cyl, phys_head)
            fill_data = bytes([0xE5] * bps)
            for s in range(1, spt + 1):
                try:
                    self.disk.write_sector(phys_cyl, phys_head, s, fill_data)
                except Exception as e:
                    self.logger.error(f"Error writing to reserved track {i} (C:{phys_cyl} H:{phys_head} S:{s}): {e}")
                    raise IOError("Failed to clear system tracks during format") from e

        dir_logical_128b_sectors_count = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        self.logger.info(f"Clearing {dir_logical_128b_sectors_count} logical 128-byte sectors for directory...")
        
        blank_sector_128b = bytes([0xE5] * CPM_SECTOR_SIZE)
        current_cpm_track_idx = self.dpb.off
        current_logical_128b_sec_on_cpm_track_idx = 0

        for _ in range(dir_logical_128b_sectors_count):
            try:
                self._write_logical_sector(current_cpm_track_idx, current_logical_128b_sec_on_cpm_track_idx, blank_sector_128b)
            except Exception as e:
                 self.logger.error(f"Error writing to directory area CP/M_T:{current_cpm_track_idx} Log.S:{current_logical_128b_sec_on_cpm_track_idx}: {e}")
                 raise IOError("Failed to clear directory area during format") from e

            logical_spt_for_dir_track = self._get_logical_spt(current_cpm_track_idx)
            current_logical_128b_sec_on_cpm_track_idx += 1
            if logical_spt_for_dir_track > 0 and current_logical_128b_sec_on_cpm_track_idx >= logical_spt_for_dir_track:
                current_logical_128b_sec_on_cpm_track_idx = 0
                current_cpm_track_idx += 1
        
        self.logger.info("Data area not explicitly cleared (standard for CP/M format).")

        # CRITICAL FIX: After formatting, the internal state must be completely reset.
        # The allocation map must be cleared and rebuilt to reflect that only
        # the directory blocks are now "in use".
        self._cached_directory = [] 
        self._cached_allocation_map = set(range(self.dpb.directory_blocks)) 
        
        self.disk.flush()
        self.logger.info("CP/M formatting complete (system tracks and directory cleared).")


    def get_display_info(self) -> Dict[str, str]:
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

    def get_disk_map_layout(self) -> Dict[str, Any]:
        if not self.dpb or not self.disk or not self.disk.physical_format or not self._init_completed:
            return {}

        allocated_data_blocks = self.get_allocated_units()

        def get_cpm_sector_type_chs(cylinder: int, head: int, sector: int) -> str:
            if self.disk.physical_format.heads > 1:
                cpm_track = cylinder * self.disk.physical_format.heads + head
            else:
                cpm_track = cylinder

            if cpm_track < self.dpb.off:
                return "system"

            track_format = self.disk.physical_format.get_track_format(cylinder, head)
            if not track_format.sector_translation_table:
                return "unknown" 
            
            try:
                logical_sector_order_on_track = track_format.sector_translation_table.index(sector)
            except ValueError:
                return "unknown" 
            
            global_logical_sector_count = 0
            for t in range(cpm_track):
                global_logical_sector_count += self._get_logical_spt(t)
            global_logical_sector_count += logical_sector_order_on_track

            dir_logical_sectors = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
            reserved_logical_sectors = 0
            for t in range(self.dpb.off):
                reserved_logical_sectors += self._get_logical_spt(t)

            if global_logical_sector_count < reserved_logical_sectors + dir_logical_sectors:
                return "directory"
            
            data_logical_sector_offset = global_logical_sector_count - (reserved_logical_sectors + dir_logical_sectors)
            if self.dpb.block_size == 0: return "data_free"
            
            logical_sectors_per_block = self.dpb.block_size // CPM_SECTOR_SIZE
            cpm_alloc_block_num = data_logical_sector_offset // logical_sectors_per_block if logical_sectors_per_block > 0 else 0
            
            return "data_used" if cpm_alloc_block_num in allocated_data_blocks else "data_free"

        def get_cpm_sector_type_lba(phys_lba: int) -> str:
            try:
                cylinder, head, sector = self.disk.physical_format.lba_to_chs(phys_lba)
                return get_cpm_sector_type_chs(cylinder, head, sector)
            except Exception as e:
                self.logger.error(f"Error converting LBA {phys_lba} to CHS for disk map: {e}")
                return "unknown"

        legend_colors = { "System Tracks": "#A0A0A0", "Directory": "#FFFF00", "Used Data Block": "#FF00FF", "Free Data Block": "#808080" }
        legend = [("System Tracks", legend_colors["System Tracks"]), ("Directory", legend_colors["Directory"]), ("Used Data Block", legend_colors["Used Data Block"]), ("Free Data Block", legend_colors["Free Data Block"])]
        type_map = { "system": legend_colors["System Tracks"], "directory": legend_colors["Directory"], "data_used": legend_colors["Used Data Block"], "data_free": legend_colors["Free Data Block"], "unknown": "#008B8B" }
        
        data_track_format = self.disk.physical_format.get_track_format(self.dpb.off, 0)
        alloc_unit_phys_sectors = self.dpb.block_size // data_track_format.bytes_per_sector if data_track_format.bytes_per_sector > 0 else 1

        return { 
            'legend': legend, 
            'get_sector_type_chs': get_cpm_sector_type_chs, 
            'get_sector_type': get_cpm_sector_type_lba,
            'allocation_unit_size_sectors': alloc_unit_phys_sectors, 
            'type_color_map': type_map 
        }

    def get_specific_config(self) -> Optional[CPMDiskParameterBlock]:
        return self.dpb

    def _build_physical_sector_order_for_tf(self, tf):
        spt   = tf.sectors_per_track
        inter = tf.interleave if getattr(tf, "interleave", 1) and tf.interleave > 0 else 1
        start = getattr(tf, "id_start", 1)
        order, used, idx = [], [False]*spt, 0
        for i in range(spt):
            order.append(start + idx); used[idx] = True
            if i < spt - 1:
                idx = (idx + inter) % spt
                while used[idx]: idx = (idx + 1) % spt
        return order

    def _ensure_sector_translation_tables(self):
        pf = self.disk.physical_format
        for c in range(pf.cylinders):
            for h in range(pf.heads):
                tf = pf.get_track_format(c, h)
                if not getattr(tf, "sector_translation_table", None):
                    tf.sector_translation_table = self._build_physical_sector_order_for_tf(tf)
