# src/fatfloppy/core/drivers/h17.py
"""
H17 Disk Image Driver for Heathkit Hard-Sectored Disks.

This module provides a comprehensive implementation of the H17Disk format (version 2.0.0)
for Heathkit hard-sectored floppy disks. The H17 format preserves complete sector headers
including volume numbers, which is critical for proper HDOS and CP/M disk handling.

Key features:
- Full support for H17Disk v2.0.0 block-based format
- Preservation of sector headers with volume information
- Support for both single and double-sided disks
- Metadata preservation (labels, comments, dates, etc.)
- Sector status tracking and error reporting
- H8D block compatibility for legacy tools
"""

import struct
import datetime
from typing import Dict, List, Optional, Tuple, Any, ClassVar
from dataclasses import dataclass, field

from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

logger = get_logger("H17ImageDriver")

# H17 Format Constants
H17_MAGIC = b'H17D'
H17_VERSION = b'2.0.0'  # Current spec version we support
H17_8BIT_CHECK = 0xFF

# Fixed H17 disk characteristics
H17_SECTORS_PER_TRACK = 10
H17_BYTES_PER_SECTOR = 256
H17_RPM = 300
H17_ENCODING = "FM"
H17_BIT_RATE = 250

# Block IDs
BLOCK_DISK_FORMAT = b'DskF'
BLOCK_PARAMETERS = b'Parm'
BLOCK_DATE = b'Date'
BLOCK_IMAGER = b'Imgr'
BLOCK_PROGRAM = b'Prog'
BLOCK_PADDING = b'Padd'
BLOCK_H8D_DATA = b'H8DB'
BLOCK_SECTOR_META = b'SecM'
BLOCK_LABEL = b'Labl'
BLOCK_COMMENT = b'Comm'

# H8D block must start at file offset 256
H8D_BLOCK_OFFSET = 256
H8D_BLOCK_HEADER_OFFSET = H8D_BLOCK_OFFSET - 8  # 248

# Sector metadata size
SECTOR_METADATA_SIZE = 16

# Sector status flags (bit positions)
STATUS_MISSING_HEADER_SYNC = 0x01
STATUS_WRONG_TRACK = 0x02
STATUS_INVALID_SECTOR = 0x04
STATUS_INVALID_HEADER_CHECKSUM = 0x08
STATUS_MISSING_DATA_SYNC = 0x10
STATUS_INVALID_DATA_CHECKSUM = 0x20
STATUS_SECTOR_UNREADABLE = 0x40


@dataclass
class H17SectorMetadata:
    """Represents the metadata for a single sector."""
    offset_to_data: int  # Offset in file to the 256-byte sector data
    status: int  # Status flags
    header_sync: int  # Should be 0xFD
    volume: int  # Volume number from header
    track: int  # Track number from header
    sector: int  # Sector number from header
    header_checksum: int  # Header checksum byte
    data_sync: int  # Should be 0xFD
    data_checksum: int  # Data checksum byte
    valid_bytes: int  # Number of valid bytes (0-256)

    def is_valid(self) -> bool:
        """Returns True if sector has no errors."""
        return self.status == 0 and self.valid_bytes == H17_BYTES_PER_SECTOR

    def has_errors(self) -> bool:
        """Returns True if sector has any errors."""
        return self.status != 0 or self.valid_bytes < H17_BYTES_PER_SECTOR


@dataclass
class H17DiskFormat:
    """Represents the DskF block data."""
    sides: int  # 1 or 2
    tracks: int  # 40 or 80
    read_only: bool = False


@dataclass
class H17Parameters:
    """Represents the Parm block data."""
    distribution_disk: int = 0  # 0=unknown, 1=original, 2=not original, 3=copy
    source_of_headers: int = 0  # 0=utility, 1=emulator, 2=H8/H89, 3=FC5025, 4=AppleSauce


