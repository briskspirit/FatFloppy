import copy
from pathlib import Path
from typing import Any, ClassVar, Optional

from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.atomic_io import atomic_write
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

MITS_TRACKS = 77
MITS_SECTORS_PER_TRACK = 32
MITS_PHYSICAL_SECTOR_SIZE = 137
MITS_LOGICAL_SECTOR_SIZE = 128
MITS_ENCODING = "FM"
MITS_RATE = 250
MITS_RPM = 360

MITS_SYSTEM_TRACK_COUNT = 6
MITS_SYSTEM_TRACK_DATA_START = 3
MITS_SYSTEM_TRACK_DATA_END = 131
MITS_SYSTEM_TRACK_STOP_BYTE_POS = 131
MITS_SYSTEM_TRACK_CHECKSUM_START = 131
MITS_SYSTEM_TRACK_CHECKSUM_END = 137
MITS_SYSTEM_TRACK_CHECKSUM_COUNT = 6

MITS_DATA_TRACK_DATA_START = 7
MITS_DATA_TRACK_DATA_END = 135
MITS_DATA_TRACK_CHECKSUM_START = 135
MITS_DATA_TRACK_CHECKSUM_END = 137
MITS_DATA_TRACK_CHECKSUM_COUNT = 2

MITS_STOP_BYTE = 0xFF
MITS_END_MARKER = 0x00
MITS_DATA_ADDRESS_MARK = 0xFB
MITS_TRACK_FLAG_MASK = 0x80
MITS_CHECKSUM_SEED = 0x01

MITS_VALIDATION_TRACKS = [0, 1, 5, 6, 20, 40]
MITS_VALIDATION_SECTORS = [0, 10, 20]
MITS_VALIDATION_MIN_COUNT = 8

MITS_GAP3_BYTES = 26


logger = get_logger("MITSDSKDriver")


