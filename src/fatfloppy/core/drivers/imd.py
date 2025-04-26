# src/fatfloppy/core/drivers/imd.py
import struct
import datetime
import copy
import os
import re
from typing import List, Optional, Tuple, Dict, Any

from .base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat
from ..formats import FormatProfile
from ..utils.logging_config import get_logger
from ..._version import __version__ as fatfloppy_version

logger = get_logger("IMDImageDriver")

# IMD Sector Data Record Types
IMD_SECTOR_UNAVAILABLE = 0
IMD_SECTOR_NORMAL = 1
IMD_SECTOR_COMPRESSED = 2
IMD_SECTOR_NORMAL_DEL = 3
IMD_SECTOR_COMPRESSED_DEL = 4
IMD_SECTOR_NORMAL_ERR = 5
IMD_SECTOR_COMPRESSED_ERR = 6
IMD_SECTOR_NORMAL_DEL_ERR = 7
IMD_SECTOR_COMPRESSED_DEL_ERR = 8

# Map IMD Mode byte to Rate (kbps) and Encoding (FM/MFM)
# Note: IMD uses transfer rate (kbps), data rate is half for FM
IMD_MODE_MAP = {
    # Mode: (rate_kbps, encoding, density_hint)
    0: (500, "FM", "SD"),   # 500 kbps FM (Data rate 250 kbps) - Unusual? Check docs. Usually 250 kbps is FM. Let's assume 250 is intended data rate.
    1: (300, "FM", "SD"),   # 300 kbps FM (Data rate 150 kbps) - Very unusual.
    2: (250, "FM", "SD"),   # 250 kbps FM (Data rate 125 kbps) - Standard SD
    3: (500, "MFM", "HD/ED"), # 500 kbps MFM - Standard HD
    4: (300, "MFM", "HD"),    # 300 kbps MFM - Often HD drive in DD mode (360rpm)
    5: (250, "MFM", "DD"),    # 250 kbps MFM - Standard DD
}

# Map IMD Sector Size Code to bytes
IMD_SECTOR_SIZE_MAP = {
    0: 128,
    1: 256,
    2: 512,
    3: 1024,
    4: 2048,
    5: 4096,
    6: 8192,
}


class IMDFormatException(Exception):
    """Custom exception for IMD parsing errors."""
    pass


class IMDTrackInfo:
    """Helper class to store parsed track information."""
    def __init__(self, mode: int, cylinder: int, head_flags: int,
                 num_sectors: int, sector_size_code: int):
        self.mode = mode
        self.cylinder = cylinder
        self.head = head_flags & 1
        self.has_cyl_map = bool(head_flags & 0x80)
        self.has_head_map = bool(head_flags & 0x40)
        self.num_sectors = num_sectors
        self.sector_size_code = sector_size_code
        self.sector_size_map: Optional[Dict[int, int]] = None # Sector Num -> Size
        self.sector_size = self._get_base_sector_size()
        self.sector_num_map: List[int] = []
        self.sector_cyl_map: Optional[Dict[int, int]] = None # Sector Num -> Logical Cyl
        self.sector_head_map: Optional[Dict[int, int]] = None # Sector Num -> Logical Head
        self.sector_data_info: Dict[int, Tuple[int, int, int]] = {} # Sector Num -> (Offset, Type, DataSize)

    def _get_base_sector_size(self) -> int:
        if self.sector_size_code == 0xFF:
            return -1 # Variable size indicated by map
        size = IMD_SECTOR_SIZE_MAP.get(self.sector_size_code)
        if size is None:
            raise IMDFormatException(f"Invalid sector size code: {self.sector_size_code}")
        return size

    def get_sector_size(self, sector_num: int) -> int:
        """Get the size of a specific sector."""
        if self.sector_size_map:
            size = self.sector_size_map.get(sector_num)
            if size is None:
                 raise IMDFormatException(f"Sector {sector_num} not found in variable size map for C:{self.cylinder} H:{self.head}")
            return size
        elif self.sector_size != -1:
            return self.sector_size
        else:
            # Should have sector_size_map if base size is -1
            raise IMDFormatException(f"Inconsistent sector size information for C:{self.cylinder} H:{self.head}")


