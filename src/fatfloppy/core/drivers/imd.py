import copy
import datetime
import re
import struct
from pathlib import Path
from typing import Any, ClassVar, Optional

from ..._version import __version__ as fatfloppy_version
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

logger = get_logger("IMDImageDriver")

IMD_SECTOR_UNAVAILABLE = 0
IMD_SECTOR_NORMAL = 1
IMD_SECTOR_COMPRESSED = 2
IMD_SECTOR_NORMAL_DEL = 3
IMD_SECTOR_COMPRESSED_DEL = 4
IMD_SECTOR_NORMAL_ERR = 5
IMD_SECTOR_COMPRESSED_ERR = 6
IMD_SECTOR_NORMAL_DEL_ERR = 7
IMD_SECTOR_COMPRESSED_DEL_ERR = 8

IMD_MODE_MAP = {
    0: (500, "FM", "SD"),
    1: (300, "FM", "SD"),
    2: (250, "FM", "SD"),
    3: (500, "MFM", "HD/ED"),
    4: (300, "MFM", "HD"),
    5: (250, "MFM", "DD"),
}

IMD_SECTOR_SIZE_MAP = {
    0: 128,
    1: 256,
    2: 512,
    3: 1024,
    4: 2048,
    5: 4096,
    6: 8192,
}

IMD_HEADER_TERMINATOR = b"\x1a"
IMD_VARIABLE_SIZE_CODE = 0xFF
IMD_DEFAULT_FILL_BYTE = 0xE5
IMD_DEFAULT_BPS = 512
IMD_FALLBACK_BPS = 128
IMD_MODE_INVALID = -1
IMD_SIZE_CODE_INVALID = -1

IMD_HEAD_FLAG_CYLINDER_MAP = 0x80
IMD_HEAD_FLAG_HEAD_MAP = 0x40
IMD_HEAD_FLAG_HEAD_MASK = 0x01

IMD_RPM_FM_DEFAULT = 360
IMD_RPM_MFM_DEFAULT = 300
IMD_RPM_HD_5_25 = 360

IMD_INTERLEAVE_FM = 6
IMD_INTERLEAVE_MFM = 9
IMD_GAP3_FM = 26
IMD_GAP3_MFM = 54

IMD_HEADER_SIZE_MIN = 5
IMD_SIZE_MAP_MULTIPLIER = 2


class IMDTrackInfo:
    """
    Represents the metadata and sector information for a single track in an IMD file.

    This class encapsulates all track-level information including sector numbering,
    optional cylinder/head maps, variable sector sizes, and sector data locations.
    """

    def __init__(
        self,
        mode: int,
        cylinder: int,
        head_flags: int,
        num_sectors: int,
        sector_size_code: int,
    ):
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
        self.head: int = head_flags & IMD_HEAD_FLAG_HEAD_MASK
        self.has_cyl_map: bool = bool(head_flags & IMD_HEAD_FLAG_CYLINDER_MAP)
        self.has_head_map: bool = bool(head_flags & IMD_HEAD_FLAG_HEAD_MAP)
        self.num_sectors: int = num_sectors
        self.sector_size_code: int = sector_size_code

        self.sector_size_map: Optional[dict[int, int]] = None
        self.sector_size: int = self._get_base_sector_size()
        self.sector_num_map: list[int] = []
        self.sector_cyl_map: Optional[dict[int, int]] = None
        self.sector_head_map: Optional[dict[int, int]] = None
        self.sector_data_info: dict[int, tuple[int, int, int]] = {}

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
                f"Inconsistent sector size information for C:{self.cylinder} H:{self.head}"
            )

    def _get_base_sector_size(self) -> int:
        """
        Determines the base sector size from the sector size code.

        Returns:
            The sector size in bytes, or -1 if sizes are variable.

        Raises:
            ValueError: If the sector size code is invalid.
        """
        if self.sector_size_code == IMD_VARIABLE_SIZE_CODE:
            return -1
        size = IMD_SECTOR_SIZE_MAP.get(self.sector_size_code)
        if size is None:
            raise ValueError(f"Invalid sector size code: {self.sector_size_code}")
        return size


