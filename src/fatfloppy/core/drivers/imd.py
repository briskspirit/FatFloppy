# src/fatfloppy/core/drivers/imd.py
"""
IMD (ImageDisk) file format driver.

This module provides a DiskIODriver implementation for reading, writing,
and creating disk images in the IMD format. It supports various sector
layouts, densities (FM/MFM), and compressed sector data as defined by the
IMD specification.

The driver can load an existing IMD file, deriving the disk's physical
format from its contents, or it can create a new IMD file from a specified
FormatProfile.
"""

import copy
import datetime
import os
import re
import struct
from typing import Dict, List, Optional, Tuple, Any, ClassVar

from ..._version import __version__ as fatfloppy_version
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

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
IMD_MODE_MAP = {
    0: (500, "FM", "SD"),
    1: (300, "FM", "SD"),
    2: (250, "FM", "SD"),
    3: (500, "MFM", "HD/ED"),
    4: (300, "MFM", "HD"),
    5: (250, "MFM", "DD"),
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


class IMDTrackInfo:
    """
    Represents the metadata and sector information for a single track in an IMD file.
    """

    def __init__(self, mode: int, cylinder: int, head_flags: int,
                 num_sectors: int, sector_size_code: int):
        """
        Initializes an IMDTrackInfo object.

        Args:
            mode: The IMD mode byte for the track.
            cylinder: The physical cylinder number.
            head_flags: The head flags byte.
            num_sectors: The number of sectors in this track.
            sector_size_code: The sector size code.
        """
        self.mode: int = mode
        self.cylinder: int = cylinder
        self.head: int = head_flags & 1
        self.has_cyl_map: bool = bool(head_flags & 0x80)
        self.has_head_map: bool = bool(head_flags & 0x40)
        self.num_sectors: int = num_sectors
        self.sector_size_code: int = sector_size_code

        self.sector_size_map: Optional[Dict[int, int]] = None  # Sector Num -> Size
        self.sector_size: int = self._get_base_sector_size()
        self.sector_num_map: List[int] = []
        self.sector_cyl_map: Optional[Dict[int, int]] = None  # Sector Num -> Logical Cyl
        self.sector_head_map: Optional[Dict[int, int]] = None  # Sector Num -> Logical Head
        self.sector_data_info: Dict[int, Tuple[int, int, int]] = {}  # Sector Num -> (Offset, Type, DataSize)

    def _get_base_sector_size(self) -> int:
        """
        Determines the base sector size from the sector size code.

        Returns:
            The sector size in bytes, or -1 if sizes are variable.

        Raises:
            ValueError: If the sector size code is invalid.
        """
        if self.sector_size_code == 0xFF:
            return -1  # Variable size indicated by map
        size = IMD_SECTOR_SIZE_MAP.get(self.sector_size_code)
        if size is None:
            raise ValueError(f"Invalid sector size code: {self.sector_size_code}")
        return size

    def get_sector_size(self, sector_num: int) -> int:
        """
        Gets the size of a specific sector.

        Args:
            sector_num: The sector number to look up.

        Returns:
            The size of the sector in bytes.

        Raises:
            ValueError: If the sector size cannot be determined.
        """
        if self.sector_size_map:
            size = self.sector_size_map.get(sector_num)
            if size is None:
                raise ValueError(
                    f"Sector {sector_num} not found in variable size map for "
                    f"C:{self.cylinder} H:{self.head}"
                )
            return size
        elif self.sector_size != -1:
            return self.sector_size
        else:
            raise ValueError(
                f"Inconsistent sector size information for "
                f"C:{self.cylinder} H:{self.head}"
            )


class IMDImageDriver(DiskIODriver):
    """
    Disk I/O driver for the ImageDisk (IMD) file format.

    This class handles reading from and writing to IMD disk image files. It can
    parse existing files to determine the disk geometry or format a new image
    based on a provided physical format profile. Sector data is cached in memory
    and flushed to the file on demand.
    """
    # Plugin metadata
    driver_type: ClassVar[str] = "IMD"
    driver_file_extensions: ClassVar[List[str]] = [".imd"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "ImageDisk format driver"

    def __init__(self, file_path: str):
        """
        Initializes the IMDImageDriver.

        If the file exists, it is loaded and parsed. Otherwise, an empty
        driver is initialized, ready for formatting.

        Args:
            file_path: The path to the IMD image file.
        """
        super().__init__()
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.comment: str = ""
        self.imd_version: str = ""
        self.creation_date: Optional[datetime.datetime] = None
        self.tracks: Dict[Tuple[int, int], IMDTrackInfo] = {}
        self.image_data: bytearray = bytearray()
        self.dirty: bool = False
        self.file_loaded: bool = False
        self.modified_sector_data: Dict[Tuple[int, int, int], bytes] = {}
        self.last_format_fill_byte: Optional[int] = None
        self.uses_physical_heads: bool = False
        self._sector_size_cache: Dict[Tuple[int, int, int], int] = {}

        if os.path.exists(self.file_path):
            try:
                self._load_and_parse_imd_file()
                self.file_loaded = True
                # Pre-populate the sector size cache
                for (cyl, head), track_info in self.tracks.items():
                    for sector in track_info.sector_num_map:
                        self._sector_size_cache[(cyl, head, sector)] = track_info.get_sector_size(sector)
            except FileNotFoundError:
                logger.error(f"IMD file not found: {self.file_path}")
                raise
            except ValueError as e:
                logger.error(f"Parse error in IMD file {self.file_path}: {e}")
                raise
            except Exception as e:
                logger.exception(f"Unexpected error loading IMD file {self.file_path}: {e}")
                raise
        else:
            logger.info(f"IMD file '{self.file_path}' not found. Initializing empty driver.")
            self.creation_date = datetime.datetime.now()
            self.imd_version = "IMD 1.18"
            self.comment = f"{self.creation_date.strftime('%d/%m/%Y %H:%M:%S')}\r\nFatFloppy v{fatfloppy_version}"

    # --- Properties ---

    @property
    def has_embedded_geometry(self) -> bool:
        """IMD files contain complete geometry information."""
        return True

    @property
    def allows_geometry_override(self) -> bool:
        """
        IMD files can have geometry overridden, but this may cause inconsistencies.
        """
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """
        IMD files cannot be formatted in place as their structure is track-based.
        """
        return False

    @property
    def supports_new_image_creation(self) -> bool:
        """IMD driver can create new formatted images."""
        return True

    def initialize_new_image(self, physical_format: PhysicalFormat,
                            profile: Optional[Any] = None) -> None:
        """
        Initializes a new blank IMD image structure.

        Args:
            physical_format: The physical format for the new image.
            profile: Optional FormatProfile with additional metadata.
        """
        self.format_imd(profile if profile else
                       FormatProfile("temp", "Temp", physical_format, "Unknown", None),
                       fill_byte=0xE5)

    def validate_state_for_opening(self) -> Tuple[bool, Optional[str]]:
        """
        Validates IMD driver state after opening.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not self.file_path:
            return False, "IMD driver has no file path"

        # If file was loaded, we should have physical format
        if self.file_loaded and not self.physical_format:
            return False, "IMD file loaded but no physical format derived"

        return True, None

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical format of the disk.

        This will override the format derived from the IMD file's metadata,
        so a warning is issued.

        Args:
            physical_format: The PhysicalFormat object to apply.

        Raises:
            TypeError: If the provided object is not a PhysicalFormat.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")

        # The driver is now responsible for issuing its own warning.
        if self.file_loaded:
            self.logger.warning(
                "Applying an external physical format to an already loaded IMD file. "
                "This will override the geometry derived from the file's metadata."
            )

        self.physical_format = copy.deepcopy(physical_format)

    def validate_for_opening(self, source: str, **kwargs) -> Tuple[bool, Optional[str]]:
        """
        Validates whether an IMD file can be opened.

        Args:
            source: Path to the IMD file.
            **kwargs: Unused for IMD driver.

        Returns:
            Tuple of (is_valid, error_message).
        """
        import os

        if not os.path.exists(source):
            return False, f"IMD file not found: {source}"

        # Quick validation: check for IMD header
        try:
            with open(source, "rb") as f:
                header = f.read(128)

            if b'\x1A' not in header:
                return False, "Missing IMD header terminator (0x1A)"

            # Check for IMD version string
            header_text = header[:header.find(b'\x1A')].decode('ascii', errors='ignore')
            if 'IMD' not in header_text.upper():
                return False, "Does not appear to be a valid IMD file"

        except Exception as e:
            return False, f"Cannot validate IMD file: {e}"

        return True, None

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for IMD driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            'needs_format_for_open': False,  # Can open without format
            'needs_format_for_io': False,    # Has embedded format
            'can_derive_format': True,       # Derives from file structure
            'preferred_detection_method': 'embedded'  # Uses embedded metadata
        }

    def prepare_for_format_application(self, format_info: dict) -> Tuple[bool, Optional[str]]:
        """
        Validates format compatibility for IMD driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        if not self.file_loaded:
            # Creating new image, format is required
            if 'physical_format' not in format_info:
                return False, "Physical format required for creating new IMD image"
            return True, None

        # Applying format to existing IMD file - warn about override
        warning = ("Applying external format to IMD file will override "
                "the format derived from the file structure. This may "
                "cause read errors if the formats don't match.")
        logger.warning(warning)

        return True, warning

    # --- Public Methods ---

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the disk image.

        Checks for a modified version of the sector in the cache first,
        otherwise reads the original data from the loaded IMD file.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            The sector data as a bytes object.

        Raises:
            ValueError: If the physical format is not set.
        """
        if not self.physical_format:
            raise ValueError("No physical format available")

        sector_key = (cylinder, head, sector)
        if sector_key in self.modified_sector_data:
            logger.debug(f"Reading modified sector C:{cylinder} H:{head} S:{sector}")
            return self.modified_sector_data[sector_key]

        return self._read_original_sector(cylinder, head, sector)

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the in-memory cache.

        The data is marked as "dirty" and will be written to the file
        when flush() is called.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.
            data: The sector data to write.

        Raises:
            ValueError: If physical format is not set or data size is incorrect.
            IOError: If the target sector is invalid for the loaded file.
        """
        if not self.physical_format:
            raise ValueError("No physical format set")

        track_info = self.tracks.get((cylinder, head))
        target_size = self._get_sector_size(track_info, cylinder, head, sector)

        if len(data) != target_size:
            raise ValueError(f"Data size mismatch: {len(data)} vs {target_size}")

        if self.file_loaded and (not track_info or sector not in track_info.sector_data_info):
            raise IOError(f"Invalid sector C:{cylinder} H:{head} S:{sector}")

        self.modified_sector_data[(cylinder, head, sector)] = bytes(data)
        self.dirty = True
        logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector}")

    def flush(self) -> None:
        """
        Writes all modified data to the IMD file.

        Reconstructs the IMD file in memory based on the physical format
        and any modified sector data, then writes it to disk.

        Raises:
            IOError: If the file cannot be written.
        """
        if not self.dirty or not self.physical_format:
            logger.debug("Nothing to flush or no format set")
            return

        logger.info(f"Flushing IMD to {self.file_path}")
        new_data = bytearray(f"{self.imd_version}: {self.comment}".encode('ascii', errors='ignore') + b'\x1A')
        new_tracks: Dict[Tuple[int, int], IMDTrackInfo] = {}

        for cyl in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_key = (cyl, head)
                track_info = self.tracks.get(track_key)
                track_format = self.physical_format.get_track_format(cyl, head)

                mode = next((m for m, (r, e, _) in IMD_MODE_MAP.items()
                             if r == track_format.rate and e == track_format.encoding), -1)
                if mode == -1:
                    logger.error(f"Unsupported format for C:{cyl} H:{head}")
                    continue

                bps = self.physical_format.get_bytes_per_sector(cyl, head)
                size_code = track_info.sector_size_code if track_info else next(
                    (c for c, s in IMD_SECTOR_SIZE_MAP.items() if s == bps), -1
                )
                if size_code == -1:
                    logger.error(f"Unsupported sector size for C:{cyl} H:{head}")
                    continue

                spt = track_format.sectors_per_track
                sector_map = track_info.sector_num_map if track_info and len(
                    track_info.sector_num_map) == spt else list(range(1, spt + 1))
                head_flags = (head & 1) | \
                             (0x80 if track_info and track_info.has_cyl_map else 0) | \
                             (0x40 if track_info and track_info.has_head_map else 0)

                rebuilt_info = IMDTrackInfo(mode, cyl, head_flags, spt, size_code)
                rebuilt_info.sector_num_map = sector_map
                if track_info and size_code == 0xFF:
                    rebuilt_info.sector_size_map = track_info.sector_size_map

                new_data.extend(struct.pack("<BBBBB", mode, cyl, head_flags, spt, size_code))
                new_data.extend(struct.pack(f"<{spt}B", *sector_map))

                for sector in sector_map:
                    sector_key = (cyl, head, sector)
                    fill = self.last_format_fill_byte or 0xE5
                    bps = self.physical_format.get_bytes_per_sector(cyl, head)
                    default_data = bytes([fill] * bps)

                    sector_data = self.modified_sector_data.get(sector_key)
                    if sector_data is None:
                        sector_data = self._read_original_sector(cyl, head, sector) if self.file_loaded else default_data

                    target_size = rebuilt_info.get_sector_size(sector)

                    # Determine if sector can be compressed
                    is_compressible = len(sector_data) == target_size and all(b == sector_data[0] for b in sector_data)
                    if is_compressible:
                        data_type = IMD_SECTOR_COMPRESSED
                        content = bytes([sector_data[0] if sector_data else fill])
                    else:
                        data_type = IMD_SECTOR_NORMAL
                        content = sector_data

                    offset = len(new_data) + 1  # Data starts after the type byte
                    rebuilt_info.sector_data_info[sector] = (offset, data_type, len(content))
                    new_data.append(data_type)
                    new_data.extend(content)

                new_tracks[track_key] = rebuilt_info

        try:
            with open(self.file_path, "wb") as f:
                f.write(new_data)
            self.image_data = new_data
            self.tracks = new_tracks
            self.modified_sector_data = {}
            self.dirty = False
            self.file_loaded = True
            logger.info("Flush successful")
        except Exception as e:
            logger.error(f"Flush failed: {e}")
            raise IOError(f"Failed to flush IMD: {e}") from e

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical format of the disk.

        This should be used when creating a new image or overriding the
        format derived from an existing file.

        Args:
            physical_format: The PhysicalFormat object to apply.

        Raises:
            TypeError: If the provided object is not a PhysicalFormat.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")
        logger.warning("External physical format set, may conflict with IMD data")
        self.physical_format = copy.deepcopy(physical_format)

    def read_boot_sector_data(self) -> Optional[bytes]:
        """
        Reads the boot sector (Cylinder 0, Head 0, Sector 1 or first available).

        Returns:
            The boot sector data as bytes, or None if it cannot be read.
        """
        if not self.physical_format or (track_info := self.tracks.get((0, 0))) is None:
            logger.warning("Cannot read boot sector: no format or track")
            return None

        sector_map = track_info.sector_num_map
        sector = 1 if 1 in sector_map else (sector_map[0] if sector_map else None)
        if sector is None:
            logger.warning("No sectors in track C:0 H:0")
            return None

        try:
            return self.read_sector(0, 0, sector)
        except Exception as e:
            logger.warning(f"Failed to read boot sector: {e}")
            return None

    def format_imd(self, profile: FormatProfile, fill_byte: int = 0xE5) -> None:
        """
        Formats the in-memory image according to a format profile.

        This clears any existing image data and builds a new IMD structure
        representing a blank, formatted disk. The image is marked as dirty
        and must be flushed to be written to a file.

        Args:
            profile: The FormatProfile to use for formatting.
            fill_byte: The byte value used to fill sectors.

        Raises:
            ValueError: If the profile does not contain a physical format.
        """
        logger.info(f"Formatting IMD with profile: {profile.name}")
        if not profile.physical_format:
            raise ValueError("Physical format required for formatting")

        self.physical_format = copy.deepcopy(profile.physical_format)
        self.tracks, self.image_data, self.modified_sector_data = {}, bytearray(), {}
        self.dirty = True
        self.file_loaded = False
        self.last_format_fill_byte = fill_byte

        # Build header
        self.image_data.extend(f"{self.imd_version}: {self.comment}".encode('ascii', errors='ignore') + b'\x1A')

        # Build track and sector data
        for cyl in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_format = self.physical_format.get_track_format(cyl, head)
                mode = next((m for m, (r, e, _) in IMD_MODE_MAP.items()
                             if r == track_format.rate and e == track_format.encoding), -1)
                size_code = next((c for c, s in IMD_SECTOR_SIZE_MAP.items()
                                  if s == track_format.bytes_per_sector), -1)

                if mode == -1 or size_code == -1:
                    logger.error(f"Unsupported format for C:{cyl} H:{head}, skipping track.")
                    continue

                spt = track_format.sectors_per_track
                # Track Header
                self.image_data.extend(struct.pack("<BBBBB", mode, cyl, head & 1, spt, size_code))
                # Sector Numbering Map
                self.image_data.extend(struct.pack(f"<{spt}B", *range(1, spt + 1)))
                # Sector Data (all compressed)
                for _ in range(spt):
                    self.image_data.extend([IMD_SECTOR_COMPRESSED, fill_byte])

        logger.info(f"Formatted IMD in memory, size: {len(self.image_data)} bytes")

    # --- Private Methods ---

    def _load_and_parse_imd_file(self) -> None:
        """
        Loads the IMD file from disk and parses its structure.
        Populates track and sector metadata.
        """
        logger.info(f"Loading IMD file: {self.file_path}")
        try:
            with open(self.file_path, "rb") as f:
                self.image_data = bytearray(f.read())
        except Exception as e:
            raise ValueError(f"Cannot read file: {e}") from e

        if not self.image_data:
            raise ValueError("IMD file is empty")

        header_end_pos = self.image_data.find(b'\x1A')
        if header_end_pos == -1:
            raise ValueError("IMD header terminator (0x1A) not found")

        # Parse header
        header_bytes = self.image_data[:header_end_pos]
        first_colon_pos = header_bytes.find(b':')
        if first_colon_pos != -1:
            self.imd_version = header_bytes[:first_colon_pos].decode('ascii', errors='ignore').strip()
            comment_bytes = header_bytes[first_colon_pos + 1:]
            try:
                comment_text = comment_bytes.decode('cp437', errors='replace')
                timestamp_pattern = r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[:]?\s*"
                self.comment = re.sub(timestamp_pattern, '', comment_text, count=1).strip()
            except Exception as e:
                logger.warning(f"Failed to decode comment: {e}")
                self.comment = "[Comment Decode Error]"
        else:
            logger.warning("No colon in header, using fallback parsing")
            self.imd_version = header_bytes.decode('ascii', errors='ignore').strip()
            self.comment = ""

        # Parse track data
        offset = header_end_pos + 1
        max_cyl, max_head, parsed_track_count = -1, -1, 0

        while offset < len(self.image_data):
            if offset + 5 > len(self.image_data):
                if all(b == 0 for b in self.image_data[offset:]):
                    logger.debug("Reached padded end of file")
                    break
                raise ValueError(f"Incomplete track header at offset {offset}")

            mode, cyl, head_flags, num_sectors, sector_size_code = struct.unpack_from(
                "<BBBBB", self.image_data, offset
            )
            offset += 5

            if num_sectors == 0:
                logger.warning(f"Track C:{cyl} H:{head_flags & 1} has zero sectors, skipping to next track header.")
                continue

            track_info = IMDTrackInfo(mode, cyl, head_flags, num_sectors, sector_size_code)
            max_cyl = max(max_cyl, cyl)
            max_head = max(max_head, track_info.head)

            # Sector numbering map
            if offset + num_sectors > len(self.image_data):
                raise ValueError(f"EOF in sector map for C:{cyl} H:{track_info.head}")
            track_info.sector_num_map = list(struct.unpack_from(f"<{num_sectors}B", self.image_data, offset))
            offset += num_sectors

            # Optional cylinder/head maps
            if track_info.has_cyl_map:
                if offset + num_sectors > len(self.image_data):
                    raise ValueError(f"EOF in Cylinder map for C:{cyl} H:{track_info.head}")
                map_data = struct.unpack_from(f"<{num_sectors}B", self.image_data, offset)
                track_info.sector_cyl_map = dict(zip(track_info.sector_num_map, map_data))
                offset += num_sectors

            if track_info.has_head_map:
                if offset + num_sectors > len(self.image_data):
                    raise ValueError(f"EOF in Head map for C:{cyl} H:{track_info.head}")
                map_data = struct.unpack_from(f"<{num_sectors}B", self.image_data, offset)
                track_info.sector_head_map = {num: val & 1 for num, val in zip(track_info.sector_num_map, map_data)}
                offset += num_sectors

            # Optional variable sector size map
            if track_info.sector_size_code == 0xFF:
                size_map_len = num_sectors * 2
                if offset + size_map_len > len(self.image_data):
                    raise ValueError(f"EOF in size map for C:{cyl} H:{track_info.head}")
                sizes = struct.unpack_from(f"<{num_sectors}H", self.image_data, offset)
                track_info.sector_size_map = dict(zip(track_info.sector_num_map, sizes))
                offset += size_map_len

            # Sector data records
            for sector_num in track_info.sector_num_map:
                if offset >= len(self.image_data):
                    raise ValueError(f"EOF before sector type for C:{cyl} H:{track_info.head} S:{sector_num}")
                data_type = self.image_data[offset]
                data_offset = offset + 1
                sector_size = track_info.get_sector_size(sector_num)

                if data_type == IMD_SECTOR_UNAVAILABLE:
                    data_size = 0
                elif data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL,
                                   IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
                    data_size = 1
                elif data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL,
                                   IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
                    data_size = sector_size
                else:
                    raise ValueError(f"Unknown sector type {data_type} for C:{cyl} H:{track_info.head} S:{sector_num}")

                if offset + 1 + data_size > len(self.image_data):
                    raise ValueError(f"EOF in sector data for C:{cyl} H:{track_info.head} S:{sector_num}")

                track_info.sector_data_info[sector_num] = (data_offset, data_type, data_size)
                offset += 1 + data_size

            self.tracks[(cyl, track_info.head)] = track_info
            parsed_track_count += 1

        logger.info(f"Parsed {parsed_track_count} tracks. Max Cyl={max_cyl}, Max Head={max_head}")
        if parsed_track_count > 0:
            self._derive_physical_format(max_cyl, max_head)

    def _derive_physical_format(self, max_cyl_idx: int, max_head_idx: int) -> None:
        """
        Derives the PhysicalFormat from the parsed track data.

        Groups contiguous tracks with identical properties (rate, encoding,
        sectors per track, bytes per sector) into TrackFormat definitions.

        Args:
            max_cyl_idx: The maximum cylinder index found in the image.
            max_head_idx: The maximum head index found in the image.
        """
        if not self.tracks:
            logger.warning("No tracks to derive format from")
            return

        track_formats: List[TrackFormat] = []
        current_start_cyl = 0
        prev_props = None

        for cyl in range(max_cyl_idx + 1):
            key = (cyl, 0)  # Assume head 0 is representative of the cylinder
            track_info = self.tracks.get(key)

            if not track_info:
                if prev_props is not None:
                    self._add_track_format(track_formats, current_start_cyl, cyl - 1, prev_props, max_head_idx)
                current_start_cyl = cyl + 1
                prev_props = None
                continue

            rate, encoding, _ = IMD_MODE_MAP.get(track_info.mode, (250, "MFM", "DD"))
            spt = track_info.num_sectors
            bps = (track_info.get_sector_size(track_info.sector_num_map[0])
                   if track_info.sector_size_code == 0xFF and track_info.sector_num_map
                   else track_info.sector_size)

            current_props = (encoding, rate, spt, bps, track_info.mode)
            if prev_props is not None and current_props != prev_props:
                self._add_track_format(track_formats, current_start_cyl, cyl - 1, prev_props, max_head_idx)
                current_start_cyl = cyl

            prev_props = current_props

        if prev_props is not None:
            self._add_track_format(track_formats, current_start_cyl, max_cyl_idx, prev_props, max_head_idx)

        # Derive RPM (heuristic)
        ref_ti = self.tracks.get((0, 0)) or self.tracks[min(self.tracks.keys())]
        ref_rate, ref_encoding, _ = IMD_MODE_MAP.get(ref_ti.mode, (500, "MFM", ""))
        rpm = 360 if ref_rate == 300 or ref_encoding == "FM" else 300
        if ref_rate == 500 and ref_encoding == "MFM" and ref_ti.num_sectors == 15:
            rpm = 360  # Heuristic for 1.2MB 5.25" HD

        # Set a disk-wide BPS, defaulting to 128 if variable/mixed.
        all_bps = {tf.bytes_per_sector for tf in track_formats}
        disk_bps = list(all_bps)[0] if len(all_bps) == 1 else 128

        self.physical_format = PhysicalFormat(
            cylinders=max_cyl_idx + 1,
            heads=max_head_idx + 1,
            rpm=rpm,
            heads_inverted=False,
            bytes_per_sector=disk_bps,
            track_formats=track_formats
        )
        logger.info(f"Derived format: Cyls={self.physical_format.cylinders}, "
                    f"Heads={self.physical_format.heads}, RPM={rpm}, "
                    f"Variable BPS={len(all_bps) > 1}")

    def _add_track_format(
        self,
        track_formats: List[TrackFormat],
        start: int,
        end: int,
        props: Tuple[str, int, int, int, int],
        max_head_idx: int
    ) -> None:
        """
        Helper to create and add a TrackFormat object to a list.

        Args:
            track_formats: The list to add the new TrackFormat to.
            start: The starting cylinder for this format.
            end: The ending cylinder for this format.
            props: A tuple of (encoding, rate, spt, bps, mode).
            max_head_idx: The maximum head index for this format range.
        """
        encoding, rate, spt, bps, _ = props
        interleave = 6 if encoding == "FM" else 9
        gap3_bytes = 26 if encoding == "FM" else 54

        tf = TrackFormat(
            track_start=start,
            track_end=end,
            head_start=0,
            head_end=max_head_idx,
            sectors_per_track=spt,
            encoding=encoding,
            rate=rate,
            interleave=interleave,
            bytes_per_sector=bps,
            id_start=1,
            iam_present=True,
            gap1_bytes=None, gap2_bytes=None, gap3_bytes=gap3_bytes,
            cskew=None, hskew=None
        )
        track_formats.append(tf)

    def _read_original_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a sector's data from the originally loaded image data, bypassing
        the modified data cache.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            The raw sector data as bytes.
        """
        track_info = self.tracks.get((cylinder, head))
        expected_size = self._get_sector_size(track_info, cylinder, head, sector)

        if not track_info or sector not in track_info.sector_data_info:
            logger.warning(f"Missing track or sector C:{cylinder} H:{head} S:{sector}")
            return bytes(expected_size)

        offset, data_type, size = track_info.sector_data_info[sector]

        if data_type == IMD_SECTOR_UNAVAILABLE:
            return bytes(expected_size)
        elif data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL,
                           IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
            fill_byte = self.image_data[offset]
            return bytes([fill_byte] * expected_size)
        elif data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL,
                           IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
            data = self.image_data[offset:offset + size]
            # Pad or truncate to match expected size
            if len(data) < expected_size:
                return data.ljust(expected_size, b'\0')
            return data[:expected_size]
        else:
            logger.error(f"Unknown sector type {data_type} for C:{cylinder} H:{head} S:{sector}")
            return bytes(expected_size)

    def _get_sector_size(self, track_info: Optional[IMDTrackInfo],
                         cylinder: int, head: int, sector: int) -> int:
        """
        Determines the size of a sector, using a cache for efficiency.

        Args:
            track_info: The IMDTrackInfo for the track, if available.
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            The size of the sector in bytes.
        """
        cache_key = (cylinder, head, sector)
        if cache_key in self._sector_size_cache:
            return self._sector_size_cache[cache_key]

        size = 512  # Fallback default
        if self.physical_format:
            try:
                size = self.physical_format.get_bytes_per_sector(cylinder, head)
            except ValueError:
                logger.warning(
                    f"No track format for C:{cylinder} H:{head}. Using disk-wide "
                    f"default BPS ({self.physical_format.bytes_per_sector})."
                )
                size = self.physical_format.bytes_per_sector

        if track_info and sector in track_info.sector_num_map:
            try:
                # This overrides the format-derived size for variable-size tracks
                size = track_info.get_sector_size(sector)
            except ValueError:
                logger.warning(
                    f"Could not get sector size for C:{cylinder} H:{head} S:{sector} "
                    "from track info, using format-derived size."
                )

        self._sector_size_cache[cache_key] = size
        return size