class IMDImageDriver(DiskIODriver):
    """
    Disk I/O Driver for ImageDisk (.IMD) floppy image files.
    """
    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.comment: str = ""
        self.imd_version: str = ""
        self.creation_date: Optional[datetime.datetime] = None
        self.tracks: Dict[Tuple[int, int], IMDTrackInfo] = {}
        self.image_data: bytearray = bytearray()
        self.dirty: bool = False
        self.file_loaded = False
        self.modified_sector_data: Dict[Tuple[int, int, int], bytes] = {}
        self.last_format_fill_byte: Optional[int] = None

        # --- Check if file exists before trying to load/parse ---
        if os.path.exists(self.file_path):
            try:
                self._load_and_parse_imd_file()
                self.file_loaded = True
            except FileNotFoundError: # Should not happen due to os.path.exists, but defensive
                self.logger.error(f"IMD file not found during load: {self.file_path}")
                raise
            except IMDFormatException as e:
                self.logger.error(f"Error parsing IMD file {self.file_path}: {e}")
                # Allow partial load? For now, fail.
                raise
            except Exception as e:
                self.logger.exception(f"Unexpected error loading IMD file {self.file_path}: {e}")
                raise
        else:
            # File doesn't exist, initialize empty state ready for format_imd
            self.logger.info(f"IMD file '{self.file_path}' not found. Initializing empty driver state.")
            # Basic defaults, format_imd will override most
            self.creation_date = datetime.datetime.now()
            self.imd_version = "IMD 1.18"
            self.comment = f"{self.creation_date.strftime('%d/%m/%Y %H:%M:%S')}\r\nFatFloppy v{fatfloppy_version}"
            self.file_loaded = False # Mark as not loaded from disk
            self.dirty = False # Not dirty until formatted
            # physical_format will be set by format_imd

    def _load_and_parse_imd_file(self):
        """Loads the entire IMD file and parses its structure."""
        self.logger.info(f"Loading IMD file: {self.file_path}")
        try:
            with open(self.file_path, "rb") as f:
                self.image_data = bytearray(f.read())
        except Exception as e:
            raise IMDFormatException(f"Cannot read file: {e}") from e

        if not self.image_data:
            raise IMDFormatException("IMD file is empty")

        # Find header end (0x1A)
        header_end_pos = -1
        for i, byte_val in enumerate(self.image_data):
            if byte_val == 0x1A:
                header_end_pos = i
                break
        if header_end_pos == -1:
            raise IMDFormatException("IMD header terminator (0x1A) not found")

        header_bytes = self.image_data[:header_end_pos]
        self.logger.debug(f"Header bytes length: {len(header_bytes)}, content: {header_bytes!r}")

        try:
            # Direct Splitting Approach
            first_colon_byte_pos = header_bytes.find(b':')
            self.logger.debug(f"First colon position: {first_colon_byte_pos}")

            if first_colon_byte_pos != -1:
                try:
                    self.imd_version = header_bytes[:first_colon_byte_pos].decode('ascii', errors='ignore').strip()
                except Exception:
                    self.imd_version = "[Version Decode Error]"
                self.logger.debug(f"Parsed version: '{self.imd_version}'")

                # Comment includes everything after the first colon
                comment_bytes_potential = header_bytes[first_colon_byte_pos + 1:]
                self.logger.debug(f"Comment bytes potential: {comment_bytes_potential!r}")
                try:
                    # Decode the whole block first
                    decoded_potential_comment = comment_bytes_potential.decode('cp437', errors='replace')
                    self.logger.debug(f"Decoded comment potential: '{decoded_potential_comment}'")

                    # Remove timestamp if present at the beginning (flexible pattern)
                    # Pattern: DD/MM/YYYY HH:MM:SS potentially followed by space or colon
                    timestamp_pattern = r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[:]?\s*"
                    comment_after_timestamp = re.sub(timestamp_pattern, '', decoded_potential_comment, count=1)

                    # Strip remaining common leading/trailing whitespace/newlines
                    self.comment = comment_after_timestamp.strip(' \t\r\n')

                except Exception as decode_err:
                     self.logger.warning(f"Could not decode/process comment part: {decode_err}")
                     self.comment = "[Comment Process Error]"

            else: # No colon found
                self.logger.warning("No colon found in header, attempting fallback parsing.")
                try:
                     # Assume version if short, comment if long
                     if len(header_bytes) < 30:
                         self.imd_version = header_bytes.decode('ascii', errors='ignore').strip()
                         self.comment = ""
                     else:
                         self.imd_version = ""
                         self.comment = header_bytes.decode('cp437', errors='replace').strip(' :\t\r\n')
                except Exception:
                      self.imd_version = "[Header Parse Error]"
                      self.comment = ""

        except Exception as e:
            self.logger.error(f"Unexpected error during header parsing: {e}")
            self.imd_version = "IMD Header Error"
            self.comment = "[Header Parse Error]"

        self.logger.debug(f"Final IMD Version: '{self.imd_version}'")
        self.logger.debug(f"Final IMD Comment: '{self.comment}'")

        # Parse track data
        offset = header_end_pos + 1
        max_cyl = -1
        max_head = -1
        parsed_track_count = 0 # Keep track for logging

        while offset < len(self.image_data):
            start_offset_for_track = offset # Remember start for error messages
            if offset + 5 > len(self.image_data):
                 if all(b == 0 for b in self.image_data[offset:]):
                     self.logger.debug(f"Reached end of data with {len(self.image_data) - offset} trailing null bytes.")
                     break
                 else:
                    raise IMDFormatException(f"Incomplete track header data at EOF, offset {offset}")

            mode, cyl, head_flags, num_sectors, sector_size_code = struct.unpack_from("<BBBBB", self.image_data, offset)
            offset += 5

            if num_sectors == 0: # Explicitly check for zero sectors
                 self.logger.warning(f"Track C:{cyl} H:{head_flags & 1} has 0 sectors at offset {start_offset_for_track}. Treating as end or ignoring.")
                 # Decide whether to break or just skip this 0-sector track entry if possible. Breaking is safer.
                 break

            try:
                track_info = IMDTrackInfo(mode, cyl, head_flags, num_sectors, sector_size_code)
            except IMDFormatException as e:
                 raise IMDFormatException(f"Error processing track header C:{cyl} H:{head_flags & 1} at offset {start_offset_for_track}: {e}") from e

            max_cyl = max(max_cyl, cyl)
            max_head = max(max_head, track_info.head)

            # --- Read Sector Numbering Map ---
            map_len = num_sectors
            if offset + map_len > len(self.image_data):
                raise IMDFormatException(f"EOF while reading Sector Number Map for C:{cyl} H:{track_info.head}")
            track_info.sector_num_map = list(struct.unpack_from(f"<{map_len}B", self.image_data, offset))
            offset += map_len

            # --- Read Optional Maps ---
            if track_info.has_cyl_map:
                map_len = num_sectors
                if offset + map_len > len(self.image_data): raise IMDFormatException(f"EOF Cyl Map C:{cyl} H:{track_info.head}")
                cyl_map_list = list(struct.unpack_from(f"<{map_len}B", self.image_data, offset))
                track_info.sector_cyl_map = {num: log_cyl for num, log_cyl in zip(track_info.sector_num_map, cyl_map_list)}
                offset += map_len

            if track_info.has_head_map:
                map_len = num_sectors
                if offset + map_len > len(self.image_data): raise IMDFormatException(f"EOF Head Map C:{cyl} H:{track_info.head}")
                head_map_list = list(struct.unpack_from(f"<{map_len}B", self.image_data, offset))
                track_info.sector_head_map = {num: log_head & 1 for num, log_head in zip(track_info.sector_num_map, head_map_list)}
                offset += map_len

            if track_info.sector_size_code == 0xFF:
                 map_len = num_sectors * 2
                 if offset + map_len > len(self.image_data): raise IMDFormatException(f"EOF Size Map C:{cyl} H:{track_info.head}")
                 size_map_list = list(struct.unpack_from(f"<{map_len}H", self.image_data, offset))
                 track_info.sector_size_map = {num: size for num, size in zip(track_info.sector_num_map, size_map_list)}
                 offset += map_len

            # --- Record Sector Data Offsets and Types ---
            for sector_num in track_info.sector_num_map:
                if offset >= len(self.image_data):
                    raise IMDFormatException(f"EOF before reading sector data type for C:{cyl} H:{track_info.head} S:{sector_num}")

                sector_data_type = self.image_data[offset]
                data_start_offset_in_image = offset + 1 # Offset where data/fill byte starts

                try:
                     # This can fail if sector_size_map is needed but missing
                     sector_size_logical = track_info.get_sector_size(sector_num)
                except (IMDFormatException, ValueError) as e:
                     raise IMDFormatException(f"Cannot determine sector size for C:{cyl} H:{track_info.head} S:{sector_num}: {e}") from e


                data_size_on_disk = 0 # The number of bytes following the type byte
                if sector_data_type == IMD_SECTOR_UNAVAILABLE:
                    data_size_on_disk = 0
                elif sector_data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL, IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
                    data_size_on_disk = 1
                elif sector_data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL, IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
                    data_size_on_disk = sector_size_logical
                else:
                    raise IMDFormatException(f"Unknown sector data type {sector_data_type} for C:{cyl} H:{track_info.head} S:{sector_num}")

                # Check if the expected data fits within the image bounds
                required_end_offset = offset + 1 + data_size_on_disk
                if required_end_offset > len(self.image_data):
                     # Allow reaching exactly EOF only on the very last sector of the last track
                     is_last_sector = (sector_num == track_info.sector_num_map[-1])
                     # This check for last track is complex, maybe just use required_end_offset check
                     if not (required_end_offset == len(self.image_data) and is_last_sector):
                         raise IMDFormatException(f"EOF while reading sector data for C:{cyl} H:{track_info.head} S:{sector_num} (offset={offset}, type={sector_data_type}, dsize_on_disk={data_size_on_disk}, required_end={required_end_offset}, total={len(self.image_data)})")

                # Store info: offset *to the data itself*, type, size *on disk*
                track_info.sector_data_info[sector_num] = (data_start_offset_in_image, sector_data_type, data_size_on_disk)

                # Advance offset past the type byte and the data/fill byte(s)
                offset += (1 + data_size_on_disk)

            # Store track info
            self.tracks[(cyl, track_info.head)] = track_info
            parsed_track_count += 1

        self.logger.info(f"Successfully parsed {parsed_track_count} tracks. Max Cyl={max_cyl}, Max Head={max_head}")

        # Derive PhysicalFormat only if tracks were parsed
        if parsed_track_count > 0 and max_cyl != -1 and max_head != -1:
            self._derive_physical_format(max_cyl, max_head)
        else:
            self.logger.warning("No tracks parsed or found, cannot derive physical format.")
            self.physical_format = None # Ensure it's None

    def _derive_physical_format(self, max_cyl_idx: int, max_head_idx: int):
        """Creates a PhysicalFormat based on parsed IMD data."""
        if not self.tracks or max_cyl_idx < 0 or max_head_idx < 0:
            self.logger.warning("No tracks found or invalid max indices, cannot derive format.")
            self.physical_format = None
            return

        cylinders = max_cyl_idx + 1
        heads = max_head_idx + 1

        ref_track_info = self.tracks.get((0, 0))
        if not ref_track_info:
            try:
                first_track_key = min(self.tracks.keys())
                ref_track_info = self.tracks[first_track_key]
                self.logger.debug(f"Using track {first_track_key} as reference for format derivation.")
            except (ValueError, KeyError): # Catch if tracks empty or key invalid
                 self.logger.error("Cannot derive format: Track dictionary is inconsistent or empty.")
                 self.physical_format = None
                 return
        else:
            self.logger.debug("Using track (0,0) as reference for format derivation.")

        try:
            ref_mode = ref_track_info.mode
            ref_spt = ref_track_info.num_sectors
            # Use get_sector_size which handles variable map lookup if needed
            # Use first sector in map as reference if variable
            base_sector_num_ref = ref_track_info.sector_num_map[0] if ref_track_info.sector_num_map else 1
            ref_sector_size = ref_track_info.get_sector_size(base_sector_num_ref)
        except (AttributeError, IndexError, ValueError, IMDFormatException) as e:
             self.logger.error(f"Error accessing reference track info: {e}. Cannot derive format.")
             self.physical_format = None
             return


        rate_kbps, encoding, _ = IMD_MODE_MAP.get(ref_mode, (0, "Unknown", ""))
        if rate_kbps <= 0:
             self.logger.warning(f"Derived invalid rate ({rate_kbps}) from mode {ref_mode}. Using default 500.")
             rate_kbps = 500
        if not encoding or encoding == "Unknown":
             self.logger.warning(f"Derived unknown encoding from mode {ref_mode}. Using default MFM.")
             encoding = "MFM"
        if ref_spt <= 0:
             self.logger.error(f"Derived invalid sectors per track ({ref_spt}). Cannot create TrackFormat.")
             self.physical_format = None
             return
        if ref_sector_size <= 0:
            self.logger.error(f"Derived invalid sector size ({ref_sector_size}). Cannot create PhysicalFormat.")
            self.physical_format = None
            return


        # Assume a default RPM based on rate/density - this is a guess
        rpm = 300
        if rate_kbps == 500 and encoding == "MFM":
             rpm = 360 if ref_spt == 15 else 300
        elif rate_kbps == 250 and encoding == "MFM":
             rpm = 300
        elif encoding == "FM":
             rpm = 360

        try:
            track_format = TrackFormat(
                track_start=0,
                track_end=cylinders - 1,
                head_start=0,
                head_end=heads - 1,
                sectors_per_track=ref_spt,
                encoding=encoding,
                rate=rate_kbps,
                gap3=84,
                interleave=1
            )

            self.physical_format = PhysicalFormat(
                cylinders=cylinders,
                heads=heads,
                rpm=rpm,
                heads_inverted=False,
                bytes_per_sector=ref_sector_size,
                track_formats=[track_format]
            )
            self.logger.info(f"Derived Physical Format: Cyl={cylinders}, Heads={heads}, SPT={ref_spt}, BPS={ref_sector_size}, Rate={rate_kbps}, Enc={encoding}, RPM={rpm} (guess)")
        except Exception as e:
             self.logger.error(f"Error creating PhysicalFormat/TrackFormat objects: {e}")
             self.physical_format = None

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Reads a single sector's data, checking the modification cache first."""
        # Don't check file_loaded here, allow reads even if init was for format
        # if not self.file_loaded:
        #     raise IOError("IMD file not loaded or parsed.")
        if not self.physical_format:
            # If format couldn't be derived, we can't know the expected size
            raise ValueError("Physical format not derived/available, cannot determine sector size to read")

        sector_key = (cylinder, head, sector)

        # Check modification cache first
        if sector_key in self.modified_sector_data:
            return self.modified_sector_data[sector_key]

        # Not in cache, read from original image_data structure
        track_key = (cylinder, head)
        track_info = self.tracks.get(track_key)

        # Determine expected size (needed even if track/sector missing)
        expected_size = self.physical_format.bytes_per_sector # Default
        if track_info:
             try:
                  # Try to get specific size if track exists
                  expected_size = track_info.get_sector_size(sector)
             except (ValueError, IMDFormatException): # Sector not in map or size error
                  try:
                       # Fallback to size of first sector on that track
                       if track_info.sector_num_map:
                            expected_size = track_info.get_sector_size(track_info.sector_num_map[0])
                       # Else: keep default physical_format.bytes_per_sector
                  except (IndexError, ValueError, IMDFormatException): pass # Keep default

        if not track_info:
            self.logger.warning(f"Track C:{cylinder} H:{head} not found in IMD.")
            return bytes(expected_size) # Return empty bytes of expected size

        if sector not in track_info.sector_data_info:
             self.logger.warning(f"Sector S:{sector} not found in map for C:{cylinder} H:{head}. Available: {track_info.sector_num_map}")
             return bytes(expected_size) # Return empty bytes of expected size

        data_offset, data_type, data_size_on_disk = track_info.sector_data_info[sector]
        # target_sector_size = expected_size # Already determined above

        if data_type == IMD_SECTOR_UNAVAILABLE:
            return bytes(expected_size)

        elif data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL, IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
            if data_size_on_disk != 1:
                 self.logger.error(f"Mismatch size for compressed sector C:{cylinder} H:{head} S:{sector}. Expected 1 byte, record size is {data_size_on_disk}")
                 return bytes(expected_size) # Return empty on error
            # Check bounds for reading the fill byte
            if data_offset >= len(self.image_data):
                self.logger.error(f"Offset {data_offset} out of bounds for reading compressed fill byte C:{cylinder} H:{head} S:{sector}")
                return bytes(expected_size)
            fill_byte = self.image_data[data_offset]
            return bytes([fill_byte] * expected_size)

        elif data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL, IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
             # Check bounds for reading normal data
             if data_offset + data_size_on_disk > len(self.image_data):
                 self.logger.error(f"Offset {data_offset} + size {data_size_on_disk} out of bounds for reading normal data C:{cylinder} H:{head} S:{sector}")
                 # Return partial data if possible? Or just empty? Empty is safer.
                 return bytes(expected_size)

             sector_bytes = self.image_data[data_offset : data_offset + data_size_on_disk]

             # Handle size mismatches (data_size_on_disk might not match expected_size if IMD is inconsistent)
             if len(sector_bytes) != expected_size:
                  self.logger.warning(f"Size mismatch for normal sector C:{cylinder} H:{head} S:{sector}. Expected {expected_size}, got {len(sector_bytes)}. Padding/truncating.")
                  if len(sector_bytes) < expected_size:
                       return bytes(sector_bytes) + bytes(expected_size - len(sector_bytes))
                  else:
                       return bytes(sector_bytes[:expected_size])
             else:
                 return bytes(sector_bytes)
        else:
            self.logger.error(f"Unexpected sector data type {data_type} encountered during read for C:{cylinder} H:{head} S:{sector}")
            return bytes(expected_size)

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """Stores modified sector data in a cache and marks the image as dirty."""
        # Allow writing even if file wasn't loaded (i.e., after format_imd)
        # if not self.file_loaded:
        #     raise IOError("IMD file not loaded or parsed.")
        if not self.physical_format:
            raise ValueError("Physical format not set/derived, cannot write sector")

        track_key = (cylinder, head)
        track_info = self.tracks.get(track_key)

        # If formatting, tracks might not exist yet. We rely on flush to build them.
        # If modifying existing, check track/sector existence.
        if self.file_loaded: # Only check if modifying existing loaded file
            if not track_info:
                raise IOError(f"Track C:{cylinder} H:{head} not found in loaded IMD structure, cannot write sector.")
            if sector not in track_info.sector_data_info:
                raise IOError(f"Sector S:{sector} not found in map for C:{cylinder} H:{head}, cannot write.")

        # Determine target size - relies on physical_format being set
        target_sector_size = self.physical_format.bytes_per_sector
        if track_info: # If track exists, try to get specific size
            try:
                target_sector_size = track_info.get_sector_size(sector)
            except (ValueError, IMDFormatException):
                 # Sector might not be in map yet if only formatting
                 # Use default BPS from physical_format
                 pass

        if len(data) != target_sector_size:
            raise ValueError(f"Data length ({len(data)}) does not match expected sector size ({target_sector_size}) for C:{cylinder} H:{head} S:{sector}")

        # Store the raw data in the cache
        sector_key = (cylinder, head, sector)
        self.modified_sector_data[sector_key] = bytes(data) # Store a copy
        self.dirty = True

    # --- Full flush Method ---
    def flush(self) -> None:
        """Writes the modified image data back to the file, rebuilding if necessary."""
        # Allow flush even if file wasn't loaded (case after format_imd)
        # if not self.file_loaded:
        #     self.logger.warning("Cannot flush, IMD file was not loaded correctly.")
        #     return

        if not self.dirty:
            self.logger.debug("No changes to flush.")
            return
        # Need physical format to rebuild
        if not self.physical_format:
            self.logger.error("Cannot flush IMD: Physical format not set.")
            # Don't clear dirty flag if flush fails pre-emptively
            return

        self.logger.info(f"Rebuilding and flushing modified IMD data to {self.file_path}")

        new_image_data = bytearray()
        current_offset_in_new_data = 0
        updated_sector_infos = {}

        # 1. Build Header
        header_str = f"{self.imd_version}: {self.comment}"
        header_bytes = header_str.encode('ascii', errors='ignore')
        new_image_data.extend(header_bytes)
        new_image_data.append(0x1A) # Header terminator
        current_offset_in_new_data = len(new_image_data)

        # 2. Build Tracks (Iterate based on PHYSICAL FORMAT, not self.tracks)
        # This ensures tracks created by format_imd but not yet 'parsed' are included.
        new_tracks_metadata: Dict[Tuple[int, int], IMDTrackInfo] = {} # Store newly built metadata

        for cylinder in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_key = (cylinder, head)
                # Get existing track info if available (for maps etc.), otherwise create new
                existing_track_info = self.tracks.get(track_key)

                try:
                    track_format = self.physical_format.get_track_format(cylinder, head)
                    bps = self.physical_format.bytes_per_sector
                    spt = track_format.sectors_per_track
                    rate = track_format.rate
                    encoding = track_format.encoding
                except ValueError as e:
                    self.logger.error(f"Skipping flush for track C:{cylinder} H:{head}: Cannot get track format - {e}")
                    continue # Skip this track

                # Determine IMD Mode
                mode = -1
                for m, (r, enc, _) in IMD_MODE_MAP.items():
                    if r == rate and enc == encoding: mode = m; break
                if mode == -1:
                    self.logger.error(f"Skipping flush for track C:{cylinder} H:{head}: Unsupported rate/encoding {rate}/{encoding}")
                    continue

                # Determine Sector Size Code
                size_code = -1
                is_variable_size = False # TODO: Support variable size map creation if needed
                if not is_variable_size:
                    for code, size in IMD_SECTOR_SIZE_MAP.items():
                        if size == bps: size_code = code; break
                else:
                    size_code = 0xFF
                if size_code == -1 and not is_variable_size:
                     self.logger.error(f"Skipping flush for track C:{cylinder} H:{head}: Unsupported BPS {bps}")
                     continue

                # Use existing maps if available, otherwise default
                # This assumes standard sector numbering/mapping for newly formatted tracks
                sector_num_map = list(range(1, spt + 1))
                has_cyl_map = False
                has_head_map = False
                if existing_track_info:
                     sector_num_map = existing_track_info.sector_num_map
                     # Retain map flags if they existed
                     has_cyl_map = existing_track_info.has_cyl_map
                     has_head_map = existing_track_info.has_head_map
                     # Ensure map length matches current SPT
                     if len(sector_num_map) != spt:
                          self.logger.warning(f"Sector number map length mismatch for C:{cylinder} H:{head}. Using default map.")
                          sector_num_map = list(range(1, spt + 1))
                          has_cyl_map = False # Reset flags if map is rebuilt
                          has_head_map = False


                head_flags = head & 1
                if has_cyl_map: head_flags |= 0x80
                if has_head_map: head_flags |= 0x40

                # Create metadata object for this rebuilt track
                rebuilt_track_info = IMDTrackInfo(mode, cylinder, head_flags, spt, size_code)
                rebuilt_track_info.sector_num_map = sector_num_map
                # Populate optional maps if needed (using existing info or defaults)
                if has_cyl_map:
                    rebuilt_track_info.sector_cyl_map = existing_track_info.sector_cyl_map if existing_track_info else {s: cylinder for s in sector_num_map}
                if has_head_map:
                    rebuilt_track_info.sector_head_map = existing_track_info.sector_head_map if existing_track_info else {s: head for s in sector_num_map}
                # TODO: Handle variable size map creation if needed


                # Append track header
                track_header = struct.pack("<BBBBB", mode, cylinder, head_flags, spt, size_code)
                new_image_data.extend(track_header)
                current_offset_in_new_data += len(track_header)

                # Append Sector Number Map
                map_bytes = struct.pack(f"<{spt}B", *sector_num_map)
                new_image_data.extend(map_bytes)
                current_offset_in_new_data += len(map_bytes)

                # Append Optional Maps (using rebuilt_track_info state)
                if rebuilt_track_info.has_cyl_map:
                    cyl_map_list = [rebuilt_track_info.sector_cyl_map.get(s_num, cylinder) for s_num in sector_num_map]
                    map_bytes = struct.pack(f"<{spt}B", *cyl_map_list)
                    new_image_data.extend(map_bytes)
                    current_offset_in_new_data += len(map_bytes)
                if rebuilt_track_info.has_head_map:
                    head_map_list = [rebuilt_track_info.sector_head_map.get(s_num, head) for s_num in sector_num_map]
                    map_bytes = struct.pack(f"<{spt}B", *head_map_list)
                    new_image_data.extend(map_bytes)
                    current_offset_in_new_data += len(map_bytes)
                # TODO: Append Variable Size Map if needed

                for sector_num in sector_num_map: # Use the determined sector_num_map
                    sector_key = (cylinder, head, sector_num)
                    if sector_key in self.modified_sector_data:
                        current_sector_data = self.modified_sector_data[sector_key]
                    else:
                        if self.file_loaded: # Read from original image data if loaded
                             current_sector_data = self._read_original_sector(cylinder, head, sector_num)
                        else: # Recreating formatted sector
                             # Use the fill byte stored during format_imd
                             fill = self.last_format_fill_byte if self.last_format_fill_byte is not None else 0xE5 # Fallback just in case
                             if self.last_format_fill_byte is None:
                                 self.logger.warning(f"Flush: last_format_fill_byte is None for formatted sector {sector_key}, using default 0xE5")
                             # --- FIX: Use stored fill byte ---
                             current_sector_data = bytes([fill] * bps)

                    # Determine compression, type, content
                    new_data_type: int
                    new_data_content: bytes
                    data_size_on_disk: int
                    target_sector_size = bps # From loop context

                    if not current_sector_data or len(current_sector_data) != target_sector_size:
                         # Handle empty/incorrect size data (default to compressed fill)
                         self.logger.warning(f"Data for C:{cylinder} H:{head} S:{sector_num} missing or wrong size ({len(current_sector_data)} vs {target_sector_size}) during flush. Using compressed fill 0xE5.")
                         new_data_type = IMD_SECTOR_COMPRESSED
                         new_data_content = bytes([0xE5])
                         data_size_on_disk = 1
                    else:
                         fill_byte = current_sector_data[0]
                         is_compressible = all(b == fill_byte for b in current_sector_data)
                         if is_compressible:
                             new_data_type = IMD_SECTOR_COMPRESSED
                             new_data_content = bytes([fill_byte])
                             data_size_on_disk = 1
                         else:
                             new_data_type = IMD_SECTOR_NORMAL
                             new_data_content = current_sector_data
                             data_size_on_disk = target_sector_size

                    # Append type byte and data/fill byte
                    new_image_data.append(new_data_type)
                    new_image_data.extend(new_data_content)

                    # Store the NEW offset, type, and data size for metadata update
                    new_data_start_offset = current_offset_in_new_data + 1
                    # Use rebuilt_track_info to store the *new* metadata
                    rebuilt_track_info.sector_data_info[sector_num] = (new_data_start_offset, new_data_type, data_size_on_disk)

                    # Update offset for the next sector record
                    current_offset_in_new_data += (1 + data_size_on_disk)

                # Store the fully populated metadata for this track
                new_tracks_metadata[track_key] = rebuilt_track_info

        # 3. Write the rebuilt data to file
        try:
            with open(self.file_path, "wb") as f:
                f.write(new_image_data)

            # 4. Update internal state ONLY after successful write
            self.image_data = new_image_data # Replace old data with rebuilt data
            self.tracks = new_tracks_metadata # Replace old track metadata
            self.modified_sector_data.clear() # Clear the modification cache
            self.dirty = False
            self.file_loaded = True # Mark as effectively loaded now
            self.logger.info("IMD flush completed successfully.")

        except Exception as e:
            self.logger.exception(f"Error writing rebuilt IMD file {self.file_path}: {e}")
            raise IOError(f"Failed to flush IMD data: {e}") from e

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical format. For IMD, this is usually derived.
        Calling this externally might lead to inconsistencies.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")
        # Use logger, not warnings.warn
        self.logger.warning("Setting physical format externally on IMDImageDriver. "
                            "This may conflict with the format defined within the IMD file.")
        self.physical_format = copy.deepcopy(physical_format)

    def read_boot_sector_data(self) -> Optional[bytes]:
        """Convenience method to read the typical boot sector (C:0, H:0, S:1)."""
        # Allow reading even if only formatted, provided physical_format is set
        if not self.physical_format:
             self.logger.warning("Cannot read boot sector data: physical format not set.")
             return None
        try:
            return self.read_sector(0, 0, 1)
        except (IOError, ValueError, KeyError) as e: # Include KeyError just in case map access fails
            self.logger.warning(f"Could not read boot sector (0,0,1) from IMD: {e}")
            return None

    # --- Methods required for formatting ---
    # These methods will construct an IMD structure in memory based on a profile.

    def _initialize_for_format(self, profile: FormatProfile):
        """Resets internal state and prepares for creating a new IMD structure."""
        self.logger.info(f"Initializing IMD driver for formatting with profile: {profile.name}")
        if not profile.physical_format: # Boot sector not strictly needed for IMD format itself
             raise ValueError("FormatProfile must include physical_format for IMD formatting.")

        self.physical_format = copy.deepcopy(profile.physical_format)
        self.tracks = {} # Clear existing track metadata
        self.image_data = bytearray() # Clear existing image data
        self.modified_sector_data = {} # Clear modifications
        self.dirty = True # Mark as dirty (needs flush to create file)
        self.file_loaded = False # Not loaded from file
        self.last_format_fill_byte = None

    def _build_imd_header(self):
        """Constructs the IMD header and comment."""
        # Use current state values
        header_str = f"{self.imd_version}: {self.comment}"
        header_bytes = header_str.encode('ascii', errors='ignore')
        self.image_data.extend(header_bytes)
        self.image_data.append(0x1A) # Header terminator

    def _build_imd_track(self, cylinder: int, head: int, fill_byte: int = 0xE5):
        """Constructs a single track record and appends to self.image_data."""
        # (Keep implementation as before - it correctly appends bytes)
        if not self.physical_format:
             raise ValueError("Physical format not set for building track.")

        try:
             track_format = self.physical_format.get_track_format(cylinder, head)
             bps = self.physical_format.bytes_per_sector
             spt = track_format.sectors_per_track
             rate = track_format.rate
             encoding = track_format.encoding
        except ValueError as e:
             self.logger.error(f"Cannot get track format for C:{cylinder} H:{head}: {e}")
             return

        mode = -1
        for m, (r, enc, _) in IMD_MODE_MAP.items():
            if r == rate and enc == encoding: mode = m; break
        if mode == -1:
            self.logger.error(f"Unsupported rate/encoding for IMD: Rate={rate}, Enc={encoding} on C:{cylinder} H:{head}")
            return

        size_code = -1
        for code, size in IMD_SECTOR_SIZE_MAP.items():
            if size == bps: size_code = code; break
        if size_code == -1:
             self.logger.error(f"Unsupported bytes_per_sector for IMD: {bps} on C:{cylinder} H:{head}.")
             return

        head_flags = head & 1
        sector_num_map = list(range(1, spt + 1))

        track_header = struct.pack("<BBBBB", mode, cylinder, head_flags, spt, size_code)
        self.image_data.extend(track_header)
        self.image_data.extend(struct.pack(f"<{spt}B", *sector_num_map))

        sector_data_type = IMD_SECTOR_COMPRESSED
        for i in range(spt):
             sector_num = i + 1 # For logging clarity
             self.image_data.append(sector_data_type)
             self.image_data.append(fill_byte)
             # +++ Add Logging +++
             if cylinder == 0 and head == 0 and sector_num <= 5: # Log first few sectors of first track
                 current_len = len(self.image_data)
                 logger.debug(f"_build_track C:{cylinder} H:{head} S:{sector_num}: Appended type {sector_data_type}, fill {hex(fill_byte)}. Offset now: {current_len}")

    def _read_original_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Helper to read sector data from the original image_data, bypassing the cache."""
        # (Keep implementation as before - it reads based on self.tracks and self.image_data)
        track_key = (cylinder, head)
        track_info = self.tracks.get(track_key)

        expected_size = self.physical_format.bytes_per_sector if self.physical_format else 512
        if track_info:
            try: expected_size = track_info.get_sector_size(sector)
            except:
                try:
                     if track_info.sector_num_map: expected_size = track_info.get_sector_size(track_info.sector_num_map[0])
                except: pass

        if not track_info: return bytes(expected_size)
        if sector not in track_info.sector_data_info: return bytes(expected_size)

        data_offset, data_type, data_size_on_disk = track_info.sector_data_info[sector]
        target_sector_size = expected_size # Use determined size

        if data_offset >= len(self.image_data): # Bounds check offset
            self.logger.error(f"_read_original: Offset {data_offset} out of bounds (size {len(self.image_data)})")
            return bytes(target_sector_size)

        if data_type == IMD_SECTOR_UNAVAILABLE:
            return bytes(target_sector_size)
        elif data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL, IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
            if data_size_on_disk != 1: return bytes(target_sector_size)
            fill_byte = self.image_data[data_offset]
            return bytes([fill_byte] * target_sector_size)
        elif data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL, IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
            end_offset = data_offset + data_size_on_disk
            if end_offset > len(self.image_data): # Bounds check end
                self.logger.error(f"_read_original: Read end offset {end_offset} out of bounds (size {len(self.image_data)})")
                # Return what we can, padded
                actual_data = self.image_data[data_offset:]
                return bytes(actual_data).ljust(target_sector_size, b'\0')

            sector_bytes = self.image_data[data_offset : end_offset]
            if len(sector_bytes) != target_sector_size: # Final size check
                if len(sector_bytes) < target_sector_size:
                    return bytes(sector_bytes).ljust(target_sector_size, b'\0')
                else:
                    return bytes(sector_bytes[:target_sector_size])
            else:
                return bytes(sector_bytes)
        else:
            return bytes(target_sector_size)


    def format_imd(self, profile: FormatProfile, fill_byte: int = 0xE5):
        self._initialize_for_format(profile)
        self._build_imd_header()
        # --- Store the fill byte used ---
        self.last_format_fill_byte = fill_byte
        # --- End Store ---

        if not self.physical_format:
            raise IMDFormatException("Physical format missing after initialization in format_imd")

        self.logger.debug(f"Starting format_imd loop with fill_byte=0x{fill_byte:02X}")
        for c in range(self.physical_format.cylinders):
             for h in range(self.physical_format.heads):
                  self._build_imd_track(c, h, fill_byte)

        self.logger.info(f"IMD structure created in memory ready for flush. Size: {len(self.image_data)} bytes.")
