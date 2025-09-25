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
        attr_str_parts = []
        if self.attributes_raw.get('t1', 0) & 0x80: attr_str_parts.append("R") # Read-Only
        if self.attributes_raw.get('t2', 0) & 0x80: attr_str_parts.append("S") # System
        if self.attributes_raw.get('t3', 0) & 0x80: attr_str_parts.append("A") # Archive
        return "-".join(attr_str_parts) if attr_str_parts else "-"


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
            try:
                sector_data = self._read_logical_sector(current_cpm_track, logical_sector_idx)
                if all(b == 0xE5 for b in sector_data):
                    break  # Stop at first all-E5 sector, end of directory
                for i in range(CPM_DIRECTORY_ENTRIES_PER_SECTOR):
                    offset = i * 32
                    if offset + 32 > len(sector_data):
                        self.logger.warning(f"Invalid sector data size at CP/M track {current_cpm_track}, logical sector {logical_sector_idx}")
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
                        user=user,
                        name=name,
                        ext=ext,
                        ex=ex,
                        s1=s1,
                        xh=xh,
                        rc=rc,
                        blks=blks,
                        attributes_raw=attributes_raw
                    )
                    entries.append(entry)
            except Exception as e:
                self.logger.error(f"Failed to read directory sector at CP/M track {current_cpm_track}, logical sector {logical_sector_idx}: {e}")
                break

            logical_spt = self._get_logical_spt(current_cpm_track)
            logical_sector_idx += 1
            if logical_sector_idx >= logical_spt:
                logical_sector_idx = 0
                current_cpm_track += 1

        return entries
    
    def get_validity_score(self) -> int:
        if self._cached_validity_score is not None:
            return self._cached_validity_score

        score = 0
        if not self.disk or not self.disk.physical_format:
            self.logger.debug("Score: 0 (No disk or physical format for validation.)")
            return 0
        if not self.dpb:
            self.logger.debug("Score: 0 (No DPB provided for validation.)")
            return 0

        try:
            self._initialize_parameters()
            score += 10  # DPB and geometry are present and initialized

            entries = self._read_directory_entries()
            if not entries:
                self.logger.debug("Score: 0 (No directory entries found.)")
                return 0
            score += 20 # Directory is readable

            # Directory entry analysis
            valid_count = 0
            bad_name_count = 0
            user_numbers = set()
            active_entries = 0

            for entry in entries:
                if entry.is_deleted() or entry.user > 15:
                    if entry.user == 0xE5: valid_count += 1 # Deleted is a valid state
                    continue
                
                active_entries += 1
                user_numbers.add(entry.user)
                
                # Check for printable 7-bit ASCII characters. This is a strong indicator.
                name_valid = all(32 <= (ord(c) & 0x7F) <= 126 for c in entry.name.strip() + entry.ext.strip())
                
                if not name_valid:
                    bad_name_count += 1

                if name_valid and (entry.name.strip() or entry.ext.strip()):
                    blocks_valid = all(0 <= b <= self.dpb.dsm for b in entry.blks)
                    rc_valid = 0 <= entry.rc <= 128
                    if blocks_valid and rc_valid:
                        valid_count += 1

            valid_ratio = valid_count / len(entries) if entries else 0
            if valid_ratio > 0.3:
                score += 30 * valid_ratio # Up to 30 points for plausible entries
            
            # Penalize for bad filenames in active entries
            if active_entries > 0:
                bad_name_ratio = bad_name_count / active_entries
                penalty = bad_name_ratio * 60 # Heavy penalty for non-ASCII names
                self.logger.debug(f"Applying filename validity penalty of {int(penalty)} points.")
                score -= penalty


            if len(user_numbers) >= 1:
                score += 15 # At least one active user number found

            # Allocation bitmap check
            dir_blocks = self.dpb.directory_blocks
            if dir_blocks > 16:
                 self.logger.debug(f"Directory blocks ({dir_blocks}) exceed AL0/AL1 capacity (16). Skipping AL check.")
            else:
                al0_bits = bin(self.dpb.al0)[2:].zfill(8)
                al1_bits = bin(self.dpb.al1)[2:].zfill(8)
                dir_bits_str = (al0_bits + al1_bits)[:dir_blocks]
                dir_bits_set = dir_bits_str.count('1')
                
                # Allocation bitmap is a strong indicator
                if dir_bits_set == dir_blocks and all(b == '1' for b in dir_bits_str):
                    score += 25
                elif dir_bits_set == dir_blocks: # Bits are correct but not contiguous
                    score += 15
                else:
                    self.logger.debug(f"Allocation bitmap mismatch: {dir_bits_set} != {dir_blocks} directory blocks.")
            
            final_score = max(0, int(score))
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
                key = (entry.user, entry.name.strip(), entry.ext.strip())
                file_groups[key].append(entry)

        files = []
        for key, group in file_groups.items():
            user, name, ext = key
            group.sort(key=lambda e: e.ex + (e.xh << 5))  # Extent number: ex low 5 bits, xh high bits (CP/M 2.2 S2 as xh)
            total_rc = sum(e.rc for e in group)
            size = total_rc * CPM_SECTOR_SIZE
            attr = group[0].get_attributes() if group else "-"
            full_name = f"U{user}:{name}.{ext}"
            files.append(FileInfo(name=full_name, size=size, is_dir=False, datetime=datetime.datetime(1978, 1, 1), attributes=attr, starting_cluster=0, extra_data=group))

        return files

    def _read_block(self, block_num: int) -> bytes:
        if block_num == 0:
            return b''  # Skip invalid block
        logical_sectors_per_block = self.dpb.block_size // CPM_SECTOR_SIZE
        logical_sector_start = block_num * logical_sectors_per_block
        data = b''
        for i in range(logical_sectors_per_block):
            cpm_track = self.dpb.off + (logical_sector_start // self.dpb.spt)
            logical_sector_on_track = (logical_sector_start % self.dpb.spt)
            data += self._read_logical_sector(cpm_track, logical_sector_on_track)
            logical_sector_start += 1
        return data

    def read_file(self, path: str) -> bytes:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD:
            raise IOError("Filesystem is not valid or not recognized as CP/M.")
        # Parse path: /Uxx:NAME.EXT or Uxx:NAME.EXT or NAME.EXT (assume U0)
        path = path.lstrip('/')
        if ':' in path:
            user_str, filename = path.split(':', 1)
            user = int(user_str.lstrip('U'))
        else:
            user = 0
            filename = path
        if '.' in filename:
            name, ext = filename.split('.', 1)
            name = name.upper().ljust(8)
            ext = ext.upper().ljust(3)
        else:
            raise ValueError(f"Invalid filename format: {filename}")

        # Find the group
        if self._cached_directory is None:
            self._cached_directory = self._read_directory_entries()
        group = [e for e in self._cached_directory if not e.is_deleted() and e.user == user and e.name.strip() == name.strip() and e.ext.strip() == ext.strip()]
        if not group:
            raise FileNotFoundError(f"File {path} not found")
        group.sort(key=lambda e: e.ex + (e.xh << 5))

        # Read data from all extents
        data = b''
        text_exts = ['ASM', 'PRN', 'BAS', 'TXT', 'DOC', 'HEX']
        is_text = ext in text_exts
        for entry in group:
            logical_records = entry.rc
            extent_data = b''
            for block in entry.blks:
                block_data = self._read_block(block)
                extent_data += block_data
            used_data = extent_data[:logical_records * CPM_SECTOR_SIZE]
            if is_text:
                used_data = bytes(b & 0x7F for b in used_data)
            used_data = used_data.rstrip(b'\x1A')
            data += used_data
        return data

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
        dir_logical_sectors = ((self.dpb.drm + 1) * 32) // CPM_SECTOR_SIZE
        
        # Data area starts *after* the directory.
        start_logical_128byte_sector_for_block_global = (block_num * logical_128byte_sectors_per_alloc_block) + dir_logical_sectors
        
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
        if self.get_validity_score() < self.VALIDITY_THRESHOLD: raise IOError("Filesystem not valid")
        self.logger.warning("CP/M write_file not fully implemented yet.")
        raise NotImplementedError("CP/M write_file not implemented.")

    def create_directory(self, path: str) -> None:
        self.logger.warning("CP/M 2.2 does not support traditional directory creation via this method.")
        raise NotImplementedError("CP/M create_directory not applicable in the standard sense.")

    def delete(self, path: str) -> None:
        if self.get_validity_score() < self.VALIDITY_THRESHOLD: raise IOError("Filesystem not valid")
        self.logger.warning("CP/M delete not fully implemented yet.")
        raise NotImplementedError("CP/M delete not implemented.")

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
