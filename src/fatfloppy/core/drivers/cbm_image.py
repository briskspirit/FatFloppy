"""Commodore D64/D71/D81 disk image driver."""

import copy
from pathlib import Path
from typing import ClassVar, Optional

from ..cbm_layout import (
    CBM_AMBIGUOUS_SIZES,
    CBM_BYTES_PER_SECTOR,
    CBM_SIZE_TABLE,
    CBMDiskLayout,
    build_physical_format,
    layout_for_variant,
)
from ..physical_format import PhysicalFormat
from ..utils.atomic_io import atomic_write
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

CBM_READONLY_TRACK_COUNTS = {42}

logger = get_logger("CBMImageDriver")


class CBMImageDriver(DiskIODriver):
    """
    Driver for Commodore 1541/1571/1581 sector-dump images.

    Geometry is derived from the exact file size. The optional trailing
    error-byte block (one byte per sector) is parsed at open, excluded from
    sector addressing, preserved verbatim on write, and exposed per sector.
    """

    driver_type: ClassVar[str] = "CBM"
    driver_file_extensions: ClassVar[list[str]] = [".d64", ".d71", ".d81"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "Commodore D64/D71/D81 image driver"
    driver_priority: ClassVar[int] = 50

    def __init__(self, file_path: str, image_data: Optional[bytes] = None):
        super().__init__()
        self.logger = get_logger(self.__class__.__name__)
        self.file_path = file_path
        self.dirty = False
        self.read_only = False
        self.image_data = bytearray()
        self.error_codes: Optional[bytearray] = None
        self._layout: Optional[CBMDiskLayout] = None

        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
            self._validate_and_configure()
        elif file_path and Path(file_path).exists():
            self.image_data = bytearray(Path(file_path).read_bytes())
            self._validate_and_configure()
        elif file_path:
            self.logger.info(f"{file_path} does not exist; awaiting new-image init")
        else:
            raise ValueError("CBM driver requires a file path or image data")

    def _validate_and_configure(self) -> None:
        size = len(self.image_data)
        if size not in CBM_SIZE_TABLE:
            raise ValueError(f"Not a known CBM image size: {size}")
        family, tracks, has_errors = CBM_SIZE_TABLE[size]
        self._layout = layout_for_variant(family, tracks)
        data_size = self._layout.total_sectors * CBM_BYTES_PER_SECTOR
        if has_errors:
            self.error_codes = bytearray(self.image_data[data_size:])
            self.image_data = self.image_data[:data_size]
        self.read_only = tracks in CBM_READONLY_TRACK_COUNTS
        self.physical_format = build_physical_format(family, tracks)
        self.logger.info(
            f"CBM {family} {tracks}-track image"
            f"{' with error block' if has_errors else ''}"
        )

    @property
    def has_embedded_geometry(self) -> bool:
        return True

    @property
    def has_error_block(self) -> bool:
        return self.error_codes is not None

    def get_format_requirements(self) -> dict:
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": True,
            "can_derive_format": True,
            "preferred_detection_method": "embedded",
        }

    def _sector_offset(self, cylinder: int, head: int, sector: int) -> int:
        if self._layout is None:
            raise OSError("CBM driver not configured (no image data)")
        if head != 0:
            raise OSError(f"CBM images are single-head, got head {head}")
        track = cylinder + 1
        if not 1 <= track <= self._layout.tracks:
            raise OSError(f"Invalid cylinder {cylinder}")
        if not 0 <= sector < self._layout.spt(track):
            raise OSError(
                f"Invalid sector {sector} on track {track} "
                f"(0-{self._layout.spt(track) - 1})"
            )
        return self._layout.linear_index(track, sector) * CBM_BYTES_PER_SECTOR

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Read one 256-byte CBM sector.

        Args:
            cylinder: 0-based cylinder (CBM track - 1).
            head: Must be 0 (CBM images are single-head).
            sector: 0-based sector index within the track.

        Returns:
            256 bytes of sector data.

        Raises:
            OSError: If the driver is not configured, head != 0, or the
                cylinder/sector is out of range for this image.
        """
        off = self._sector_offset(cylinder, head, sector)
        return bytes(self.image_data[off : off + CBM_BYTES_PER_SECTOR])

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """Write one 256-byte CBM sector.

        Args:
            cylinder: 0-based cylinder (CBM track - 1).
            head: Must be 0 (CBM images are single-head).
            sector: 0-based sector index within the track.
            data: Exactly 256 bytes to write.

        Raises:
            OSError: If the image is read-only or addressing is out of range.
            ValueError: If ``data`` is not exactly 256 bytes.
        """
        if self.read_only:
            raise OSError(f"{self._layout.tracks}-track CBM images are read-only")
        if len(data) != CBM_BYTES_PER_SECTOR:
            raise ValueError(f"Sector data must be 256 bytes, got {len(data)}")
        off = self._sector_offset(cylinder, head, sector)
        self.image_data[off : off + CBM_BYTES_PER_SECTOR] = data
        self.dirty = True

    def get_sector_error(self, cylinder: int, head: int, sector: int) -> Optional[int]:
        if self.error_codes is None:
            return None
        return self.error_codes[
            self._sector_offset(cylinder, head, sector) // CBM_BYTES_PER_SECTOR
        ]

    def flush(self) -> None:
        """Persist the image (and optional error block) to disk atomically.

        Raises:
            OSError: If the atomic write fails.
        """
        if not self.dirty:
            return
        blob = bytes(self.image_data) + (
            bytes(self.error_codes) if self.error_codes is not None else b""
        )
        atomic_write(self.file_path, blob)
        self.dirty = False
        self.logger.info(f"Flushed CBM image to {self.file_path}")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")
        self.physical_format = copy.deepcopy(physical_format)

    def initialize_new_image(
        self, physical_format: Optional[PhysicalFormat] = None, _profile=None
    ) -> None:
        pf = physical_format or build_physical_format("D64", 35)
        if pf.bytes_per_sector != CBM_BYTES_PER_SECTOR:
            raise ValueError(
                f"CBM images require 256-byte sectors, got {pf.bytes_per_sector}"
            )
        if pf.heads != 1:
            raise ValueError(f"CBM images are single-head, got {pf.heads} heads")
        spt0 = pf.get_sectors_per_track(0, 0)
        if spt0 not in (21, 40):
            raise ValueError(
                f"CBM geometry requires 21 or 40 sectors/track on track 0, got {spt0}"
            )
        family = {21: "D64", 40: "D81"}[spt0]
        if pf.cylinders == 70:
            family = "D71"
        tracks = pf.cylinders
        if tracks in CBM_READONLY_TRACK_COUNTS:
            raise ValueError(
                f"Creating {tracks}-track CBM images is not supported (read-only variant)"
            )
        layout = layout_for_variant(family, tracks)
        self.image_data = bytearray(layout.total_sectors * CBM_BYTES_PER_SECTOR)
        self.error_codes = None
        self._layout = layout
        self.read_only = False
        self.physical_format = build_physical_format(family, tracks)
        self.dirty = True

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        """Accept CBM image files by size; probe content for ambiguous sizes.

        Unique sizes (174848, 175531, etc.) are accepted on size alone.
        Ambiguous sizes (196608, 819200) that collide with raw IMG or Mac/Atari
        800 K images require a minimal BAM/header content probe before the
        driver claims the file.
        """
        path = Path(source)
        if not path.exists():
            return False, f"File not found: {source}"
        size = path.stat().st_size
        if size not in CBM_SIZE_TABLE:
            return False, f"Not a known CBM image size: {size}"
        if size not in CBM_AMBIGUOUS_SIZES:
            return True, None
        family, tracks, _err = CBM_SIZE_TABLE[size]
        layout = layout_for_variant(family, tracks)
        probe_off = layout.sectors_before(layout.dir_track) * CBM_BYTES_PER_SECTOR
        with path.open("rb") as f:
            f.seek(probe_off)
            probe = f.read(2 * CBM_BYTES_PER_SECTOR)
        if len(probe) < 2 * CBM_BYTES_PER_SECTOR:
            return False, "Truncated CBM image"
        if family == "D81":
            bam = probe[CBM_BYTES_PER_SECTOR:]
            strong = bam[2] == 0x44 and bam[3] == 0xBB
            weak = (probe[2] == 0x44) + (probe[0x19:0x1B] == b"3D")
            if strong or weak >= 2:
                return True, None
            return False, "Size matches D81 but no CBM 1581 header/BAM signatures"
        signals = 0
        if 1 <= probe[0] <= tracks and probe[1] < 21:
            signals += 1
        if probe[2] in (0x41, 0x00):
            signals += 1
        if 0xA0 in probe[0x90:0xA0]:
            signals += 1
        if signals >= 2:
            return True, None
        return False, "Size matches 40-track D64 but no CBM BAM signals"

    def validate_state_for_opening(self) -> tuple[bool, Optional[str]]:
        if self._layout is None:
            return False, "CBM driver has no image data"
        return True, None