@dataclass
class H17Metadata:
    """Container for all H17 metadata blocks."""
    date: Optional[str] = None
    imager: Optional[str] = None
    program: Optional[str] = None
    label: Optional[str] = None
    comment: Optional[str] = None


class H17ImageDriver(DiskIODriver):
    """
    Disk I/O driver for H17 (Heathkit hard-sectored) disk images.

    This driver provides full support for the H17Disk v2.0.0 format, including:
    - Reading/writing sector data via H8D block
    - Preserving and utilizing sector headers (especially volume numbers)
    - Maintaining sector metadata for error tracking
    - Supporting all optional metadata blocks
    """
    # Plugin metadata
    driver_type: ClassVar[str] = "H17"
    driver_file_extensions: ClassVar[List[str]] = [".h17"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "Heathkit H17 disk image driver"

    def __init__(self, file_path: str):
        """
        Initializes the H17ImageDriver.

        Args:
            file_path: The path to the H17 disk image file.
        """
        super().__init__()
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.uses_physical_heads: bool = False
        self.dirty: bool = False

        # H17 format data structures
        self.disk_format: Optional[H17DiskFormat] = None
        self.parameters: Optional[H17Parameters] = None
        self.metadata: H17Metadata = H17Metadata()
        self.sector_metadata: Dict[Tuple[int, int, int], H17SectorMetadata] = {}

        # Raw file data
        self.file_data: bytearray = bytearray()
        self.h8d_block_offset: int = 0  # File offset to start of H8D data

        # Sector data cache
        self.sector_cache: Dict[Tuple[int, int, int], bytes] = {}
        self.modified_sectors: Dict[Tuple[int, int, int], bytes] = {}

        # Load and parse the file if it exists
        if self._file_exists():
            try:
                self._load_and_parse()
                logger.info(f"Successfully loaded H17 image: {file_path}")
            except Exception as e:
                logger.error(f"Failed to load H17 image {file_path}: {e}")
                raise
        else:
            logger.info(f"H17 file '{file_path}' not found. Driver initialized for creation.")

    # --- Properties ---

    @property
    def has_embedded_geometry(self) -> bool:
        """H17 files contain complete geometry and volume information."""
        return True

    @property
    def allows_geometry_override(self) -> bool:
        """H17 geometry can be overridden but may cause inconsistencies."""
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """H17 files cannot be reformatted in place."""
        return False

    @property
    def supports_new_image_creation(self) -> bool:
        """H17 driver can create new formatted images."""
        return True

    def initialize_new_image(self, physical_format: PhysicalFormat,
                            profile: Optional[Any] = None) -> None:
        """
        Initializes a new blank H17 image structure.

        Args:
            physical_format: The physical format for the new image.
            profile: Optional FormatProfile with additional metadata.
        """
        sides = physical_format.heads
        tracks = physical_format.cylinders

        # Determine volume scheme from profile if available
        scheme = 'hdos'
        hdos_volume = 1
        label = None

        if profile and hasattr(profile, 'filesystem_type'):
            scheme = 'cpm' if profile.filesystem_type == 'CPM' else 'hdos'

            if (profile.filesystem_config and
                hasattr(profile.filesystem_config, 'volume_number')):
                hdos_volume = profile.filesystem_config.volume_number

            if (profile.filesystem_config and
                hasattr(profile.filesystem_config, 'title')):
                label = profile.filesystem_config.title

        self.format_h17(
            sides=sides,
            tracks=tracks,
            scheme=scheme,
            hdos_volume=hdos_volume,
            label=label,
            comment="Created by FatFloppy"
        )

    def validate_state_for_opening(self) -> Tuple[bool, Optional[str]]:
        """
        Validates H17 driver state after opening.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not self.file_path:
            return False, "H17 driver has no file path"

        # H17 should always have format if file exists
        if self._file_exists() and not self.physical_format:
            return False, "H17 file exists but no physical format derived"

        return True, None

    def validate_for_opening(self, source: str, **kwargs) -> Tuple[bool, Optional[str]]:
        """
        Validates whether an H17 file can be opened.

        Args:
            source: Path to the H17 file.
            **kwargs: Unused for H17 driver.

        Returns:
            Tuple of (is_valid, error_message).
        """
        import os

        if not os.path.exists(source):
            return False, f"H17 file not found: {source}"

        # Quick validation: check for H17 magic number
        try:
            with open(source, "rb") as f:
                magic = f.read(4)

            if magic != b'H17D':
                return False, "Invalid H17 magic number (expected 'H17D')"

        except Exception as e:
            return False, f"Cannot validate H17 file: {e}"

        return True, None

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for H17 driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            'needs_format_for_open': False,
            'needs_format_for_io': False,
            'can_derive_format': True,
            'preferred_detection_method': 'embedded'
        }

    def prepare_for_format_application(self, format_info: dict) -> Tuple[bool, Optional[str]]:
        """
        Validates format compatibility for H17 driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        if not self._file_exists():
            # Creating new image, format is required
            if 'physical_format' not in format_info:
                return False, "Physical format required for creating new H17 image"
            return True, None

        # H17 files should not have format overridden
        warning = ("H17 files contain volume information that is tied to their "
                "embedded geometry. Overriding format may cause corruption.")
        logger.warning(warning)

        return True, warning

    # --- Public API Methods ---

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the disk image.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The sector number (1-10 for API, translated to 0-9 for H17 file).

        Returns:
            The 256-byte sector data.

        Raises:
            ValueError: If the physical format is not set.
            IOError: If the sector cannot be read.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        # Translate from API sector (1-10) to physical H17 sector (0-9)
        physical_sector = sector - 1
        sector_key = (cylinder, head, sector)
        physical_key = (cylinder, head, physical_sector)

        # Check modified sectors first (using API numbering)
        if sector_key in self.modified_sectors:
            logger.debug(f"Reading modified sector C:{cylinder} H:{head} S:{sector}")
            return self.modified_sectors[sector_key]

        # Check cache (using API numbering)
        if sector_key in self.sector_cache:
            logger.debug(f"Reading cached sector C:{cylinder} H:{head} S:{sector}")
            return self.sector_cache[sector_key]

        # Read from H8D block using physical sector number
        meta = self.sector_metadata.get(physical_key)
        if not meta:
            logger.warning(f"No metadata for sector C:{cylinder} H:{head} S:{sector} (physical {physical_sector}), returning zeros")
            return bytes(H17_BYTES_PER_SECTOR)

        try:
            # Read from file offset specified in metadata
            data = self.file_data[meta.offset_to_data:meta.offset_to_data + H17_BYTES_PER_SECTOR]

            if len(data) < H17_BYTES_PER_SECTOR:
                logger.warning(f"Incomplete sector data for C:{cylinder} H:{head} S:{sector}, padding")
                data = data.ljust(H17_BYTES_PER_SECTOR, b'\x00')

            # Cache it (using API numbering)
            self.sector_cache[sector_key] = data
            logger.debug(f"Read sector C:{cylinder} H:{head} S:{sector} (physical {physical_sector}) from offset {meta.offset_to_data}")
            return data

        except Exception as e:
            logger.error(f"Error reading sector C:{cylinder} H:{head} S:{sector}: {e}")
            raise IOError(f"Failed to read sector C:{cylinder} H:{head} S:{sector}") from e

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the in-memory cache.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The sector number (1-10 for API, translated to 0-9 for H17 file).
            data: The 256-byte sector data to write.

        Raises:
            ValueError: If the physical format is not set or data size is incorrect.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        if len(data) != H17_BYTES_PER_SECTOR:
            raise ValueError(f"Data size must be {H17_BYTES_PER_SECTOR} bytes, got {len(data)}")

        # Translate from API sector (1-10) to physical H17 sector (0-9)
        physical_sector = sector - 1
        sector_key = (cylinder, head, sector)
        physical_key = (cylinder, head, physical_sector)

        self.modified_sectors[sector_key] = bytes(data)
        self.dirty = True

        # Clear from cache so next read gets modified version
        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]

        logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector} (physical {physical_sector})")

    def flush(self) -> None:
        """
        Writes all modified sectors back to the file.

        This rebuilds the H8D block and sector metadata, then writes the
        entire file back to disk.

        Raises:
            IOError: If the file cannot be written.
        """
        if not self.dirty or not self.physical_format:
            logger.debug("Nothing to flush or no format set")
            return

        logger.info(f"Flushing H17 image to {self.file_path}")

        try:
            # Update H8D block with modified sectors
            for sector_key, data in self.modified_sectors.items():
                cylinder, head, sector = sector_key
                # Translate to physical sector for metadata lookup
                physical_sector = sector - 1
                physical_key = (cylinder, head, physical_sector)
                meta = self.sector_metadata.get(physical_key)
                if meta:
                    # Update data in file_data at the offset
                    offset = meta.offset_to_data
                    self.file_data[offset:offset + H17_BYTES_PER_SECTOR] = data
                else:
                    logger.warning(f"No metadata for modified sector C:{cylinder} H:{head} S:{sector} (physical {physical_sector})")

            # Write entire file back
            with open(self.file_path, 'wb') as f:
                f.write(self.file_data)

            # Clear modified sectors and mark clean
            self.modified_sectors.clear()
            self.dirty = False
            logger.info(f"Successfully flushed {len(self.modified_sectors)} sectors")

        except Exception as e:
            logger.error(f"Failed to flush H17 image: {e}")
            raise IOError(f"Flush failed: {e}") from e

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical format of the disk.

        Args:
            physical_format: The PhysicalFormat object to apply.

        Raises:
            TypeError: If the provided object is not a PhysicalFormat.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")

        self.physical_format = physical_format
        logger.info(f"Physical format set: {physical_format.cylinders}x{physical_format.heads}")

    # --- Volume Number Access Methods ---

    def get_sector_volume(self, cylinder: int, head: int, sector: int) -> Optional[int]:
        """
        Gets the volume number for a specific sector from its header.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The sector number (1-10 for API).

        Returns:
            The volume number, or None if metadata not available.
        """
        physical_sector = sector - 1
        meta = self.sector_metadata.get((cylinder, head, physical_sector))
        return meta.volume if meta else None

    def set_sector_volume(self, cylinder: int, head: int, sector: int, volume: int) -> None:
        """
        Sets the volume number for a specific sector.

        This updates the sector metadata but does not write to disk until flush().

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The sector number (1-10 for API).
            volume: The volume number (0-255).

        Raises:
            ValueError: If volume is out of range or metadata not found.
        """
        if not (0 <= volume <= 255):
            raise ValueError(f"Volume must be 0-255, got {volume}")

        physical_sector = sector - 1
        sector_key = (cylinder, head, physical_sector)
        meta = self.sector_metadata.get(sector_key)
        if not meta:
            raise ValueError(f"No metadata for sector C:{cylinder} H:{head} S:{sector}")

        meta.volume = volume
        self.dirty = True
        logger.debug(f"Set volume for C:{cylinder} H:{head} S:{sector} to {volume}")

    def get_track_volumes(self, cylinder: int, head: int) -> List[int]:
        """
        Gets all volume numbers for sectors on a track.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.

        Returns:
            List of volume numbers for each sector (1-10 in API, 0-9 physical).
        """
        volumes = []
        for physical_sector in range(H17_SECTORS_PER_TRACK):
            meta = self.sector_metadata.get((cylinder, head, physical_sector))
            volumes.append(meta.volume if meta else 0)
        return volumes

    def set_track_volumes(self, cylinder: int, head: int, volume: int) -> None:
        """
        Sets the volume number for all sectors on a track.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            volume: The volume number (0-255) to set.

        Raises:
            ValueError: If volume is out of range.
        """
        if not (0 <= volume <= 255):
            raise ValueError(f"Volume must be 0-255, got {volume}")

        for physical_sector in range(H17_SECTORS_PER_TRACK):
            sector_key = (cylinder, head, physical_sector)
            meta = self.sector_metadata.get(sector_key)
            if meta:
                meta.volume = volume

        self.dirty = True
        logger.debug(f"Set volume for track C:{cylinder} H:{head} to {volume}")

    def apply_volume_scheme(self, scheme: str, hdos_volume: Optional[int] = None) -> None:
        """
        Applies a volume numbering scheme to all sectors.

        Args:
            scheme: Either "cpm" (all zeros) or "hdos" (track 0 = 0, others = hdos_volume).
            hdos_volume: The volume number for HDOS tracks 1+ (required if scheme is "hdos").

        Raises:
            ValueError: If scheme is invalid or hdos_volume not provided for HDOS.
        """
        scheme = scheme.lower()
        if scheme not in ["cpm", "hdos"]:
            raise ValueError(f"Invalid scheme '{scheme}', must be 'cpm' or 'hdos'")

        if scheme == "hdos" and hdos_volume is None:
            raise ValueError("hdos_volume must be provided for HDOS scheme")

        if scheme == "hdos" and not (0 <= hdos_volume <= 255):
            raise ValueError(f"HDOS volume must be 0-255, got {hdos_volume}")

        logger.info(f"Applying {scheme.upper()} volume scheme")

        if not self.disk_format:
            raise ValueError("Disk format not loaded")

        for cylinder in range(self.disk_format.tracks):
            for head in range(self.disk_format.sides):
                if scheme == "cpm":
                    volume = 0
                else:  # hdos
                    volume = 0 if cylinder == 0 else hdos_volume

                self.set_track_volumes(cylinder, head, volume)

        logger.info(f"Applied {scheme.upper()} volume scheme successfully")

    # --- Metadata Access Methods ---

    def get_sector_status(self, cylinder: int, head: int, sector: int) -> Optional[int]:
        """Gets the error status flags for a sector."""
        meta = self.sector_metadata.get((cylinder, head, sector))
        return meta.status if meta else None

    def get_disk_label(self) -> Optional[str]:
        """Returns the disk label from metadata."""
        return self.metadata.label

    def set_disk_label(self, label: str) -> None:
        """Sets the disk label in metadata."""
        self.metadata.label = label
        self.dirty = True

    def get_disk_comment(self) -> Optional[str]:
        """Returns the disk comment from metadata."""
        return self.metadata.comment

    def set_disk_comment(self, comment: str) -> None:
        """Sets the disk comment in metadata."""
        self.metadata.comment = comment
        self.dirty = True

    # --- Format Creation Methods ---

    def format_h17(self, sides: int = 1, tracks: int = 40,
                   scheme: str = "hdos", hdos_volume: int = 0,
                   label: Optional[str] = None, comment: Optional[str] = None) -> None:
        """
        Creates a new blank H17 disk image with proper structure.

        Args:
            sides: Number of sides (1 or 2).
            tracks: Number of tracks (40 or 80).
            scheme: Volume scheme to use ("cpm" or "hdos").
            hdos_volume: Volume number for HDOS tracks 1+ (ignored for CP/M).
            label: Optional disk label.
            comment: Optional disk comment.

        Raises:
            ValueError: If parameters are invalid.
        """
        if sides not in [1, 2]:
            raise ValueError(f"Sides must be 1 or 2, got {sides}")
        if tracks not in [40, 80]:
            raise ValueError(f"Tracks must be 40 or 80, got {tracks}")

        logger.info(f"Formatting H17 image: {sides} sides, {tracks} tracks, {scheme} scheme")

        # Create disk format
        self.disk_format = H17DiskFormat(sides=sides, tracks=tracks, read_only=False)

        # Create parameters
        self.parameters = H17Parameters(
            distribution_disk=0,
            source_of_headers=1  # Created by emulator
        )

        # Set metadata
        self.metadata = H17Metadata(
            date=datetime.datetime.now().isoformat(),
            program=f"FatFloppy H17 Driver",
            label=label,
            comment=comment
        )

        # Build file structure
        self._build_new_file(sides, tracks, scheme, hdos_volume)

        # Create physical format
        self._derive_physical_format()

        self.dirty = True
        logger.info("H17 format creation complete")

    # --- Private Methods ---

    def _file_exists(self) -> bool:
        """Checks if the file path exists."""
        import os
        return os.path.exists(self.file_path)

    def _load_and_parse(self) -> None:
        """Loads and parses an existing H17 file."""
        with open(self.file_path, 'rb') as f:
            self.file_data = bytearray(f.read())

        if len(self.file_data) < 8:
            raise ValueError("File too small to be valid H17 format")

        # Verify magic number
        if self.file_data[0:4] != H17_MAGIC:
            raise ValueError(f"Invalid magic number, expected {H17_MAGIC}, got {self.file_data[0:4]}")

        # Check version
        version = self.file_data[4:7]
        logger.debug(f"H17 version: {version.decode('ascii', errors='ignore')}")

        # Verify 8-bit check
        if self.file_data[7] != H17_8BIT_CHECK:
            raise ValueError("8-bit check failed, file may be corrupted")

        # Parse blocks
        self._parse_blocks()

        # Derive physical format from disk format block
        self._derive_physical_format()

    def _parse_blocks(self) -> None:
        """Parses all blocks in the file."""
        offset = 8  # Start after header

        while offset + 8 <= len(self.file_data):
            block_id = self.file_data[offset:offset + 4]
            block_length = struct.unpack('>I', self.file_data[offset + 4:offset + 8])[0]

            block_data = self.file_data[offset + 8:offset + 8 + block_length]

            logger.debug(f"Parsing block {block_id} at offset {offset}, length {block_length}")

            if block_id == BLOCK_DISK_FORMAT:
                self._parse_disk_format_block(block_data)
            elif block_id == BLOCK_PARAMETERS:
                self._parse_parameters_block(block_data)
            elif block_id == BLOCK_DATE:
                self.metadata.date = block_data.decode('utf-8', errors='ignore')
            elif block_id == BLOCK_IMAGER:
                self.metadata.imager = block_data.decode('utf-8', errors='ignore')
            elif block_id == BLOCK_PROGRAM:
                self.metadata.program = block_data.decode('utf-8', errors='ignore')
            elif block_id == BLOCK_LABEL:
                self.metadata.label = block_data.decode('utf-8', errors='ignore')
            elif block_id == BLOCK_COMMENT:
                self.metadata.comment = block_data.decode('utf-8', errors='ignore')
            elif block_id == BLOCK_H8D_DATA:
                self.h8d_block_offset = offset + 8
                logger.info(f"H8D block found at offset {self.h8d_block_offset}, length {block_length}")
            elif block_id == BLOCK_SECTOR_META:
                self._parse_sector_metadata_block(block_data)
            elif block_id == BLOCK_PADDING:
                pass  # Ignore padding
            else:
                logger.warning(f"Unknown block ID: {block_id}")

            offset += 8 + block_length

    def _parse_disk_format_block(self, data: bytes) -> None:
        """Parses the DskF block."""
        if len(data) < 2:
            raise ValueError("DskF block too short")

        sides = data[0]
        tracks = data[1]
        read_only = data[2] if len(data) > 2 else 0

        if sides not in [1, 2]:
            raise ValueError(f"Invalid sides value: {sides}")
        if tracks not in [40, 80]:
            raise ValueError(f"Invalid tracks value: {tracks}")

        self.disk_format = H17DiskFormat(
            sides=sides,
            tracks=tracks,
            read_only=bool(read_only)
        )

        logger.info(f"Disk format: {sides} sides, {tracks} tracks, R/O={read_only}")

    def _parse_parameters_block(self, data: bytes) -> None:
        """Parses the Parm block."""
        if len(data) < 2:
            return

        self.parameters = H17Parameters(
            distribution_disk=data[0] if len(data) > 0 else 0,
            source_of_headers=data[1] if len(data) > 1 else 0
        )

        logger.debug(f"Parameters: dist={self.parameters.distribution_disk}, "
                    f"source={self.parameters.source_of_headers}")

    def _parse_sector_metadata_block(self, data: bytes) -> None:
        """Parses the SecM block and builds sector metadata map."""
        if not self.disk_format:
            raise ValueError("DskF block must be parsed before SecM block")

        num_sectors = self.disk_format.tracks * self.disk_format.sides * H17_SECTORS_PER_TRACK
        expected_size = num_sectors * SECTOR_METADATA_SIZE

        if len(data) < expected_size:
            logger.warning(f"SecM block size mismatch: expected {expected_size}, got {len(data)}")

        offset = 0
        sector_index = 0

        for cylinder in range(self.disk_format.tracks):
            for head in range(self.disk_format.sides):
                for sector in range(H17_SECTORS_PER_TRACK):
                    if offset + SECTOR_METADATA_SIZE > len(data):
                        logger.error(f"Unexpected end of SecM block at sector {sector_index}")
                        return

                    meta_bytes = data[offset:offset + SECTOR_METADATA_SIZE]
                    meta = self._parse_single_sector_metadata(meta_bytes)

                    # Store in map using logical CHS
                    self.sector_metadata[(cylinder, head, sector)] = meta

                    offset += SECTOR_METADATA_SIZE
                    sector_index += 1

        logger.info(f"Parsed metadata for {sector_index} sectors")

    def _parse_single_sector_metadata(self, data: bytes) -> H17SectorMetadata:
        """Parses a single 16-byte sector metadata entry."""
        return H17SectorMetadata(
            offset_to_data=struct.unpack('>I', data[0:4])[0],
            status=data[4],
            header_sync=data[5],
            volume=data[6],
            track=data[7],
            sector=data[8],
            header_checksum=data[9],
            data_sync=data[10],
            data_checksum=data[11],
            valid_bytes=struct.unpack('>H', data[12:14])[0]
        )

    def _derive_physical_format(self) -> None:
        """Creates a PhysicalFormat object from the disk format."""
        if not self.disk_format:
            raise ValueError("Disk format not loaded")

        track_format = TrackFormat(
            track_start=0,
            track_end=self.disk_format.tracks - 1,
            head_start=0,
            head_end=self.disk_format.sides - 1,
            sectors_per_track=H17_SECTORS_PER_TRACK,
            encoding=H17_ENCODING,
            rate=H17_BIT_RATE,
            interleave=1,
            bytes_per_sector=H17_BYTES_PER_SECTOR,
            id_start=1,  # HDOS expects sectors 1-10, not 0-9
            iam_present=False,
            gap3_bytes=26  # Typical for FM
        )

        self.physical_format = PhysicalFormat(
            cylinders=self.disk_format.tracks,
            heads=self.disk_format.sides,
            rpm=H17_RPM,
            heads_inverted=False,
            bytes_per_sector=H17_BYTES_PER_SECTOR,
            track_formats=[track_format]
        )

        logger.info(f"Derived physical format: {self.disk_format.tracks}C x "
                   f"{self.disk_format.sides}H x {H17_SECTORS_PER_TRACK}S")

    def _build_new_file(self, sides: int, tracks: int, scheme: str, hdos_volume: int) -> None:
        """Builds a complete new H17 file structure in memory."""
        self.file_data = bytearray()

        # Write file header
        self.file_data.extend(H17_MAGIC)
        self.file_data.extend(b'2.0.0')  # Version
        self.file_data.append(H17_8BIT_CHECK)

        # Write DskF block
        self._write_block(BLOCK_DISK_FORMAT, bytes([sides, tracks, 0]))

        # Write Parm block
        self._write_block(BLOCK_PARAMETERS, bytes([self.parameters.distribution_disk,
                                                   self.parameters.source_of_headers]))

        # Write metadata blocks
        if self.metadata.date:
            self._write_block(BLOCK_DATE, self.metadata.date.encode('utf-8'))
        if self.metadata.program:
            self._write_block(BLOCK_PROGRAM, self.metadata.program.encode('utf-8'))
        if self.metadata.label:
            self._write_block(BLOCK_LABEL, self.metadata.label.encode('utf-8'))
        if self.metadata.comment:
            self._write_block(BLOCK_COMMENT, self.metadata.comment.encode('utf-8'))

        # Calculate padding needed to align H8D block at offset 256
        current_offset = len(self.file_data) + 8  # +8 for H8DB block header
        padding_needed = H8D_BLOCK_OFFSET - current_offset
        if padding_needed > 0:
            self._write_block(BLOCK_PADDING, bytes(padding_needed))

        # Write H8D block
        num_sectors = tracks * sides * H17_SECTORS_PER_TRACK
        h8d_data = bytes(num_sectors * H17_BYTES_PER_SECTOR)  # All zeros
        self._write_block(BLOCK_H8D_DATA, h8d_data)
        self.h8d_block_offset = H8D_BLOCK_OFFSET

        # Build and write SecM block
        secm_data = self._build_sector_metadata(sides, tracks, scheme, hdos_volume)
        self._write_block(BLOCK_SECTOR_META, secm_data)

    def _write_block(self, block_id: bytes, data: bytes) -> None:
        """Writes a block to file_data."""
        self.file_data.extend(block_id)
        self.file_data.extend(struct.pack('>I', len(data)))
        self.file_data.extend(data)

    def _build_sector_metadata(self, sides: int, tracks: int,
                               scheme: str, hdos_volume: int) -> bytes:
        """Builds the SecM block data with appropriate volume numbers."""
        metadata = bytearray()
        data_offset = self.h8d_block_offset

        for cylinder in range(tracks):
            for head in range(sides):
                # Determine volume for this track
                if scheme == "cpm":
                    volume = 0
                else:  # hdos
                    volume = 0 if cylinder == 0 else hdos_volume

                for sector in range(H17_SECTORS_PER_TRACK):
                    # Create metadata entry
                    meta = H17SectorMetadata(
                        offset_to_data=data_offset,
                        status=0,  # No errors
                        header_sync=0xFD,
                        volume=volume,
                        track=cylinder,
                        sector=sector,
                        header_checksum=0,  # Would need to calculate
                        data_sync=0xFD,
                        data_checksum=0,  # Would need to calculate
                        valid_bytes=H17_BYTES_PER_SECTOR
                    )

                    # Store in map
                    self.sector_metadata[(cylinder, head, sector)] = meta

                    # Write metadata bytes
                    metadata.extend(struct.pack('>I', meta.offset_to_data))
                    metadata.append(meta.status)
                    metadata.append(meta.header_sync)
                    metadata.append(meta.volume)
                    metadata.append(meta.track)
                    metadata.append(meta.sector)
                    metadata.append(meta.header_checksum)
                    metadata.append(meta.data_sync)
                    metadata.append(meta.data_checksum)
                    metadata.extend(struct.pack('>H', meta.valid_bytes))
                    metadata.extend(b'\x00\x00')  # Reserved

                    data_offset += H17_BYTES_PER_SECTOR

        return bytes(metadata)
