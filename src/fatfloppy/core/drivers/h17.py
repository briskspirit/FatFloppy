import copy
import datetime
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Optional

from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

H17_MAGIC = b"H17D"
H17_VERSION_2_0 = b"2.0.0"
H17_8BIT_CHECK = 0xFF

H17_SECTORS_PER_TRACK = 10
H17_BYTES_PER_SECTOR = 256
H17_RPM = 300
H17_ENCODING = "FM"
H17_BIT_RATE = 250

# Version 2.0 block IDs (4-byte ASCII)
BLOCK_DISK_FORMAT = b"DskF"
BLOCK_PARAMETERS = b"Parm"
BLOCK_DATE = b"Date"
BLOCK_IMAGER = b"Imgr"
BLOCK_PROGRAM = b"Prog"
BLOCK_PADDING = b"Padd"
BLOCK_H8D_DATA = b"H8DB"
BLOCK_SECTOR_META = b"SecM"
BLOCK_LABEL = b"Labl"
BLOCK_COMMENT = b"Comm"

# Draft block IDs (1-byte numeric)
DRAFT_BLOCK_DISK_FORMAT = 0x00
DRAFT_BLOCK_PARAMETERS = 0x01
DRAFT_BLOCK_COMMENT = 0x02
DRAFT_BLOCK_LABEL = 0x03
DRAFT_BLOCK_DATE = 0x04
DRAFT_BLOCK_IMAGER = 0x05
DRAFT_BLOCK_PROGRAM = 0x06
DRAFT_BLOCK_DATA = 0x10
DRAFT_BLOCK_HOLE = 0x20
DRAFT_BLOCK_RAW_DATA = 0x30

# Draft sub-block IDs
DRAFT_SUBBLOCK_TRACK = 0x11
DRAFT_SUBBLOCK_SECTOR = 0x12

H8D_BLOCK_OFFSET = 256
H8D_BLOCK_HEADER_OFFSET = H8D_BLOCK_OFFSET - 8

SECTOR_METADATA_SIZE = 16

STATUS_MISSING_HEADER_SYNC = 0x01
STATUS_WRONG_TRACK = 0x02
STATUS_INVALID_SECTOR = 0x04
STATUS_INVALID_HEADER_CHECKSUM = 0x08
STATUS_MISSING_DATA_SYNC = 0x10
STATUS_INVALID_DATA_CHECKSUM = 0x20
STATUS_SECTOR_UNREADABLE = 0x40

H17_SIDES_SINGLE = 1
H17_SIDES_DOUBLE = 2
H17_TRACKS_40 = 40
H17_TRACKS_80 = 80

H17_HEADER_SYNC = 0xFD
H17_DATA_SYNC = 0xFD

H17_GAP3_BYTES = 26
H17_INTERLEAVE = 1

DISTRIBUTION_UNKNOWN = 0
DISTRIBUTION_ORIGINAL = 1
DISTRIBUTION_NOT_ORIGINAL = 2
DISTRIBUTION_COPY = 3

SOURCE_UTILITY = 0
SOURCE_EMULATOR = 1
SOURCE_H8_H89 = 2
SOURCE_FC5025 = 3
SOURCE_APPLESAUCE = 4


logger = get_logger("H17ImageDriver")


@dataclass
class H17SectorMetadata:
    """
    Represents the metadata for a single sector.

    Attributes:
        offset_to_data: Offset in file to the 256-byte sector data.
        status: Status flags indicating sector errors.
        header_sync: Should be 0xFD.
        volume: Volume number from header.
        track: Track number from header.
        sector: Sector number from header.
        header_checksum: Header checksum byte.
        data_sync: Should be 0xFD.
        data_checksum: Data checksum byte.
        valid_bytes: Number of valid bytes (0-256).
    """

    offset_to_data: int
    status: int
    header_sync: int
    volume: int
    track: int
    sector: int
    header_checksum: int
    data_sync: int
    data_checksum: int
    valid_bytes: int

    def has_errors(self) -> bool:
        """
        Returns True if sector has any errors.

        Returns:
            True if the sector has errors, False otherwise.
        """
        return self.status != 0 or self.valid_bytes < H17_BYTES_PER_SECTOR

    def is_valid(self) -> bool:
        """
        Returns True if sector has no errors.

        Returns:
            True if the sector is valid, False otherwise.
        """
        return self.status == 0 and self.valid_bytes == H17_BYTES_PER_SECTOR


@dataclass
class H17DiskFormat:
    """
    Represents the DskF block data.

    Attributes:
        sides: Number of sides (1 or 2).
        tracks: Number of tracks (40 or 80).
        read_only: Whether the disk is marked read-only.
    """

    sides: int
    tracks: int
    read_only: bool = False


@dataclass
class H17Parameters:
    """
    Represents the Parm block data.

    Attributes:
        distribution_disk: 0=unknown, 1=original, 2=not original, 3=copy.
        source_of_headers: 0=utility, 1=emulator, 2=H8/H89, 3=FC5025, 4=AppleSauce.
    """

    distribution_disk: int = 0
    source_of_headers: int = 0


