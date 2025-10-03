# src/fatfloppy/core/drivers/mits_dsk.py
"""
MITS Altair .DSK file format driver.

This module provides a DiskIODriver for handling MITS Altair 8800 disk images
in the .DSK format. This format uses 137-byte physical sectors that contain:
- Sector header with track/sector numbers and checksums
- 128 bytes of actual data
- Additional metadata bytes

The driver exposes 128-byte sectors in physical disk order. The filesystem
is responsible for any sector reordering using its own skew table.
"""

import copy
import os
from typing import Dict, List, Optional, Tuple, Any, ClassVar

from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

logger = get_logger("MITSDSKDriver")

# MITS Altair disk constants
MITS_TRACKS = 77
MITS_SECTORS_PER_TRACK = 32
MITS_PHYSICAL_SECTOR_SIZE = 137
MITS_LOGICAL_SECTOR_SIZE = 128
MITS_ENCODING = "FM"
MITS_RATE = 250
MITS_RPM = 360


class MITSDSKDriver(DiskIODriver):
    """
    Disk I/O driver for MITS Altair .DSK format.

    This driver handles the physical sector layout of MITS Altair disks,
    extracting 128-byte data from 137-byte physical sectors. Sectors are
    exposed in physical disk order - no reordering is performed.
    """
    # Plugin metadata
    driver_type: ClassVar[str] = "MITS_DSK"
    driver_file_extensions: ClassVar[List[str]] = [".dsk"]
    driver_category: ClassVar[str] = "raw"  # Fixed format, not self-describing
    driver_description: ClassVar[str] = "MITS Altair DSK format driver"
    driver_priority: ClassVar[int] = 100  # Higher priority than raw IMG

    def __init__(self, file_path: str, image_data: Optional[bytes] = None):
        """
        Initializes the MITSDSKDriver.

        Args:
            file_path: The path to the MITS .DSK file.
            image_data: Optional byte array to initialize with.

        Raises:
            ValueError: If file format is invalid.
            FileNotFoundError: If the specified file does not exist.
        """
        super().__init__()
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.dirty: bool = False
        self.image_data: bytearray
        self.uses_physical_heads: bool = False

        # Cache for sector data
        self.sector_cache: Dict[Tuple[int, int, int], bytes] = {}
        self.modified_sectors: Dict[Tuple[int, int, int], bytes] = {}

        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
            self._validate_format()
            logger.debug(f"Initialized MITS DSK driver with provided data of size {len(self.image_data)}")
        else:
            if not self.file_path:
                logger.error("MITS DSK driver initialized without file_path and no image_data.")
                raise ValueError("File path must be provided for MITS DSK driver if image_data is not given.")

            try:
                with open(self.file_path, "rb") as f:
                    self.image_data = bytearray(f.read())

                self._validate_format()
                logger.info(f"Loaded MITS DSK file {self.file_path}, size {len(self.image_data)}")
            except FileNotFoundError:
                logger.error(f"MITS DSK file not found: {self.file_path}")
                raise
            except ValueError as ve:
                logger.error(f"Invalid MITS DSK format in {self.file_path}: {ve}")
                raise
            except Exception as e:
                logger.error(f"Failed to read MITS DSK file {self.file_path}: {e}")
                if isinstance(e, OSError):
                    raise
                raise IOError(f"Failed to read MITS DSK file {self.file_path}") from e

        # Don't create physical format - let controller apply profile like IMG does

    # --- Properties ---

    @property
    def has_embedded_geometry(self) -> bool:
        """MITS DSK has fixed structure but no self-describing geometry."""
        return False

    @property
    def allows_geometry_override(self) -> bool:
        """MITS DSK requires external geometry specification."""
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """MITS DSK files can be formatted by overwriting content."""
        return True

    @property
    def supports_new_image_creation(self) -> bool:
        """MITS DSK driver can create new blank images."""
        return True

    def validate_state_for_opening(self) -> Tuple[bool, Optional[str]]:
        """
        Validates MITS DSK driver state after opening.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not hasattr(self, 'image_data') or not self.image_data:
            return False, "MITS DSK driver has no image data"

        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        if len(self.image_data) < expected_size:
            return False, f"MITS DSK file too small: {len(self.image_data)} < {expected_size}"

        # Physical format not required at open time (like IMG driver)
        return True, None

    def validate_for_opening(self, source: str, **kwargs) -> Tuple[bool, Optional[str]]:
        """
        Validates whether a file is a valid MITS Altair .DSK format.

        Uses checksum validation on multiple sectors to detect format.

        Args:
            source: Path to the .DSK file.
            **kwargs: Unused for MITS DSK driver.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not os.path.exists(source):
            return False, f"File not found: {source}"

        try:
            with open(source, "rb") as f:
                data = f.read()

            # Check file size
            expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
            if len(data) < expected_size:
                return False, f"File too small for MITS DSK format: {len(data)} < {expected_size}"

            # Validate checksums on several sectors from different tracks
            validation_count = 0
            for track in [0, 1, 5, 6, 20, 40]:
                for phys_sector in [0, 10, 20]:
                    offset = (track * MITS_SECTORS_PER_TRACK + phys_sector) * MITS_PHYSICAL_SECTOR_SIZE
                    if offset + MITS_PHYSICAL_SECTOR_SIZE <= len(data):
                        sector_bytes = data[offset:offset + MITS_PHYSICAL_SECTOR_SIZE]
                        if self._validate_sector_checksum(sector_bytes, track):
                            validation_count += 1

            # Need at least 8 valid checksums to be confident
            if validation_count < 8:
                return False, f"MITS DSK checksum validation failed: only {validation_count} valid sectors found"

            logger.info(f"MITS DSK format validated with {validation_count} valid checksums")
            return True, None

        except Exception as e:
            return False, f"Error validating MITS DSK format: {e}"

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for MITS DSK driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            'needs_format_for_open': False,
            'needs_format_for_io': True,     # Needs format for actual I/O
            'can_derive_format': True,       # Detection system can figure it out
            'preferred_detection_method': 'auto'  # Should use auto-detection
        }

    def prepare_for_format_application(self, format_info: dict) -> Tuple[bool, Optional[str]]:
        """
        MITS DSK allows temporary format override for filesystem validation.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        return True, None

    # --- Public Methods ---

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector (128 bytes) from the disk image in physical order.

        No sector reordering is performed - sectors are read from their physical
        positions on disk. The filesystem is responsible for any sector translation.

        Args:
            cylinder: The cylinder (track) number.
            head: The head number (always 0 for MITS).
            sector: The physical sector number (1-32).

        Returns:
            The 128-byte sector data.

        Raises:
            ValueError: If the physical format has not been set.
            IOError: If the sector is out of bounds.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        if head != 0:
            raise IOError(f"MITS DSK only supports head 0, got head {head}")

        if not (1 <= sector <= MITS_SECTORS_PER_TRACK):
            raise IOError(f"Invalid sector number: {sector} (must be 1-{MITS_SECTORS_PER_TRACK})")

        sector_key = (cylinder, head, sector)

        # Check modified sectors first
        if sector_key in self.modified_sectors:
            logger.debug(f"Reading modified sector C:{cylinder} H:{head} S:{sector}")
            return self.modified_sectors[sector_key]

        # Check cache
        if sector_key in self.sector_cache:
            return self.sector_cache[sector_key]

        # Read from physical position (sector numbering is 1-based, convert to 0-based for offset)
        physical_index = sector - 1
        offset = (cylinder * MITS_SECTORS_PER_TRACK + physical_index) * MITS_PHYSICAL_SECTOR_SIZE

        if offset + MITS_PHYSICAL_SECTOR_SIZE > len(self.image_data):
            raise IOError(f"Sector C:{cylinder} H:{head} S:{sector} out of bounds")

        sector_bytes = self.image_data[offset:offset + MITS_PHYSICAL_SECTOR_SIZE]
        data = self._extract_sector_data(sector_bytes, cylinder)

        # Cache it
        self.sector_cache[sector_key] = data
        logger.debug(f"Read sector C:{cylinder} H:{head} S:{sector} from physical position {physical_index}")
        return data

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the in-memory image data in physical order.

        Args:
            cylinder: The cylinder number.
            head: The head number (must be 0).
            sector: The physical sector number (1-32).
            data: The 128-byte sector data to write.

        Raises:
            ValueError: If parameters are invalid or data size is incorrect.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        if head != 0:
            raise ValueError(f"MITS DSK only supports head 0, got head {head}")

        if not (1 <= sector <= MITS_SECTORS_PER_TRACK):
            raise ValueError(f"Invalid sector number: {sector} (must be 1-{MITS_SECTORS_PER_TRACK})")

        if len(data) != MITS_LOGICAL_SECTOR_SIZE:
            raise ValueError(f"Data size must be {MITS_LOGICAL_SECTOR_SIZE} bytes, got {len(data)}")

        sector_key = (cylinder, head, sector)
        self.modified_sectors[sector_key] = bytes(data)
        self.dirty = True

        # Clear from cache
        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]

        logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector}")

    def flush(self) -> None:
        """
        Writes the in-memory image data back to the file.

        Raises:
            IOError: If the file cannot be written.
        """
        if not self.dirty:
            logger.debug("No changes to flush")
            return

        try:
            # Write modified sectors back to image_data
            for sector_key, data in self.modified_sectors.items():
                cylinder, head, sector = sector_key

                # Convert 1-based sector to 0-based physical position
                physical_index = sector - 1
                offset = (cylinder * MITS_SECTORS_PER_TRACK + physical_index) * MITS_PHYSICAL_SECTOR_SIZE

                # Read existing physical sector
                old_sector = self.image_data[offset:offset + MITS_PHYSICAL_SECTOR_SIZE]

                # Reconstruct physical sector with new data
                new_sector = self._reconstruct_physical_sector(old_sector, data, cylinder, physical_index)

                # Write back
                self.image_data[offset:offset + MITS_PHYSICAL_SECTOR_SIZE] = new_sector

            # Write entire file
            with open(self.file_path, "wb") as f:
                f.write(self.image_data)

            logger.info(f"Flushed {len(self.modified_sectors)} sectors to {self.file_path}")
            self.modified_sectors.clear()
            self.dirty = False

        except Exception as e:
            logger.error(f"Failed to flush MITS DSK image to {self.file_path}: {e}")
            raise IOError(f"Flush failed: {e}") from e

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical format of the disk.

        While MITS DSK has a fixed on-disk format, the physical_format can be
        temporarily overridden for filesystem validation purposes.

        Args:
            physical_format: The format to set.

        Raises:
            TypeError: If the provided object is not a PhysicalFormat.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")

        # Check if this is changing from the native MITS format
        if self.physical_format and not self._formats_match(physical_format, self.physical_format):
            logger.warning(
                f"Overriding MITS DSK native format "
                f"({self.physical_format.cylinders}C x {self.physical_format.heads}H x "
                f"{self.physical_format.bytes_per_sector}B) with "
                f"({physical_format.cylinders}C x {physical_format.heads}H x "
                f"{physical_format.bytes_per_sector}B). "
                f"This is allowed for filesystem validation but may cause I/O errors."
            )

        self.physical_format = copy.deepcopy(physical_format)
        logger.debug(f"Physical format set: {physical_format.cylinders}C x {physical_format.heads}H")

    def initialize_new_image(self, physical_format: PhysicalFormat,
                            profile: Optional[Any] = None) -> None:
        """
        Creates a new blank MITS DSK image.

        Args:
            physical_format: Ignored (MITS format is fixed).
            profile: Ignored.
        """
        logger.info("Creating new blank MITS DSK image")

        # Create blank image
        total_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        self.image_data = bytearray(total_size)

        # Initialize each physical sector with proper structure
        for track in range(MITS_TRACKS):
            for phys_sector in range(MITS_SECTORS_PER_TRACK):
                offset = (track * MITS_SECTORS_PER_TRACK + phys_sector) * MITS_PHYSICAL_SECTOR_SIZE

                # Create blank sector structure
                sector_data = bytearray(MITS_PHYSICAL_SECTOR_SIZE)

                if track < 6:
                    # System track structure
                    sector_data[0] = 0x00  # Sync byte
                    sector_data[1] = track
                    sector_data[2] = phys_sector
                    # Data bytes 3-130 are already zero
                    # Checksums at 131-136
                    sector_data[131:137] = self._calculate_sector_checksums(
                        bytes(sector_data[3:131]), track, phys_sector
                    )
                else:
                    # Data track structure
                    sector_data[0:3] = b'\x00\x00\x00'  # Header bytes
                    sector_data[3] = 0xFB  # Data address mark
                    sector_data[4] = track
                    sector_data[5] = phys_sector
                    sector_data[6] = 0x00  # Flags
                    # Data bytes 7-134 are already zero
                    # Checksums at 135-136
                    sector_data[135:137] = self._calculate_sector_checksums(
                        bytes(sector_data[7:135]), track, phys_sector
                    )

                self.image_data[offset:offset + MITS_PHYSICAL_SECTOR_SIZE] = sector_data

        self.dirty = True
        self._create_physical_format()
        logger.info("Created blank MITS DSK image")

    # --- Private Methods ---

    def _validate_format(self) -> None:
        """
        Validates that the image data is in MITS DSK format.

        Raises:
            ValueError: If format validation fails.
        """
        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        actual_size = len(self.image_data)

        if actual_size < expected_size:
            raise ValueError(f"File too small for MITS DSK: {actual_size} < {expected_size}")

        # Trim any padding
        if actual_size > expected_size:
            logger.warning(f"Trimming {actual_size - expected_size} bytes of padding")
            self.image_data = self.image_data[:expected_size]

    def _create_physical_format(self) -> None:
        """Creates the fixed PhysicalFormat for MITS Altair disks."""
        track_format = TrackFormat(
            track_start=0,
            track_end=MITS_TRACKS - 1,
            head_start=0,
            head_end=0,
            sectors_per_track=MITS_SECTORS_PER_TRACK,
            encoding=MITS_ENCODING,
            rate=MITS_RATE,
            interleave=1,
            bytes_per_sector=MITS_LOGICAL_SECTOR_SIZE,
            id_start=1,
            iam_present=False,
            gap3_bytes=26
        )

        self.physical_format = PhysicalFormat(
            cylinders=MITS_TRACKS,
            heads=1,
            rpm=MITS_RPM,
            heads_inverted=False,
            bytes_per_sector=MITS_LOGICAL_SECTOR_SIZE,
            track_formats=[track_format]
        )

        logger.debug(f"Created MITS DSK physical format: {MITS_TRACKS}C x 1H x {MITS_SECTORS_PER_TRACK}S")

    def _extract_sector_data(self, sector_bytes: bytes, track: int) -> bytes:
        """
        Extracts 128 bytes of data from a 137-byte physical sector.

        Args:
            sector_bytes: The 137-byte physical sector.
            track: The track number.

        Returns:
            The 128-byte data portion.
        """
        if len(sector_bytes) < MITS_PHYSICAL_SECTOR_SIZE:
            logger.warning(f"Short sector on track {track}, padding")
            sector_bytes = sector_bytes.ljust(MITS_PHYSICAL_SECTOR_SIZE, b'\x00')

        if track < 6:
            # System tracks: data at bytes 3-130
            return bytes(sector_bytes[3:131])
        else:
            # Data tracks: data at bytes 7-134
            return bytes(sector_bytes[7:135])

    def _reconstruct_physical_sector(self, old_sector: bytes, new_data: bytes,
                                     track: int, physical_index: int) -> bytes:
        """
        Reconstructs a 137-byte physical sector with new data.

        Args:
            old_sector: The original 137-byte sector (for metadata).
            new_data: The new 128-byte data to insert.
            track: The track number.
            physical_index: The physical sector index (0-31).

        Returns:
            The reconstructed 137-byte sector.
        """
        sector = bytearray(old_sector)

        if track < 6:
            # System track: data at bytes 3-130
            sector[3:131] = new_data
            # Update checksums
            sector[131:137] = self._calculate_sector_checksums(new_data, track, physical_index)
        else:
            # Data track: data at bytes 7-134
            sector[7:135] = new_data
            # Update checksums
            sector[135:137] = self._calculate_sector_checksums(new_data, track, physical_index)

        return bytes(sector)

    def _calculate_sector_checksums(self, data: bytes, track: int, sector: int) -> bytes:
        """
        Calculates MITS-style checksums for sector data.

        Args:
            data: The 128-byte data.
            track: Track number.
            sector: Sector number.

        Returns:
            Checksum bytes (varies by track type).
        """
        # Simplified checksum - XOR of all bytes
        checksum = 0
        for b in data:
            checksum ^= b
        checksum ^= track
        checksum ^= sector

        if track < 6:
            # System tracks: 6 bytes of checksums
            return bytes([checksum, checksum, checksum, checksum, checksum, checksum])
        else:
            # Data tracks: 2 bytes of checksums
            return bytes([checksum, checksum])

    def _validate_sector_checksum(self, sector_bytes: bytes, track: int) -> bool:
        """
        Validates a sector using MITS Altair checksum algorithm.

        Args:
            sector_bytes: The 137-byte sector.
            track: The track number.

        Returns:
            True if sector is valid.
        """
        if len(sector_bytes) < MITS_PHYSICAL_SECTOR_SIZE:
            return False

        try:
            if track < 6:
                # System track validation
                # Format: track|0x80, byte1, byte2, data[128], 0xFF, checksum, padding[4]

                expected_track = track | 0x80
                if sector_bytes[0] != expected_track:
                    return False

                # Stop byte at position 131
                if sector_bytes[131] != 0xFF:
                    return False

                # Checksum calculation
                checksum = 0x01
                for i in range(131):
                    checksum = (checksum + sector_bytes[i]) & 0xFF
                checksum = (checksum + sector_bytes[132]) & 0xFF
                checksum = (checksum - sector_bytes[0]) & 0xFF
                checksum = (checksum - sector_bytes[1]) & 0xFF
                checksum = (checksum - sector_bytes[2]) & 0xFF
                checksum = (checksum - sector_bytes[2]) & 0xFF
                checksum = (checksum - sector_bytes[132]) & 0xFF
                checksum = (checksum - sector_bytes[132]) & 0xFF

                return checksum == 0

            else:
                # Data track validation
                # Format: track|0x80, sector, format_byte, alloc_flag, checksum, meta[2], data[128], 0xFF, 0x00

                expected_track = track | 0x80
                if sector_bytes[0] != expected_track:
                    return False

                # Format byte should be 0x00 or 0x01
                if sector_bytes[2] not in (0x00, 0x01):
                    return False

                # Check end markers
                if sector_bytes[135] != 0xFF:
                    return False
                if sector_bytes[136] != 0x00:
                    return False

                # Checksum calculation (roll_sum_seed1)
                payload = sector_bytes[:136]
                checksum = 0x01
                for byte in payload:
                    checksum = (checksum + byte) & 0xFF

                checksum = (checksum - payload[0]) & 0xFF
                checksum = (checksum - payload[1]) & 0xFF
                chk = payload[4]
                checksum = (checksum - chk) & 0xFF
                checksum = (checksum - chk) & 0xFF

                return checksum == 0

        except Exception:
            return False

    def _formats_match(self, fmt1: PhysicalFormat, fmt2: PhysicalFormat) -> bool:
        """
        Checks if two physical formats are equivalent.

        Args:
            fmt1: First format.
            fmt2: Second format.

        Returns:
            True if formats match.
        """
        return (fmt1.cylinders == fmt2.cylinders and
                fmt1.heads == fmt2.heads and
                fmt1.bytes_per_sector == fmt2.bytes_per_sector and
                fmt1.rpm == fmt2.rpm)