class IMDImageDriver(DiskIODriver):
    """
    Disk I/O driver for the ImageDisk (IMD) file format.

    This class handles reading from and writing to IMD disk image files. It can
    parse existing files to determine the disk geometry or format a new image
    based on a provided physical format profile. Sector data is cached in memory
    and flushed to the file on demand.
    """

    driver_type: ClassVar[str] = "IMD"
    driver_file_extensions: ClassVar[list[str]] = [".imd"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "ImageDisk format driver"
    driver_priority: ClassVar[int] = 50

    def __init__(self, file_path: str):
        """
        Initializes the IMDImageDriver.

        If the file exists, it is loaded and parsed. Otherwise, an empty
        driver is initialized, ready for formatting.

        Args:
            file_path: The path to the IMD image file.
        """
        super().__init__()
        self.logger = get_logger(self.__class__.__name__)
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.comment: str = ""
        self.imd_version: str = ""
        self.creation_date: Optional[datetime.datetime] = None
        self.tracks: dict[tuple[int, int], IMDTrackInfo] = {}
        self.image_data: bytearray = bytearray()
        self.dirty: bool = False
        self.file_loaded: bool = False
        self.modified_sector_data: dict[tuple[int, int, int], bytes] = {}
        self.last_format_fill_byte: Optional[int] = None
        self.uses_physical_heads: bool = False
        self._sector_size_cache: dict[tuple[int, int, int], int] = {}

        if Path(self.file_path).exists():
            try:
                self._load_and_parse_imd_file()
                self.file_loaded = True
                for (cyl, head), track_info in self.tracks.items():
                    for sector in track_info.sector_num_map:
                        self._sector_size_cache[(cyl, head, sector)] = (
                            track_info.get_sector_size(sector)
                        )
            except FileNotFoundError:
                self.logger.error(f"IMD file not found: {self.file_path}")
                raise
            except ValueError as e:
                self.logger.error(f"Parse error in IMD file {self.file_path}: {e}")
                raise
            except Exception as e:
                self.logger.exception(
                    f"Unexpected error loading IMD file {self.file_path}: {e}"
                )
                raise
        else:
            self.logger.info(
                f"IMD file '{self.file_path}' not found. Initializing empty driver."
            )
            self.creation_date = datetime.datetime.now()
            self.imd_version = "IMD 1.18"
            self.comment = (
                f"{self.creation_date.strftime('%d/%m/%Y %H:%M:%S')}\r\n"
                f"FatFloppy v{fatfloppy_version}"
            )

    @property
    def allows_geometry_override(self) -> bool:
        """
        IMD files can have geometry overridden, but this may cause inconsistencies.

        Returns:
            True as geometry override is allowed.
        """
        return True

    @property
    def has_embedded_geometry(self) -> bool:
        """
        IMD files contain complete geometry information.

        Returns:
            True as IMD files have embedded geometry.
        """
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """
        IMD files cannot be formatted in place as their structure is track-based.

        Returns:
            False as in-place formatting is not supported.
        """
        return False

    @property
    def supports_new_image_creation(self) -> bool:
        """
        IMD driver can create new formatted images.

        Returns:
            True as new image creation is supported.
        """
        return True

    def flush(self) -> None:
        """
        Writes all modified data to the IMD file.

        Reconstructs the IMD file in memory based on the physical format
        and any modified sector data, then writes it to disk.

        Raises:
            IOError: If the file cannot be written.
        """
        if not self.dirty or not self.physical_format:
            self.logger.debug("Nothing to flush or no format set")
            return

        self.logger.info(f"Flushing IMD to {self.file_path}")
        new_data = bytearray(
            f"{self.imd_version}: {self.comment}".encode("ascii", errors="ignore")
            + IMD_HEADER_TERMINATOR
        )
        new_tracks: dict[tuple[int, int], IMDTrackInfo] = {}

        for cyl in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_key = (cyl, head)
                track_info = self.tracks.get(track_key)
                track_format = self.physical_format.get_track_format(cyl, head)

                mode = next(
                    (
                        m
                        for m, (r, e, _) in IMD_MODE_MAP.items()
                        if r == track_format.rate and e == track_format.encoding
                    ),
                    IMD_MODE_INVALID,
                )
                if mode == IMD_MODE_INVALID:
                    self.logger.error(f"Unsupported format for C:{cyl} H:{head}")
                    continue

                bps = self.physical_format.get_bytes_per_sector(cyl, head)
                size_code = (
                    track_info.sector_size_code
                    if track_info
                    else next(
                        (c for c, s in IMD_SECTOR_SIZE_MAP.items() if s == bps),
                        IMD_SIZE_CODE_INVALID,
                    )
                )
                if size_code == IMD_SIZE_CODE_INVALID:
                    self.logger.error(f"Unsupported sector size for C:{cyl} H:{head}")
                    continue

                spt = track_format.sectors_per_track
                sector_map = (
                    track_info.sector_num_map
                    if track_info and len(track_info.sector_num_map) == spt
                    else track_format.sector_translation_table
                )
                head_flags = (
                    (head & IMD_HEAD_FLAG_HEAD_MASK)
                    | (
                        IMD_HEAD_FLAG_CYLINDER_MAP
                        if track_info and track_info.has_cyl_map
                        else 0
                    )
                    | (
                        IMD_HEAD_FLAG_HEAD_MAP
                        if track_info and track_info.has_head_map
                        else 0
                    )
                )

                rebuilt_info = IMDTrackInfo(mode, cyl, head_flags, spt, size_code)
                rebuilt_info.sector_num_map = sector_map
                if track_info and size_code == IMD_VARIABLE_SIZE_CODE:
                    rebuilt_info.sector_size_map = track_info.sector_size_map

                new_data.extend(
                    struct.pack("<BBBBB", mode, cyl, head_flags, spt, size_code)
                )
                new_data.extend(struct.pack(f"<{spt}B", *sector_map))

                for sector in sector_map:
                    sector_key = (cyl, head, sector)
                    fill = self.last_format_fill_byte or IMD_DEFAULT_FILL_BYTE
                    bps = self.physical_format.get_bytes_per_sector(cyl, head)
                    default_data = bytes([fill] * bps)

                    sector_data = self.modified_sector_data.get(sector_key)
                    if sector_data is None:
                        sector_data = (
                            self._read_original_sector(cyl, head, sector)
                            if self.file_loaded
                            else default_data
                        )

                    target_size = rebuilt_info.get_sector_size(sector)

                    is_compressible = len(sector_data) == target_size and all(
                        b == sector_data[0] for b in sector_data
                    )
                    if is_compressible:
                        data_type = IMD_SECTOR_COMPRESSED
                        content = bytes([sector_data[0] if sector_data else fill])
                    else:
                        data_type = IMD_SECTOR_NORMAL
                        content = sector_data

                    offset = len(new_data) + 1
                    rebuilt_info.sector_data_info[sector] = (
                        offset,
                        data_type,
                        len(content),
                    )
                    new_data.append(data_type)
                    new_data.extend(content)

                new_tracks[track_key] = rebuilt_info

        try:
            with Path(self.file_path).open("wb") as f:
                f.write(new_data)
            self.image_data = new_data
            self.tracks = new_tracks
            self.modified_sector_data = {}
            self.dirty = False
            self.file_loaded = True
            self.logger.info("Flush successful")
        except Exception as e:
            self.logger.error(f"Flush failed: {e}")
            raise OSError(f"Failed to flush IMD: {e}") from e

    def format_imd(
        self, profile: FormatProfile, fill_byte: int = IMD_DEFAULT_FILL_BYTE
    ) -> None:
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
        self.logger.info(f"Formatting IMD with profile: {profile.name}")
        if not profile.physical_format:
            raise ValueError("Physical format required for formatting")

        self.physical_format = copy.deepcopy(profile.physical_format)
        self.tracks, self.image_data, self.modified_sector_data = {}, bytearray(), {}
        self.dirty = True
        self.file_loaded = False
        self.last_format_fill_byte = fill_byte

        self.image_data.extend(
            f"{self.imd_version}: {self.comment}".encode("ascii", errors="ignore")
            + IMD_HEADER_TERMINATOR
        )

        for cyl in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_format = self.physical_format.get_track_format(cyl, head)
                mode = next(
                    (
                        m
                        for m, (r, e, _) in IMD_MODE_MAP.items()
                        if r == track_format.rate and e == track_format.encoding
                    ),
                    IMD_MODE_INVALID,
                )
                size_code = next(
                    (
                        c
                        for c, s in IMD_SECTOR_SIZE_MAP.items()
                        if s == track_format.bytes_per_sector
                    ),
                    IMD_SIZE_CODE_INVALID,
                )

                if mode == IMD_MODE_INVALID or size_code == IMD_SIZE_CODE_INVALID:
                    self.logger.error(
                        f"Unsupported format for C:{cyl} H:{head}, skipping track."
                    )
                    continue

                spt = track_format.sectors_per_track
                sector_num_map = track_format.sector_translation_table
                track_info = IMDTrackInfo(
                    mode, cyl, head & IMD_HEAD_FLAG_HEAD_MASK, spt, size_code
                )
                track_info.sector_num_map = sector_num_map
                self.tracks[(cyl, head)] = track_info

                self.image_data.extend(
                    struct.pack(
                        "<BBBBB",
                        mode,
                        cyl,
                        head & IMD_HEAD_FLAG_HEAD_MASK,
                        spt,
                        size_code,
                    )
                )
                self.image_data.extend(struct.pack(f"<{spt}B", *sector_num_map))

                for _ in range(spt):
                    self.image_data.extend([IMD_SECTOR_COMPRESSED, fill_byte])

        self.logger.info(f"Formatted IMD in memory, size: {len(self.image_data)} bytes")

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for IMD driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": False,
            "can_derive_format": True,
            "preferred_detection_method": "embedded",
        }

    def initialize_new_image(
        self, physical_format: PhysicalFormat, profile: Optional[Any] = None
    ) -> None:
        """
        Initializes a new blank IMD image structure.

        Args:
            physical_format: The physical format for the new image.
            profile: Optional FormatProfile with additional metadata.
        """
        self.format_imd(
            profile
            if profile
            else FormatProfile("temp", "Temp", physical_format, "Unknown", None),
            fill_byte=IMD_DEFAULT_FILL_BYTE,
        )

    def prepare_for_format_application(
        self, format_info: dict
    ) -> tuple[bool, Optional[str]]:
        """
        Validates format compatibility for IMD driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        if not self.file_loaded:
            if "physical_format" not in format_info:
                return False, "Physical format required for creating new IMD image"
            return True, None

        warning = (
            "Applying external format to IMD file will override "
            "the format derived from the file structure. This may "
            "cause read errors if the formats don't match."
        )
        self.logger.warning(warning)

        return True, warning

    def read_boot_sector_data(self) -> Optional[bytes]:
        """
        Reads the boot sector (Cylinder 0, Head 0, Sector 1 or first available).

        Returns:
            The boot sector data as bytes, or None if it cannot be read.
        """
        if not self.physical_format or (track_info := self.tracks.get((0, 0))) is None:
            self.logger.warning("Cannot read boot sector: no format or track")
            return None

        sector_map = track_info.sector_num_map
        sector = 1 if 1 in sector_map else (sector_map[0] if sector_map else None)
        if sector is None:
            self.logger.warning("No sectors in track C:0 H:0")
            return None

        try:
            return self.read_sector(0, 0, sector)
        except Exception as e:
            self.logger.warning(f"Failed to read boot sector: {e}")
            return None

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
            self.logger.debug(
                f"Reading modified sector C:{cylinder} H:{head} S:{sector}"
            )
            return self.modified_sector_data[sector_key]

        return self._read_original_sector(cylinder, head, sector)

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

        if self.file_loaded:
            self.logger.warning(
                "External physical format set, may conflict with IMD data"
            )

        self.physical_format = copy.deepcopy(physical_format)

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        """
        Validates whether an IMD file can be opened.

        Args:
            source: Path to the IMD file.
            **_kwargs: Unused for IMD driver.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not Path(source).exists():
            return False, f"IMD file not found: {source}"

        try:
            with Path(source).open("rb") as f:
                header = f.read(128)

            if IMD_HEADER_TERMINATOR not in header:
                return False, "Missing IMD header terminator (0x1A)"

            header_text = header[: header.find(IMD_HEADER_TERMINATOR)].decode(
                "ascii", errors="ignore"
            )
            if "IMD" not in header_text.upper():
                return False, "Does not appear to be a valid IMD file"

        except Exception as e:
            return False, f"Cannot validate IMD file: {e}"

        return True, None

    def validate_state_for_opening(self) -> tuple[bool, Optional[str]]:
        """
        Validates IMD driver state after opening.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not self.file_path:
            return False, "IMD driver has no file path"

        if self.file_loaded and not self.physical_format:
            return False, "IMD file loaded but no physical format derived"

        return True, None

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

        if self.file_loaded and (
            not track_info or sector not in track_info.sector_data_info
        ):
            raise OSError(f"Invalid sector C:{cylinder} H:{head} S:{sector}")

        self.modified_sector_data[(cylinder, head, sector)] = bytes(data)
        self.dirty = True
        self.logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector}")

    def _add_track_format(
        self,
        track_formats: list[TrackFormat],
        start: int,
        end: int,
        props: tuple[str, int, int, int, int],
        max_head_idx: int,
        sector_translation_table: Optional[list[int]] = None,
    ) -> None:
        """
        Helper to create and add a TrackFormat object to a list.

        Args:
            track_formats: The list to add the new TrackFormat to.
            start: The starting cylinder for this format.
            end: The ending cylinder for this format.
            props: A tuple of (encoding, rate, spt, bps, mode).
            max_head_idx: The maximum head index for this format range.
            sector_translation_table: Optional sector translation table from IMD.
        """
        encoding, rate, spt, bps, _ = props

        if sector_translation_table:
            expected_sequential = list(range(1, spt + 1))
            is_custom_ordering = sector_translation_table != expected_sequential
        else:
            is_custom_ordering = False

        if is_custom_ordering:
            # Custom sector ordering - use minimal defaults
            interleave = 1
            iam_present = False
            gap3_bytes = 0
        else:
            interleave = IMD_INTERLEAVE_FM if encoding == "FM" else IMD_INTERLEAVE_MFM
            iam_present = True
            gap3_bytes = IMD_GAP3_FM if encoding == "FM" else IMD_GAP3_MFM

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
            sector_translation_table=sector_translation_table,
            id_start=1,
            iam_present=iam_present,
            gap1_bytes=None,
            gap2_bytes=None,
            gap3_bytes=gap3_bytes,
            cskew=None,
            hskew=None,
        )
        track_formats.append(tf)

    def _derive_physical_format(self, max_cyl_idx: int, max_head_idx: int) -> None:
        """
        Derives the PhysicalFormat from the parsed track data.

        Groups contiguous tracks with identical properties into TrackFormat definitions.
        Preserves the sector_num_map from the IMD as sector_translation_table.

        Args:
            max_cyl_idx: The maximum cylinder index found in the image.
            max_head_idx: The maximum head index found in the image.
        """
        if not self.tracks:
            self.logger.warning("No tracks to derive format from")
            return

        track_formats: list[TrackFormat] = []
        current_start_cyl = 0
        prev_props = None
        prev_sector_map = None

        for cyl in range(max_cyl_idx + 1):
            key = (cyl, 0)
            track_info = self.tracks.get(key)

            if not track_info:
                if prev_props is not None:
                    self._add_track_format(
                        track_formats,
                        current_start_cyl,
                        cyl - 1,
                        prev_props,
                        max_head_idx,
                        prev_sector_map,
                    )
                current_start_cyl = cyl + 1
                prev_props = None
                prev_sector_map = None
                continue

            rate, encoding, _ = IMD_MODE_MAP.get(track_info.mode, (250, "MFM", "DD"))
            spt = track_info.num_sectors
            bps = (
                track_info.get_sector_size(track_info.sector_num_map[0])
                if track_info.sector_size_code == IMD_VARIABLE_SIZE_CODE
                and track_info.sector_num_map
                else track_info.sector_size
            )

            current_props = (encoding, rate, spt, bps, track_info.mode)
            current_sector_map = track_info.sector_num_map

            if prev_props is not None and (
                current_props != prev_props or current_sector_map != prev_sector_map
            ):
                self._add_track_format(
                    track_formats,
                    current_start_cyl,
                    cyl - 1,
                    prev_props,
                    max_head_idx,
                    prev_sector_map,
                )
                current_start_cyl = cyl

            prev_props = current_props
            prev_sector_map = current_sector_map

        if prev_props is not None:
            self._add_track_format(
                track_formats,
                current_start_cyl,
                max_cyl_idx,
                prev_props,
                max_head_idx,
                prev_sector_map,
            )

        ref_ti = self.tracks.get((0, 0)) or self.tracks[min(self.tracks.keys())]
        ref_rate, ref_encoding, _ = IMD_MODE_MAP.get(ref_ti.mode, (500, "MFM", ""))
        rpm = (
            IMD_RPM_FM_DEFAULT
            if ref_rate == 300 or ref_encoding == "FM"
            else IMD_RPM_MFM_DEFAULT
        )
        if ref_rate == 500 and ref_encoding == "MFM" and ref_ti.num_sectors == 15:
            rpm = IMD_RPM_HD_5_25

        all_bps = {tf.bytes_per_sector for tf in track_formats}
        disk_bps = list(all_bps)[0] if len(all_bps) == 1 else IMD_FALLBACK_BPS

        self.physical_format = PhysicalFormat(
            cylinders=max_cyl_idx + 1,
            heads=max_head_idx + 1,
            rpm=rpm,
            heads_inverted=False,
            bytes_per_sector=disk_bps,
            track_formats=track_formats,
        )
        self.logger.info(
            f"Derived format: Cyls={self.physical_format.cylinders}, "
            f"Heads={self.physical_format.heads}, RPM={rpm}, "
            f"Variable BPS={len(all_bps) > 1}"
        )

    def _get_sector_size(
        self, track_info: Optional[IMDTrackInfo], cylinder: int, head: int, sector: int
    ) -> int:
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

        size = IMD_DEFAULT_BPS
        if self.physical_format:
            try:
                size = self.physical_format.get_bytes_per_sector(cylinder, head)
            except ValueError:
                self.logger.warning(
                    f"No track format for C:{cylinder} H:{head}. Using disk-wide "
                    f"default BPS ({self.physical_format.bytes_per_sector})."
                )
                size = self.physical_format.bytes_per_sector

        if track_info and sector in track_info.sector_num_map:
            try:
                size = track_info.get_sector_size(sector)
            except ValueError:
                self.logger.warning(
                    f"Could not get sector size for C:{cylinder} H:{head} S:{sector} "
                    "from track info, using format-derived size."
                )

        self._sector_size_cache[cache_key] = size
        return size

    def _load_and_parse_imd_file(self) -> None:
        """
        Loads the IMD file from disk and parses its structure.

        Populates track and sector metadata.

        Raises:
            ValueError: If the file cannot be parsed.
        """
        self.logger.info(f"Loading IMD file: {self.file_path}")
        try:
            with Path(self.file_path).open("rb") as f:
                self.image_data = bytearray(f.read())
        except Exception as e:
            raise ValueError(f"Cannot read file: {e}") from e

        if not self.image_data:
            raise ValueError("IMD file is empty")

        header_end_pos = self.image_data.find(IMD_HEADER_TERMINATOR)
        if header_end_pos == -1:
            raise ValueError("IMD header terminator (0x1A) not found")

        header_bytes = self.image_data[:header_end_pos]
        first_colon_pos = header_bytes.find(b":")
        if first_colon_pos != -1:
            self.imd_version = (
                header_bytes[:first_colon_pos].decode("ascii", errors="ignore").strip()
            )
            comment_bytes = header_bytes[first_colon_pos + 1 :]
            try:
                comment_text = comment_bytes.decode("cp437", errors="replace")
                timestamp_pattern = (
                    r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[:]?\s*"
                )
                self.comment = re.sub(
                    timestamp_pattern, "", comment_text, count=1
                ).strip()
            except Exception as e:
                self.logger.warning(f"Failed to decode comment: {e}")
                self.comment = "[Comment Decode Error]"
        else:
            self.logger.warning("No colon in header, using fallback parsing")
            self.imd_version = header_bytes.decode("ascii", errors="ignore").strip()
            self.comment = ""

        offset = header_end_pos + 1
        max_cyl, max_head, parsed_track_count = -1, -1, 0

        while offset < len(self.image_data):
            if offset + IMD_HEADER_SIZE_MIN > len(self.image_data):
                if all(b == 0 for b in self.image_data[offset:]):
                    self.logger.debug("Reached padded end of file")
                    break
                raise ValueError(f"Incomplete track header at offset {offset}")

            mode, cyl, head_flags, num_sectors, sector_size_code = struct.unpack_from(
                "<BBBBB", self.image_data, offset
            )
            offset += IMD_HEADER_SIZE_MIN

            if num_sectors == 0:
                self.logger.warning(
                    f"Track C:{cyl} H:{head_flags & IMD_HEAD_FLAG_HEAD_MASK} has zero sectors, "
                    "skipping to next track header."
                )
                continue

            track_info = IMDTrackInfo(
                mode, cyl, head_flags, num_sectors, sector_size_code
            )
            max_cyl = max(max_cyl, cyl)
            max_head = max(max_head, track_info.head)

            if offset + num_sectors > len(self.image_data):
                raise ValueError(f"EOF in sector map for C:{cyl} H:{track_info.head}")
            track_info.sector_num_map = list(
                struct.unpack_from(f"<{num_sectors}B", self.image_data, offset)
            )
            offset += num_sectors

            if track_info.has_cyl_map:
                if offset + num_sectors > len(self.image_data):
                    raise ValueError(
                        f"EOF in Cylinder map for C:{cyl} H:{track_info.head}"
                    )
                map_data = struct.unpack_from(
                    f"<{num_sectors}B", self.image_data, offset
                )
                track_info.sector_cyl_map = dict(
                    zip(track_info.sector_num_map, map_data)
                )
                offset += num_sectors

            if track_info.has_head_map:
                if offset + num_sectors > len(self.image_data):
                    raise ValueError(f"EOF in Head map for C:{cyl} H:{track_info.head}")
                map_data = struct.unpack_from(
                    f"<{num_sectors}B", self.image_data, offset
                )
                track_info.sector_head_map = {
                    num: val & IMD_HEAD_FLAG_HEAD_MASK
                    for num, val in zip(track_info.sector_num_map, map_data)
                }
                offset += num_sectors

            if track_info.sector_size_code == IMD_VARIABLE_SIZE_CODE:
                size_map_len = num_sectors * IMD_SIZE_MAP_MULTIPLIER
                if offset + size_map_len > len(self.image_data):
                    raise ValueError(f"EOF in size map for C:{cyl} H:{track_info.head}")
                sizes = struct.unpack_from(f"<{num_sectors}H", self.image_data, offset)
                track_info.sector_size_map = dict(zip(track_info.sector_num_map, sizes))
                offset += size_map_len

            for sector_num in track_info.sector_num_map:
                if offset >= len(self.image_data):
                    raise ValueError(
                        f"EOF before sector type for C:{cyl} H:{track_info.head} S:{sector_num}"
                    )
                data_type = self.image_data[offset]
                data_offset = offset + 1
                sector_size = track_info.get_sector_size(sector_num)

                if data_type == IMD_SECTOR_UNAVAILABLE:
                    data_size = 0
                elif data_type in (
                    IMD_SECTOR_COMPRESSED,
                    IMD_SECTOR_COMPRESSED_DEL,
                    IMD_SECTOR_COMPRESSED_ERR,
                    IMD_SECTOR_COMPRESSED_DEL_ERR,
                ):
                    data_size = 1
                elif data_type in (
                    IMD_SECTOR_NORMAL,
                    IMD_SECTOR_NORMAL_DEL,
                    IMD_SECTOR_NORMAL_ERR,
                    IMD_SECTOR_NORMAL_DEL_ERR,
                ):
                    data_size = sector_size
                else:
                    raise ValueError(
                        f"Unknown sector type {data_type} for C:{cyl} H:{track_info.head} "
                        f"S:{sector_num}"
                    )

                if offset + 1 + data_size > len(self.image_data):
                    raise ValueError(
                        f"EOF in sector data for C:{cyl} H:{track_info.head} S:{sector_num}"
                    )

                track_info.sector_data_info[sector_num] = (
                    data_offset,
                    data_type,
                    data_size,
                )
                offset += 1 + data_size

            self.tracks[(cyl, track_info.head)] = track_info
            parsed_track_count += 1

        self.logger.info(
            f"Parsed {parsed_track_count} tracks. Max Cyl={max_cyl}, Max Head={max_head}"
        )
        if parsed_track_count > 0:
            self._derive_physical_format(max_cyl, max_head)

    def _read_original_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a sector's data from the originally loaded image data.

        Bypasses the modified data cache.

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
            self.logger.warning(
                f"Missing track or sector C:{cylinder} H:{head} S:{sector}"
            )
            return bytes(expected_size)

        offset, data_type, size = track_info.sector_data_info[sector]

        if data_type == IMD_SECTOR_UNAVAILABLE:
            return bytes(expected_size)
        elif data_type in (
            IMD_SECTOR_COMPRESSED,
            IMD_SECTOR_COMPRESSED_DEL,
            IMD_SECTOR_COMPRESSED_ERR,
            IMD_SECTOR_COMPRESSED_DEL_ERR,
        ):
            fill_byte = self.image_data[offset]
            return bytes([fill_byte] * expected_size)
        elif data_type in (
            IMD_SECTOR_NORMAL,
            IMD_SECTOR_NORMAL_DEL,
            IMD_SECTOR_NORMAL_ERR,
            IMD_SECTOR_NORMAL_DEL_ERR,
        ):
            data = self.image_data[offset : offset + size]
            if len(data) < expected_size:
                return data.ljust(expected_size, b"\0")
            return data[:expected_size]
        else:
            self.logger.error(
                f"Unknown sector type {data_type} for C:{cylinder} H:{head} S:{sector}"
            )
            return bytes(expected_size)