@dataclass
class H17Metadata:
    """
    Container for all H17 metadata blocks.

    Attributes:
        date: ISO format date string.
        imager: Name of imaging software.
        program: Program that created the image.
        label: Disk label.
        comment: User comment.
    """

    date: Optional[str] = None
    imager: Optional[str] = None
    program: Optional[str] = None
    label: Optional[str] = None
    comment: Optional[str] = None


class H17ImageDriver(DiskIODriver):
    """
    Disk I/O driver for H17 (Heathkit hard-sectored) disk images.

    This driver provides full support for both draft and H17Disk v2.0.0 formats,
    including reading/writing sector data, preserving sector headers with volume
    numbers, maintaining sector metadata for error tracking, and supporting all
    optional metadata blocks.
    """

    driver_type: ClassVar[str] = "H17"
    driver_file_extensions: ClassVar[list[str]] = [".h17", ".h17disk"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "Heathkit H17 Disk Image Driver"
    driver_priority: ClassVar[int] = 50

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

        self.disk_format: Optional[H17DiskFormat] = None
        self.parameters: Optional[H17Parameters] = None
        self.metadata: H17Metadata = H17Metadata()
        self.sector_metadata: dict[tuple[int, int, int], H17SectorMetadata] = {}

        self.file_data: bytearray = bytearray()
        self.h8d_block_offset: int = 0
        self.file_version: str = "2.0.0"  # Track which version we're reading

        self.sector_cache: dict[tuple[int, int, int], bytes] = {}
        self.modified_sectors: dict[tuple[int, int, int], bytes] = {}

        if self._file_exists():
            try:
                self._load_and_parse()
                self.logger.info(
                    f"Successfully loaded H17 image: {file_path} (version: {self.file_version})"
                )
            except Exception as e:
                self.logger.error(f"Failed to load H17 image {file_path}: {e}")
                raise
        else:
            self.logger.info(
                f"H17 file '{file_path}' not found. Driver initialized for creation."
            )

    @property
    def allows_geometry_override(self) -> bool:
        """
        H17 geometry can be overridden but may cause inconsistencies.

        Returns:
            True if geometry override is allowed.
        """
        return True

    @property
    def has_embedded_geometry(self) -> bool:
        """
        H17 files contain complete geometry and volume information.

        Returns:
            True as H17 files have embedded geometry.
        """
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """
        H17 files cannot be reformatted in place.

        Returns:
            False as in-place formatting is not supported.
        """
        return False

    @property
    def supports_new_image_creation(self) -> bool:
        """
        H17 driver can create new formatted images.

        Returns:
            True as new image creation is supported.
        """
        return True

    def apply_volume_scheme(
        self, scheme: str, hdos_volume: Optional[int] = None
    ) -> None:
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

        self.logger.info(f"Applying {scheme.upper()} volume scheme")

        if not self.disk_format:
            raise ValueError("Disk format not loaded")

        for cylinder in range(self.disk_format.tracks):
            for head in range(self.disk_format.sides):
                volume = 0 if scheme == "cpm" else 0 if cylinder == 0 else hdos_volume

                self.set_track_volumes(cylinder, head, volume)

        self.logger.info(f"Applied {scheme.upper()} volume scheme successfully")

    def flush(self) -> None:
        """
        Writes all modified sectors back to the file.

        This rebuilds the H8D block and sector metadata, then writes the
        entire file back to disk.

        Raises:
            IOError: If the file cannot be written.
        """
        if not self.dirty or not self.physical_format:
            self.logger.debug("Nothing to flush or no format set")
            return

        self.logger.info(f"Flushing H17 image to {self.file_path}")

        try:
            for sector_key, data in self.modified_sectors.items():
                cylinder, head, sector = sector_key
                meta = self.sector_metadata.get(sector_key)
                if meta:
                    offset = meta.offset_to_data
                    self.file_data[offset : offset + H17_BYTES_PER_SECTOR] = data
                else:
                    self.logger.warning(
                        f"No metadata for modified sector C:{cylinder} H:{head} S:{sector}"
                    )

            with Path(self.file_path).open("wb") as f:
                f.write(self.file_data)

            flushed_count = len(self.modified_sectors)
            self.modified_sectors.clear()
            self.dirty = False
            self.logger.info(f"Successfully flushed {flushed_count} sectors")

        except Exception as e:
            self.logger.error(f"Failed to flush H17 image: {e}")
            raise OSError(f"Flush failed: {e}") from e

    def format_h17(
        self,
        sides: int = 1,
        tracks: int = 40,
        scheme: str = "hdos",
        hdos_volume: int = 0,
        label: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> None:
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
        if sides not in [H17_SIDES_SINGLE, H17_SIDES_DOUBLE]:
            raise ValueError(f"Sides must be 1 or 2, got {sides}")
        if tracks not in [H17_TRACKS_40, H17_TRACKS_80]:
            raise ValueError(f"Tracks must be 40 or 80, got {tracks}")

        self.logger.info(
            f"Formatting H17 image: {sides} sides, {tracks} tracks, {scheme} scheme"
        )

        self.disk_format = H17DiskFormat(sides=sides, tracks=tracks, read_only=False)

        self.parameters = H17Parameters(
            distribution_disk=DISTRIBUTION_UNKNOWN, source_of_headers=SOURCE_EMULATOR
        )

        self.metadata = H17Metadata(
            date=datetime.datetime.now().isoformat(),
            program="FatFloppy H17 Driver",
            label=label,
            comment=comment,
        )

        self._build_new_file(sides, tracks, scheme, hdos_volume)
        self._derive_physical_format()

        self.dirty = True
        self.logger.info("H17 format creation complete")

    def get_disk_comment(self) -> Optional[str]:
        """
        Returns the disk comment from metadata.

        Returns:
            The disk comment string, or None if not set.
        """
        return self.metadata.comment

    def get_disk_label(self) -> Optional[str]:
        """
        Returns the disk label from metadata.

        Returns:
            The disk label string, or None if not set.
        """
        return self.metadata.label

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for H17 driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": False,
            "can_derive_format": True,
            "preferred_detection_method": "embedded",
        }

    def get_sector_status(self, cylinder: int, head: int, sector: int) -> Optional[int]:
        """
        Gets the error status flags for a sector.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The logical sector index (0-based, 0-9 for H17).

        Returns:
            The status flags, or None if metadata not available.
        """
        meta = self.sector_metadata.get((cylinder, head, sector))
        return meta.status if meta else None

    def get_sector_volume(self, cylinder: int, head: int, sector: int) -> Optional[int]:
        """
        Gets the volume number for a specific sector from its header.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The logical sector index (0-based, 0-9 for H17).

        Returns:
            The volume number, or None if metadata not available.
        """
        meta = self.sector_metadata.get((cylinder, head, sector))
        return meta.volume if meta else None

    def get_track_volumes(self, cylinder: int, head: int) -> list[int]:
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

    def initialize_new_image(
        self, physical_format: PhysicalFormat, profile: Optional[Any] = None
    ) -> None:
        """
        Initializes a new blank H17 image structure.

        Args:
            physical_format: The physical format for the new image.
            profile: Optional FormatProfile with additional metadata.
        """
        sides = physical_format.heads
        tracks = physical_format.cylinders

        scheme = "hdos"
        hdos_volume = 1
        label = None

        if profile and hasattr(profile, "filesystem_type"):
            scheme = "cpm" if profile.filesystem_type == "CPM" else "hdos"

            if profile.filesystem_config and hasattr(
                profile.filesystem_config, "volume_number"
            ):
                hdos_volume = profile.filesystem_config.volume_number

            if profile.filesystem_config and hasattr(
                profile.filesystem_config, "title"
            ):
                label = profile.filesystem_config.title

        self.format_h17(
            sides=sides,
            tracks=tracks,
            scheme=scheme,
            hdos_volume=hdos_volume,
            label=label,
            comment="Created by FatFloppy",
        )

    def prepare_for_format_application(
        self, format_info: dict
    ) -> tuple[bool, Optional[str]]:
        """
        Validates format compatibility for H17 driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        if not self._file_exists():
            if "physical_format" not in format_info:
                return False, "Physical format required for creating new H17 image"
            return True, None

        warning = (
            "H17 files contain volume information that is tied to their "
            "embedded geometry. Overriding format may cause corruption."
        )
        self.logger.warning(warning)

        return True, warning

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the disk image.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The logical sector index (0-based, 0-9 for H17).

        Returns:
            The 256-byte sector data.

        Raises:
            ValueError: If the physical format is not set.
            IOError: If the sector cannot be read.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        sector_key = (cylinder, head, sector)

        if sector_key in self.modified_sectors:
            self.logger.debug(
                f"Reading modified sector C:{cylinder} H:{head} S:{sector}"
            )
            return self.modified_sectors[sector_key]

        if sector_key in self.sector_cache:
            self.logger.debug(f"Reading cached sector C:{cylinder} H:{head} S:{sector}")
            return self.sector_cache[sector_key]

        meta = self.sector_metadata.get(sector_key)
        if not meta:
            self.logger.warning(
                f"No metadata for sector C:{cylinder} H:{head} S:{sector}, returning zeros"
            )
            return bytes(H17_BYTES_PER_SECTOR)

        try:
            data = self.file_data[
                meta.offset_to_data : meta.offset_to_data + H17_BYTES_PER_SECTOR
            ]

            if len(data) < H17_BYTES_PER_SECTOR:
                self.logger.warning(
                    f"Incomplete sector data for C:{cylinder} H:{head} S:{sector}, padding"
                )
                data = data.ljust(H17_BYTES_PER_SECTOR, b"\x00")

            self.sector_cache[sector_key] = data
            self.logger.debug(
                f"Read sector C:{cylinder} H:{head} S:{sector} from offset {meta.offset_to_data}"
            )
            return data

        except Exception as e:
            self.logger.error(
                f"Error reading sector C:{cylinder} H:{head} S:{sector}: {e}"
            )
            raise OSError(
                f"Failed to read sector C:{cylinder} H:{head} S:{sector}"
            ) from e

    def set_disk_comment(self, comment: str) -> None:
        """
        Sets the disk comment in metadata.

        Args:
            comment: The comment string to set.
        """
        self.metadata.comment = comment
        self.dirty = True

    def set_disk_label(self, label: str) -> None:
        """
        Sets the disk label in metadata.

        Args:
            label: The label string to set.
        """
        self.metadata.label = label
        self.dirty = True

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

        self.physical_format = copy.deepcopy(physical_format)
        self.logger.info(
            f"Physical format set: {physical_format.cylinders}x{physical_format.heads}"
        )

    def set_sector_volume(
        self, cylinder: int, head: int, sector: int, volume: int
    ) -> None:
        """
        Sets the volume number for a specific sector.

        This updates the sector metadata but does not write to disk until flush().

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The logical sector index (0-based, 0-9 for H17).
            volume: The volume number (0-255).

        Raises:
            ValueError: If volume is out of range or metadata not found.
        """
        if not (0 <= volume <= 255):
            raise ValueError(f"Volume must be 0-255, got {volume}")

        sector_key = (cylinder, head, sector)
        meta = self.sector_metadata.get(sector_key)
        if not meta:
            raise ValueError(f"No metadata for sector C:{cylinder} H:{head} S:{sector}")

        meta.volume = volume
        self.dirty = True
        self.logger.debug(
            f"Set volume for C:{cylinder} H:{head} S:{sector} to {volume}"
        )

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
        self.logger.debug(f"Set volume for track C:{cylinder} H:{head} to {volume}")

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        """
        Validates whether an H17 file can be opened.

        Args:
            source: Path to the H17 file.
            **_kwargs: Unused for H17 driver.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not Path(source).exists():
            return False, f"H17 file not found: {source}"

        try:
            with Path(source).open("rb") as f:
                magic = f.read(4)

            if magic != b"H17D":
                return False, "Invalid H17 magic number (expected 'H17D')"

        except Exception as e:
            return False, f"Cannot validate H17 file: {e}"

        return True, None

    def validate_state_for_opening(self) -> tuple[bool, Optional[str]]:
        """
        Validates H17 driver state after opening.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not self.file_path:
            return False, "H17 driver has no file path"

        if self._file_exists() and not self.physical_format:
            return False, "H17 file exists but no physical format derived"

        return True, None

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the in-memory cache.

        Args:
            cylinder: The cylinder (track) number.
            head: The head (side) number.
            sector: The logical sector index (0-based, 0-9 for H17).
            data: The 256-byte sector data to write.

        Raises:
            ValueError: If the physical format is not set or data size is incorrect.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        if len(data) != H17_BYTES_PER_SECTOR:
            raise ValueError(
                f"Data size must be {H17_BYTES_PER_SECTOR} bytes, got {len(data)}"
            )

        sector_key = (cylinder, head, sector)

        self.modified_sectors[sector_key] = bytes(data)
        self.dirty = True

        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]

        self.logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector}")

    def _build_new_file(
        self, sides: int, tracks: int, scheme: str, hdos_volume: int
    ) -> None:
        """
        Builds a complete new H17 file structure in memory.
        Always creates version 2.0.0 format.

        Args:
            sides: Number of sides.
            tracks: Number of tracks.
            scheme: Volume scheme ("cpm" or "hdos").
            hdos_volume: Volume number for HDOS tracks.
        """
        self.file_data = bytearray()

        # Write header for version 2.0.0
        self.file_data.extend(H17_MAGIC)
        self.file_data.extend(H17_VERSION_2_0)
        self.file_data.append(H17_8BIT_CHECK)
        self.file_version = "2.0.0"

        self._write_block_v2(BLOCK_DISK_FORMAT, bytes([sides, tracks, 0]))

        self._write_block_v2(
            BLOCK_PARAMETERS,
            bytes(
                [self.parameters.distribution_disk, self.parameters.source_of_headers]
            ),
        )

        if self.metadata.date:
            self._write_block_v2(BLOCK_DATE, self.metadata.date.encode("utf-8"))
        if self.metadata.program:
            self._write_block_v2(BLOCK_PROGRAM, self.metadata.program.encode("utf-8"))
        if self.metadata.label:
            self._write_block_v2(BLOCK_LABEL, self.metadata.label.encode("utf-8"))
        if self.metadata.comment:
            self._write_block_v2(BLOCK_COMMENT, self.metadata.comment.encode("utf-8"))

        current_offset = len(self.file_data) + 8
        padding_needed = H8D_BLOCK_OFFSET - current_offset
        if padding_needed > 0:
            self._write_block_v2(BLOCK_PADDING, bytes(padding_needed))

        num_sectors = tracks * sides * H17_SECTORS_PER_TRACK
        h8d_data = bytes(num_sectors * H17_BYTES_PER_SECTOR)
        self._write_block_v2(BLOCK_H8D_DATA, h8d_data)
        self.h8d_block_offset = H8D_BLOCK_OFFSET

        secm_data = self._build_sector_metadata(sides, tracks, scheme, hdos_volume)
        self._write_block_v2(BLOCK_SECTOR_META, secm_data)

    def _build_sector_metadata(
        self, sides: int, tracks: int, scheme: str, hdos_volume: int
    ) -> bytes:
        """
        Builds the SecM block data with appropriate volume numbers.

        Args:
            sides: Number of sides.
            tracks: Number of tracks.
            scheme: Volume scheme ("cpm" or "hdos").
            hdos_volume: Volume number for HDOS tracks.

        Returns:
            The sector metadata block bytes.
        """
        metadata = bytearray()
        data_offset = self.h8d_block_offset

        for cylinder in range(tracks):
            for head in range(sides):
                volume = 0 if scheme == "cpm" else 0 if cylinder == 0 else hdos_volume

                for sector in range(H17_SECTORS_PER_TRACK):
                    meta = H17SectorMetadata(
                        offset_to_data=data_offset,
                        status=0,
                        header_sync=H17_HEADER_SYNC,
                        volume=volume,
                        track=cylinder,
                        sector=sector,
                        header_checksum=0,
                        data_sync=H17_DATA_SYNC,
                        data_checksum=0,
                        valid_bytes=H17_BYTES_PER_SECTOR,
                    )

                    self.sector_metadata[(cylinder, head, sector)] = meta

                    metadata.extend(struct.pack(">I", meta.offset_to_data))
                    metadata.append(meta.status)
                    metadata.append(meta.header_sync)
                    metadata.append(meta.volume)
                    metadata.append(meta.track)
                    metadata.append(meta.sector)
                    metadata.append(meta.header_checksum)
                    metadata.append(meta.data_sync)
                    metadata.append(meta.data_checksum)
                    metadata.extend(struct.pack(">H", meta.valid_bytes))
                    metadata.extend(b"\x00\x00")

                    data_offset += H17_BYTES_PER_SECTOR

        return bytes(metadata)

    def _derive_physical_format(self) -> None:
        """
        Creates a PhysicalFormat object from the disk format.

        Raises:
            ValueError: If the disk format is not loaded.
        """
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
            interleave=H17_INTERLEAVE,
            bytes_per_sector=H17_BYTES_PER_SECTOR,
            iam_present=False,
            gap3_bytes=H17_GAP3_BYTES,
        )

        self.physical_format = PhysicalFormat(
            cylinders=self.disk_format.tracks,
            heads=self.disk_format.sides,
            rpm=H17_RPM,
            heads_inverted=False,
            bytes_per_sector=H17_BYTES_PER_SECTOR,
            track_formats=[track_format],
        )

        self.logger.info(
            f"Derived physical format: {self.disk_format.tracks}C x "
            f"{self.disk_format.sides}H x {H17_SECTORS_PER_TRACK}S"
        )

    def _detect_version(self) -> tuple[str, int]:
        """
        Detects whether this is draft or version 2.0 format.

        Returns:
            Tuple of (version_string, blocks_start_offset)
        """
        if len(self.file_data) < 8:
            raise ValueError("File too small to be valid H17 format")

        # Check if byte 7 is 0xFF (version 2.0)
        if self.file_data[7] == H17_8BIT_CHECK:
            # Version 2.0 - ASCII version bytes
            version_bytes = self.file_data[4:7]
            try:
                version_str = version_bytes.decode("ascii")
                return f"2.0 ({version_str})", 8
            except UnicodeDecodeError:
                return "2.0 (unknown)", 8

        # Draft version - no 0xFF at byte 7
        version_bytes = self.file_data[4:7]
        version_str = (
            f"draft ({version_bytes[0]}.{version_bytes[1]}.{version_bytes[2]})"
        )
        return version_str, 7

    def _file_exists(self) -> bool:
        """
        Checks if the file path exists.

        Returns:
            True if the file exists, False otherwise.
        """
        return Path(self.file_path).exists()

    def _load_and_parse(self) -> None:
        """
        Loads and parses an existing H17 file.
        Supports both draft and version 2.0 formats.

        Raises:
            ValueError: If the file is invalid or corrupted.
        """
        with Path(self.file_path).open("rb") as f:
            self.file_data = bytearray(f.read())

        if len(self.file_data) < 8:
            raise ValueError("File too small to be valid H17 format")

        if self.file_data[0:4] != H17_MAGIC:
            raise ValueError(
                f"Invalid magic number, expected {H17_MAGIC}, got {self.file_data[0:4]}"
            )

        # Detect version and get block start offset
        self.file_version, blocks_offset = self._detect_version()
        self.logger.debug(f"Detected H17 version: {self.file_version}")

        # Parse blocks based on format
        if self.file_version.startswith("draft"):
            self._parse_blocks_draft(blocks_offset)
        else:
            self._parse_blocks_v2(blocks_offset)

        self._derive_physical_format()

    def _parse_blocks_draft(self, start_offset: int = 7) -> None:
        """
        Parses blocks in draft format (1-byte ID, 1-byte flags, 4-byte length).

        Args:
            start_offset: Byte offset where blocks begin (7 for draft)
        """
        offset = start_offset

        while offset + 6 <= len(self.file_data):
            block_id = self.file_data[offset]
            flags = self.file_data[offset + 1]
            block_length = struct.unpack(">I", self.file_data[offset + 2 : offset + 6])[
                0
            ]

            block_data = self.file_data[offset + 6 : offset + 6 + block_length]

            self.logger.debug(
                f"Parsing draft block 0x{block_id:02X} at offset {offset}, "
                f"length {block_length}, flags 0x{flags:02X}"
            )

            if block_id == DRAFT_BLOCK_DISK_FORMAT:
                self._parse_disk_format_block(block_data)
            elif block_id == DRAFT_BLOCK_PARAMETERS:
                self._parse_parameters_block(block_data)
            elif block_id == DRAFT_BLOCK_COMMENT:
                self.metadata.comment = block_data.decode("utf-8", errors="ignore")
            elif block_id == DRAFT_BLOCK_LABEL:
                self.metadata.label = block_data.decode("utf-8", errors="ignore")
            elif block_id == DRAFT_BLOCK_DATE:
                self.metadata.date = block_data.decode("utf-8", errors="ignore")
            elif block_id == DRAFT_BLOCK_IMAGER:
                self.metadata.imager = block_data.decode("utf-8", errors="ignore")
            elif block_id == DRAFT_BLOCK_PROGRAM:
                self.metadata.program = block_data.decode("utf-8", errors="ignore")
            elif block_id == DRAFT_BLOCK_DATA:
                # Draft data block with nested track/sector sub-blocks
                self._parse_draft_data_block(block_data, offset + 6)
            elif block_id == DRAFT_BLOCK_HOLE:
                self.logger.debug("Skipping draft Hole block")
            elif block_id == DRAFT_BLOCK_RAW_DATA:
                self.logger.debug("Skipping draft Raw Data block")
            else:
                self.logger.warning(f"Unknown draft block ID: 0x{block_id:02X}")

            offset += 6 + block_length

    def _parse_blocks_v2(self, start_offset: int = 8) -> None:
        """
        Parses blocks in version 2.0 format (4-byte ASCII ID, 4-byte length).

        Args:
            start_offset: Byte offset where blocks begin (8 for v2.0)
        """
        offset = start_offset

        while offset + 8 <= len(self.file_data):
            block_id = self.file_data[offset : offset + 4]
            block_length = struct.unpack(">I", self.file_data[offset + 4 : offset + 8])[
                0
            ]

            block_data = self.file_data[offset + 8 : offset + 8 + block_length]

            self.logger.debug(
                f"Parsing v2.0 block {block_id} at offset {offset}, length {block_length}"
            )

            if block_id == BLOCK_DISK_FORMAT:
                self._parse_disk_format_block(block_data)
            elif block_id == BLOCK_PARAMETERS:
                self._parse_parameters_block(block_data)
            elif block_id == BLOCK_DATE:
                self.metadata.date = block_data.decode("utf-8", errors="ignore")
            elif block_id == BLOCK_IMAGER:
                self.metadata.imager = block_data.decode("utf-8", errors="ignore")
            elif block_id == BLOCK_PROGRAM:
                self.metadata.program = block_data.decode("utf-8", errors="ignore")
            elif block_id == BLOCK_LABEL:
                self.metadata.label = block_data.decode("utf-8", errors="ignore")
            elif block_id == BLOCK_COMMENT:
                self.metadata.comment = block_data.decode("utf-8", errors="ignore")
            elif block_id == BLOCK_H8D_DATA:
                self.h8d_block_offset = offset + 8
                self.logger.info(
                    f"H8D block found at offset {self.h8d_block_offset}, length {block_length}"
                )
            elif block_id == BLOCK_SECTOR_META:
                self._parse_sector_metadata_block(block_data)
            elif block_id == BLOCK_PADDING:
                pass
            else:
                self.logger.warning(f"Unknown v2.0 block ID: {block_id}")

            offset += 8 + block_length

    def _parse_disk_format_block(self, data: bytes) -> None:
        """
        Parses the DskF block (same for both draft and v2.0).

        Args:
            data: The block data bytes.

        Raises:
            ValueError: If the block is invalid.
        """
        if len(data) < 2:
            raise ValueError("DskF block too short")

        sides = data[0]
        tracks = data[1]
        read_only = data[2] if len(data) > 2 else 0

        if sides not in [H17_SIDES_SINGLE, H17_SIDES_DOUBLE]:
            raise ValueError(f"Invalid sides value: {sides}")
        if tracks not in [H17_TRACKS_40, H17_TRACKS_80]:
            raise ValueError(f"Invalid tracks value: {tracks}")

        self.disk_format = H17DiskFormat(
            sides=sides, tracks=tracks, read_only=bool(read_only)
        )

        self.logger.info(
            f"Disk format: {sides} sides, {tracks} tracks, R/O={read_only}"
        )

    def _parse_draft_data_block(self, data: bytes, data_offset: int) -> None:
        """
        Parses the draft Data Block with nested track and sector sub-blocks.

        Draft format structure:
        - Track Sub-block (0x11): ID, head, track, length[2]
        - Sector Sub-block (0x12): ID, sector#, error_status[4], length[2], data[variable]

        The sector data contains raw physical sector with variable-length gaps and timing bytes.
        We must dynamically search for sync bytes (0xFD) rather than using fixed offsets.

        IMPORTANT: The draft sub-block sector# field represents the physical position in the file,
        while the actual sector header contains the LOGICAL sector number. We must use the
        logical sector number from the header, not the sub-block position, to support
        interleaved sector layouts.

        Args:
            data: The block data bytes.
            data_offset: Absolute file offset where this block's data starts.

        Raises:
            ValueError: If the block structure is invalid.
        """
        if not self.disk_format:
            raise ValueError("DskF block must be parsed before Data block")

        offset = 0
        sector_count = 0

        while offset < len(data):
            if offset + 5 > len(data):
                break

            # Check for track sub-block
            if data[offset] == DRAFT_SUBBLOCK_TRACK:
                head = data[offset + 1]
                track = data[offset + 2]
                track_length = struct.unpack(">H", data[offset + 3 : offset + 5])[0]

                self.logger.debug(
                    f"Track sub-block: T={track} H={head} length={track_length}"
                )

                offset += 5
                track_offset = 0

                # Parse sectors in this track
                while track_offset < track_length and offset < len(data):
                    if offset + 8 > len(data):
                        self.logger.warning(
                            f"Incomplete sector header at offset {offset}"
                        )
                        break

                    # Check for sector sub-block
                    if data[offset] != DRAFT_SUBBLOCK_SECTOR:
                        self.logger.warning(
                            f"Expected sector sub-block 0x12, got 0x{data[offset]:02X} "
                            f"at offset {offset}"
                        )
                        break

                    # NOTE: This is the physical position in the file, NOT the logical sector number
                    file_position = data[offset + 1]
                    error_status = struct.unpack(">I", data[offset + 2 : offset + 6])[0]

                    # Calculate sector data size from track length
                    bytes_per_sector = track_length // H17_SECTORS_PER_TRACK
                    sector_length = bytes_per_sector - 6  # Subtract header bytes

                    self.logger.debug(
                        f"Sector sub-block: file_position={file_position} status=0x{error_status:08X} "
                        f"data_length={sector_length} at offset {offset}"
                    )

                    offset += 6
                    track_offset += 6

                    # Read sector data (variable length in draft format)
                    if offset + sector_length > len(data):
                        self.logger.error(
                            f"Sector data extends beyond block boundary at offset {offset}"
                        )
                        break

                    sector_data = data[offset : offset + sector_length]

                    # Calculate absolute file offset for this sector's raw data
                    sector_data_file_offset = data_offset + offset

                    # The sector data in draft format contains the raw physical sector
                    # We need to dynamically find the header sync and data sync bytes
                    # Format: [gap bytes] FD volume track sector checksum [gap bytes] FD [256 data bytes] checksum [gap]

                    # Search for header sync (first 0xFD)
                    header_sync_pos = sector_data.find(b"\xfd")

                    if header_sync_pos >= 0 and header_sync_pos + 5 < sector_length:
                        # Extract header information
                        header_sync = sector_data[header_sync_pos]
                        volume = sector_data[header_sync_pos + 1]
                        track_in_header = sector_data[header_sync_pos + 2]
                        # THIS IS THE FIX: Use the logical sector number from the header
                        logical_sector_num = sector_data[header_sync_pos + 3]
                        header_checksum = sector_data[header_sync_pos + 4]

                        # Search for data sync (second 0xFD) starting after header
                        data_sync_pos = sector_data.find(b"\xfd", header_sync_pos + 5)

                        if (
                            data_sync_pos >= 0
                            and data_sync_pos + 1 + H17_BYTES_PER_SECTOR
                            <= sector_length
                        ):
                            data_sync = sector_data[data_sync_pos]
                            user_data_offset = data_sync_pos + 1

                            # Calculate absolute file offset where user data starts
                            user_data_file_offset = (
                                sector_data_file_offset + user_data_offset
                            )

                            data_checksum = (
                                sector_data[user_data_offset + H17_BYTES_PER_SECTOR]
                                if user_data_offset + H17_BYTES_PER_SECTOR
                                < sector_length
                                else 0
                            )

                            if track == 0 and (
                                logical_sector_num == 9 or logical_sector_num <= 2
                            ):
                                # Debug first few sectors and label sector
                                self.logger.debug(
                                    f"T:{track} H:{head} S:{logical_sector_num} (file_pos:{file_position}) "
                                    f"header_sync@{header_sync_pos}, data_sync@{data_sync_pos}, "
                                    f"user_data@{user_data_offset}, file_offset={user_data_file_offset}, vol={volume}"
                                )
                        else:
                            # No data sync found - malformed sector
                            self.logger.warning(
                                f"No data sync found for T:{track} H:{head} S:{logical_sector_num} "
                                f"(file_pos:{file_position}), header_sync@{header_sync_pos} - sector will be read-only"
                            )
                            user_data_file_offset = 0
                            data_sync = H17_DATA_SYNC
                            data_checksum = 0
                    else:
                        # No header sync found - severely malformed sector
                        self.logger.warning(
                            f"No header sync found for file_position {file_position} on T:{track} H:{head} "
                            f"- sector will be read-only"
                        )
                        user_data_file_offset = 0
                        header_sync = H17_HEADER_SYNC
                        volume = 0
                        track_in_header = track
                        logical_sector_num = file_position  # Fallback to file position
                        header_checksum = 0
                        data_sync = H17_DATA_SYNC
                        data_checksum = 0

                    # Create metadata pointing to the actual location in the draft format
                    meta = H17SectorMetadata(
                        offset_to_data=user_data_file_offset,
                        status=error_status,
                        header_sync=header_sync,
                        volume=volume,
                        track=track_in_header,
                        sector=logical_sector_num,  # FIXED: Use logical sector from header
                        header_checksum=header_checksum,
                        data_sync=data_sync,
                        data_checksum=data_checksum,
                        valid_bytes=H17_BYTES_PER_SECTOR,
                    )

                    # KEY FIX: Use the logical sector number from the header, not the file position
                    # Draft format: sector numbers are 0-9 in the headers
                    # Internal storage: physical_sector is 0-9
                    # API: sectors are 1-10
                    physical_sector = logical_sector_num

                    self.sector_metadata[(track, head, physical_sector)] = meta

                    self.logger.debug(
                        f"Parsed sector T:{track} H:{head} S:{logical_sector_num} "
                        f"(file_pos {file_position}) volume={volume} status=0x{error_status:08X} "
                        f"at file offset {user_data_file_offset}"
                    )

                    offset += sector_length
                    track_offset += sector_length
                    sector_count += 1

            else:
                self.logger.warning(
                    f"Expected track sub-block 0x11, got 0x{data[offset]:02X} "
                    f"at offset {offset}"
                )
                break

        # For draft format, data lives within Track/Sector sub-blocks at various offsets
        # We don't have a separate H8DB block, so h8d_block_offset is not used
        self.h8d_block_offset = 0
        self.logger.info(
            f"Parsed draft data block: {sector_count} sectors, "
            f"data preserved in original draft format structure"
        )

    def _parse_parameters_block(self, data: bytes) -> None:
        """
        Parses the Parm block (same for both draft and v2.0).

        Args:
            data: The block data bytes.
        """
        if len(data) < 2:
            return

        self.parameters = H17Parameters(
            distribution_disk=data[0] if len(data) > 0 else 0,
            source_of_headers=data[1] if len(data) > 1 else 0,
        )

        self.logger.debug(
            f"Parameters: dist={self.parameters.distribution_disk}, "
            f"source={self.parameters.source_of_headers}"
        )

    def _parse_sector_metadata_block(self, data: bytes) -> None:
        """
        Parses the SecM block and builds sector metadata map (v2.0 only).

        Args:
            data: The block data bytes.

        Raises:
            ValueError: If the DskF block has not been parsed yet.
        """
        if not self.disk_format:
            raise ValueError("DskF block must be parsed before SecM block")

        num_sectors = (
            self.disk_format.tracks * self.disk_format.sides * H17_SECTORS_PER_TRACK
        )
        expected_size = num_sectors * SECTOR_METADATA_SIZE

        if len(data) < expected_size:
            self.logger.warning(
                f"SecM block size mismatch: expected {expected_size}, got {len(data)}"
            )

        offset = 0
        sector_index = 0

        for cylinder in range(self.disk_format.tracks):
            for head in range(self.disk_format.sides):
                for sector in range(H17_SECTORS_PER_TRACK):
                    if offset + SECTOR_METADATA_SIZE > len(data):
                        self.logger.error(
                            f"Unexpected end of SecM block at sector {sector_index}"
                        )
                        return

                    meta_bytes = data[offset : offset + SECTOR_METADATA_SIZE]
                    meta = self._parse_single_sector_metadata(meta_bytes)

                    self.sector_metadata[(cylinder, head, sector)] = meta

                    offset += SECTOR_METADATA_SIZE
                    sector_index += 1

        self.logger.info(f"Parsed metadata for {sector_index} sectors")

    def _parse_single_sector_metadata(self, data: bytes) -> H17SectorMetadata:
        """
        Parses a single 16-byte sector metadata entry.

        Args:
            data: The 16-byte metadata entry.

        Returns:
            An H17SectorMetadata object.
        """
        return H17SectorMetadata(
            offset_to_data=struct.unpack(">I", data[0:4])[0],
            status=data[4],
            header_sync=data[5],
            volume=data[6],
            track=data[7],
            sector=data[8],
            header_checksum=data[9],
            data_sync=data[10],
            data_checksum=data[11],
            valid_bytes=struct.unpack(">H", data[12:14])[0],
        )

    def _write_block_v2(self, block_id: bytes, data: bytes) -> None:
        """
        Writes a block to file_data in version 2.0 format.

        Args:
            block_id: The 4-byte ASCII block identifier.
            data: The block data bytes.
        """
        self.file_data.extend(block_id)
        self.file_data.extend(struct.pack(">I", len(data)))
        self.file_data.extend(data)