class MITSDSKDriver(DiskIODriver):
    """
    Disk I/O driver for MITS Altair .DSK format.

    This driver handles the physical sector layout of MITS Altair disks,
    extracting 128-byte data from 137-byte physical sectors. Sectors are
    exposed in physical disk order - no reordering is performed.
    """

    driver_type: ClassVar[str] = "MITS_DSK"
    driver_file_extensions: ClassVar[list[str]] = [".dsk"]
    driver_category: ClassVar[str] = "raw"
    driver_description: ClassVar[str] = "MITS Altair DSK format driver"
    driver_priority: ClassVar[int] = 100

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
        self.logger = get_logger(self.__class__.__name__)
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.dirty: bool = False
        self.image_data: bytearray
        self.uses_physical_heads: bool = False

        self.sector_cache: dict[tuple[int, int, int], bytes] = {}
        self.modified_sectors: dict[tuple[int, int, int], bytes] = {}

        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
            self._validate_format()
            self.logger.debug(
                f"Initialized MITS DSK driver with provided data of size {len(self.image_data)}"
            )
        else:
            if not self.file_path:
                self.logger.error(
                    "MITS DSK driver initialized without file_path and no image_data."
                )
                raise ValueError(
                    "File path must be provided for MITS DSK driver if image_data is not given."
                )

            if not Path(self.file_path).exists():
                self.logger.info(
                    f"File {self.file_path} does not exist. Initializing empty for new image creation."
                )
                expected_size = (
                    MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
                )
                self.image_data = bytearray(expected_size)
                self.dirty = True
                self.logger.debug(
                    f"Initialized empty MITS DSK driver for new image creation, size {len(self.image_data)}"
                )
            else:
                try:
                    with Path(self.file_path).open("rb") as f:
                        self.image_data = bytearray(f.read())

                    self._validate_format()
                    self.logger.info(
                        f"Loaded MITS DSK file {self.file_path}, size {len(self.image_data)}"
                    )
                except FileNotFoundError:
                    self.logger.error(f"MITS DSK file not found: {self.file_path}")
                    raise
                except ValueError as ve:
                    self.logger.error(
                        f"Invalid MITS DSK format in {self.file_path}: {ve}"
                    )
                    raise
                except Exception as e:
                    self.logger.error(
                        f"Failed to read MITS DSK file {self.file_path}: {e}"
                    )
                    if isinstance(e, OSError):
                        raise
                    raise OSError(
                        f"Failed to read MITS DSK file {self.file_path}"
                    ) from e

    @property
    def allows_geometry_override(self) -> bool:
        """
        MITS DSK requires external geometry specification.

        Returns:
            True as geometry override is required.
        """
        return True

    @property
    def has_embedded_geometry(self) -> bool:
        """
        MITS DSK has fixed structure but no self-describing geometry.

        Returns:
            False as geometry is not embedded.
        """
        return False

    @property
    def supports_in_place_formatting(self) -> bool:
        """
        MITS DSK files can be formatted by overwriting content.

        Returns:
            True as in-place formatting is supported.
        """
        return True

    @property
    def supports_new_image_creation(self) -> bool:
        """
        MITS DSK driver can create new blank images.

        Returns:
            True as new image creation is supported.
        """
        return True

    def flush(self) -> None:
        """
        Writes the in-memory image data back to the file.

        Raises:
            IOError: If the file cannot be written.
        """
        if not self.dirty:
            self.logger.debug("No changes to flush")
            return

        try:
            for sector_key, data in self.modified_sectors.items():
                cylinder, head, sector = sector_key

                offset = (
                    cylinder * MITS_SECTORS_PER_TRACK + sector
                ) * MITS_PHYSICAL_SECTOR_SIZE

                old_sector = self.image_data[
                    offset : offset + MITS_PHYSICAL_SECTOR_SIZE
                ]

                new_sector = self._reconstruct_physical_sector(
                    old_sector, data, cylinder, sector
                )

                self.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE] = (
                    new_sector
                )

            atomic_write(self.file_path, bytes(self.image_data))

            self.logger.info(
                f"Flushed {len(self.modified_sectors)} sectors to {self.file_path}"
            )
            self.modified_sectors.clear()
            self.dirty = False

        except Exception as e:
            self.logger.error(
                f"Failed to flush MITS DSK image to {self.file_path}: {e}"
            )
            raise OSError(f"Flush failed: {e}") from e

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for MITS DSK driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": True,
            "can_derive_format": True,
            "preferred_detection_method": "auto",
        }

    def initialize_new_image(
        self, _physical_format: PhysicalFormat, _profile: Optional[Any] = None
    ) -> None:
        """
        Creates a new blank MITS DSK image.

        Args:
            _physical_format: Ignored (MITS format is fixed).
            _profile: Ignored.
        """
        self.logger.info("Creating new blank MITS DSK image")

        total_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        self.image_data = bytearray(total_size)

        for track in range(MITS_TRACKS):
            for sector in range(MITS_SECTORS_PER_TRACK):
                offset = (
                    track * MITS_SECTORS_PER_TRACK + sector
                ) * MITS_PHYSICAL_SECTOR_SIZE

                sector_data = bytearray(MITS_PHYSICAL_SECTOR_SIZE)

                if track < MITS_SYSTEM_TRACK_COUNT:
                    sector_data[0] = track | MITS_TRACK_FLAG_MASK
                    sector_data[1] = track
                    sector_data[2] = sector
                    sector_data[
                        MITS_SYSTEM_TRACK_CHECKSUM_START:MITS_SYSTEM_TRACK_CHECKSUM_END
                    ] = self._calculate_sector_checksums(sector_data, track)
                else:
                    sector_data[0] = track | MITS_TRACK_FLAG_MASK
                    sector_data[1] = 0x00
                    sector_data[2] = 0x00
                    sector_data[3] = MITS_DATA_ADDRESS_MARK
                    sector_data[4] = track
                    sector_data[5] = sector
                    sector_data[6] = 0x00
                    sector_data[
                        MITS_DATA_TRACK_CHECKSUM_START:MITS_DATA_TRACK_CHECKSUM_END
                    ] = self._calculate_sector_checksums(sector_data, track)

                self.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE] = (
                    sector_data
                )

        self.dirty = True
        self._create_physical_format()
        self.logger.info("Created blank MITS DSK image")

    def prepare_for_format_application(
        self, _format_info: dict
    ) -> tuple[bool, Optional[str]]:
        """
        MITS DSK allows temporary format override for filesystem validation.

        Args:
            _format_info: Dictionary containing format parameters, unused.

        Returns:
            Tuple of (is_ready, error_message).
        """
        return True, None

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector (128 bytes) from the disk image in physical order.

        No sector reordering is performed - sectors are read from their physical
        positions on disk. The filesystem is responsible for any sector translation.

        Args:
            cylinder: The cylinder (track) number.
            head: The head number (always 0 for MITS).
            sector: The logical sector index (0-based, 0-31 for MITS).

        Returns:
            The 128-byte sector data.

        Raises:
            ValueError: If the physical format has not been set.
            IOError: If the sector is out of bounds.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        if head != 0:
            raise OSError(f"MITS DSK only supports head 0, got head {head}")

        if not (0 <= sector < MITS_SECTORS_PER_TRACK):
            raise OSError(
                f"Invalid sector index: {sector} (must be 0-{MITS_SECTORS_PER_TRACK - 1})"
            )

        sector_key = (cylinder, head, sector)

        if sector_key in self.modified_sectors:
            self.logger.debug(
                f"Reading modified sector C:{cylinder} H:{head} S:{sector}"
            )
            return self.modified_sectors[sector_key]

        if sector_key in self.sector_cache:
            return self.sector_cache[sector_key]

        offset = (
            cylinder * MITS_SECTORS_PER_TRACK + sector
        ) * MITS_PHYSICAL_SECTOR_SIZE

        if offset + MITS_PHYSICAL_SECTOR_SIZE > len(self.image_data):
            raise OSError(f"Sector C:{cylinder} H:{head} S:{sector} out of bounds")

        sector_bytes = self.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE]
        data = self._extract_sector_data(sector_bytes, cylinder)

        self.sector_cache[sector_key] = data
        self.logger.debug(
            f"Read sector C:{cylinder} H:{head} S:{sector} from physical position {sector}"
        )
        return data

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

        if self.physical_format and not self._formats_match(
            physical_format, self.physical_format
        ):
            self.logger.warning(
                f"Overriding MITS DSK native format "
                f"({self.physical_format.cylinders}C x {self.physical_format.heads}H x "
                f"{self.physical_format.bytes_per_sector}B) with "
                f"({physical_format.cylinders}C x {physical_format.heads}H x "
                f"{physical_format.bytes_per_sector}B). "
                f"This is allowed for filesystem validation but may cause I/O errors."
            )

        self.physical_format = copy.deepcopy(physical_format)
        self.logger.debug(
            f"Physical format set: {physical_format.cylinders}C x {physical_format.heads}H"
        )

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        """
        Validates whether a file is a valid MITS Altair .DSK format.

        Uses checksum validation on multiple sectors to detect format.

        Args:
            source: Path to the .DSK file.
            **_kwargs: Unused for MITS DSK driver.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not Path(source).exists():
            return False, f"File not found: {source}"

        try:
            with Path(source).open("rb") as f:
                data = f.read()

            expected_size = (
                MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
            )
            if len(data) < expected_size:
                return (
                    False,
                    f"File too small for MITS DSK format: {len(data)} < {expected_size}",
                )

            validation_count = 0
            for track in MITS_VALIDATION_TRACKS:
                for phys_sector in MITS_VALIDATION_SECTORS:
                    offset = (
                        track * MITS_SECTORS_PER_TRACK + phys_sector
                    ) * MITS_PHYSICAL_SECTOR_SIZE
                    if offset + MITS_PHYSICAL_SECTOR_SIZE <= len(data):
                        sector_bytes = data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE]
                        if self._validate_sector_checksum(sector_bytes, track):
                            validation_count += 1

            if validation_count < MITS_VALIDATION_MIN_COUNT:
                return (
                    False,
                    f"MITS DSK checksum validation failed: only {validation_count} valid sectors found",
                )

            self.logger.info(
                f"MITS DSK format validated with {validation_count} valid checksums"
            )
            return True, None

        except Exception as e:
            return False, f"Error validating MITS DSK format: {e}"

    def validate_state_for_opening(self) -> tuple[bool, Optional[str]]:
        """
        Validates MITS DSK driver state after opening.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not hasattr(self, "image_data") or not self.image_data:
            return False, "MITS DSK driver has no image data"

        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        if len(self.image_data) < expected_size:
            return (
                False,
                f"MITS DSK file too small: {len(self.image_data)} < {expected_size}",
            )

        return True, None

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the in-memory image data in physical order.

        Args:
            cylinder: The cylinder number.
            head: The head number (must be 0).
            sector: The logical sector index (0-based, 0-31 for MITS).
            data: The 128-byte sector data to write.

        Raises:
            ValueError: If parameters are invalid or data size is incorrect.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        if head != 0:
            raise ValueError(f"MITS DSK only supports head 0, got head {head}")

        if not (0 <= cylinder < MITS_TRACKS):
            # Without this, flush() slice-assigns past the end of the image,
            # silently appending a malformed blob (audit mits_dsk.py:488).
            raise ValueError(
                f"Invalid cylinder: {cylinder} (must be 0-{MITS_TRACKS - 1})"
            )

        if not (0 <= sector < MITS_SECTORS_PER_TRACK):
            raise ValueError(
                f"Invalid sector index: {sector} (must be 0-{MITS_SECTORS_PER_TRACK - 1})"
            )

        if len(data) != MITS_LOGICAL_SECTOR_SIZE:
            raise ValueError(
                f"Data size must be {MITS_LOGICAL_SECTOR_SIZE} bytes, got {len(data)}"
            )

        sector_key = (cylinder, head, sector)
        self.modified_sectors[sector_key] = bytes(data)
        self.dirty = True

        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]

        self.logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector}")

    def _calculate_sector_checksums(self, sector_bytes: bytearray, track: int) -> bytes:
        """
        Calculates MITS-style checksums from the full 137-byte sector.

        Uses the same additive algorithm as _validate_sector_checksum so that
        sectors written by FatFloppy pass its own validation on re-open.

        Args:
            sector_bytes: The full 137-byte sector with data already inserted
                          but checksum area not yet set.
            track: Track number.

        Returns:
            Checksum bytes to write at the checksum area.
        """
        if track < MITS_SYSTEM_TRACK_COUNT:
            # System tracks: checksum byte at [132] makes the validate sum zero.
            # Validate formula: SEED + sum([0:131]) - [0] - [1] - 2*[2] - [132] == 0
            # So: [132] = (SEED + sum([0:131]) - [0] - [1] - 2*[2]) & 0xFF
            partial = MITS_CHECKSUM_SEED
            for i in range(MITS_SYSTEM_TRACK_DATA_END):  # 0-130
                partial = (partial + sector_bytes[i]) & 0xFF
            partial = (partial - sector_bytes[0]) & 0xFF
            partial = (partial - sector_bytes[1]) & 0xFF
            partial = (partial - sector_bytes[2]) & 0xFF
            partial = (partial - sector_bytes[2]) & 0xFF
            checksum = partial & 0xFF
            # [131] = stop byte 0xFF, [132-136] = checksum copies
            return bytes(
                [MITS_STOP_BYTE] + [checksum] * (MITS_SYSTEM_TRACK_CHECKSUM_COUNT - 1)
            )
        else:
            # Data tracks: byte [4] is the additive checksum byte. The validator
            # accepts the sector iff (over payload = bytes[0:136], where byte
            # [135] is the 0xFF stop byte):
            #   SEED + sum(payload) - payload[0] - payload[1] - 2*payload[4] == 0
            # Solving for payload[4]:
            #   payload[4] = SEED + sum(bytes[0:136] except [4]) - byte[0] - byte[1]
            # Compute over the final sector (byte[135] = stop), excluding byte [4],
            # then set it in place on the mutable buffer (audit mits_dsk.py:538/541).
            total = MITS_CHECKSUM_SEED
            for i in range(MITS_DATA_TRACK_DATA_END):  # 0..134
                if i == 4:
                    continue
                total = (total + sector_bytes[i]) & 0xFF
            total = (total + MITS_STOP_BYTE) & 0xFF  # byte [135] in the final sector
            total = (total - sector_bytes[0]) & 0xFF
            total = (total - sector_bytes[1]) & 0xFF
            sector_bytes[4] = total & 0xFF
            # [135]=stop byte, [136]=end marker.
            return bytes([MITS_STOP_BYTE, MITS_END_MARKER])

    def _create_physical_format(self) -> None:
        """
        Creates the fixed PhysicalFormat for MITS Altair disks.
        """
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
            iam_present=False,
            gap3_bytes=MITS_GAP3_BYTES,
        )

        self.physical_format = PhysicalFormat(
            cylinders=MITS_TRACKS,
            heads=1,
            rpm=MITS_RPM,
            heads_inverted=False,
            bytes_per_sector=MITS_LOGICAL_SECTOR_SIZE,
            track_formats=[track_format],
        )

        self.logger.debug(
            f"Created MITS DSK physical format: {MITS_TRACKS}C x 1H x {MITS_SECTORS_PER_TRACK}S"
        )

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
            self.logger.warning(f"Short sector on track {track}, padding")
            sector_bytes = sector_bytes.ljust(MITS_PHYSICAL_SECTOR_SIZE, b"\x00")

        if track < MITS_SYSTEM_TRACK_COUNT:
            return bytes(
                sector_bytes[MITS_SYSTEM_TRACK_DATA_START:MITS_SYSTEM_TRACK_DATA_END]
            )
        else:
            return bytes(
                sector_bytes[MITS_DATA_TRACK_DATA_START:MITS_DATA_TRACK_DATA_END]
            )

    def _formats_match(self, fmt1: PhysicalFormat, fmt2: PhysicalFormat) -> bool:
        """
        Checks if two physical formats are equivalent.

        Args:
            fmt1: First format.
            fmt2: Second format.

        Returns:
            True if formats match.
        """
        return (
            fmt1.cylinders == fmt2.cylinders
            and fmt1.heads == fmt2.heads
            and fmt1.bytes_per_sector == fmt2.bytes_per_sector
            and fmt1.rpm == fmt2.rpm
        )

    def _reconstruct_physical_sector(
        self, old_sector: bytes, new_data: bytes, track: int, _sector: int
    ) -> bytes:
        """
        Reconstructs a 137-byte physical sector with new data.

        Args:
            old_sector: The original 137-byte sector (for metadata).
            new_data: The new 128-byte data to insert.
            track: The track number.
            sector: The sector index (0-31).

        Returns:
            The reconstructed 137-byte sector.
        """
        sector_data = bytearray(old_sector)

        if track < MITS_SYSTEM_TRACK_COUNT:
            sector_data[MITS_SYSTEM_TRACK_DATA_START:MITS_SYSTEM_TRACK_DATA_END] = (
                new_data
            )
            sector_data[
                MITS_SYSTEM_TRACK_CHECKSUM_START:MITS_SYSTEM_TRACK_CHECKSUM_END
            ] = self._calculate_sector_checksums(sector_data, track)
        else:
            sector_data[MITS_DATA_TRACK_DATA_START:MITS_DATA_TRACK_DATA_END] = new_data
            sector_data[MITS_DATA_TRACK_CHECKSUM_START:MITS_DATA_TRACK_CHECKSUM_END] = (
                self._calculate_sector_checksums(sector_data, track)
            )

        return bytes(sector_data)

    def _validate_format(self) -> None:
        """
        Validates that the image data is in MITS DSK format.

        Raises:
            ValueError: If format validation fails.
        """
        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        actual_size = len(self.image_data)

        if actual_size < expected_size:
            raise ValueError(
                f"File too small for MITS DSK: {actual_size} < {expected_size}"
            )

        if actual_size > expected_size:
            self.logger.warning(
                f"Trimming {actual_size - expected_size} bytes of padding"
            )
            self.image_data = self.image_data[:expected_size]

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
            if track < MITS_SYSTEM_TRACK_COUNT:
                expected_track = track | MITS_TRACK_FLAG_MASK
                if sector_bytes[0] != expected_track:
                    return False

                if sector_bytes[MITS_SYSTEM_TRACK_STOP_BYTE_POS] != MITS_STOP_BYTE:
                    return False

                checksum = MITS_CHECKSUM_SEED
                for i in range(MITS_SYSTEM_TRACK_DATA_END):
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
                expected_track = track | MITS_TRACK_FLAG_MASK
                if sector_bytes[0] != expected_track:
                    return False

                if sector_bytes[2] not in (0x00, 0x01):
                    return False

                if sector_bytes[MITS_DATA_TRACK_CHECKSUM_START] != MITS_STOP_BYTE:
                    return False
                if sector_bytes[136] != MITS_END_MARKER:
                    return False

                payload = sector_bytes[:136]
                checksum = MITS_CHECKSUM_SEED
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
