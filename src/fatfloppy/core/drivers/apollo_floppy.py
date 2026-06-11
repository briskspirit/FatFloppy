"""Apollo DOMAIN floppy image driver (read-only).

Handles 77×2×8×1024-byte flat images produced by Apollo DOMAIN workstations.
These images carry a PV label at sector 0 whose first 6 bytes are the ASCII
magic ``APOLLO``.  Sector content is laid out cylinder-major (cyl, head, sector
all zero-based), matching PhysicalFormat.chs_to_byte_offset with interleave 1
and id_start 0:

    offset = ((cylinder * heads + head) * spt + sector) * bps
           = ((cylinder * 2   + head)  * 8   + sector) * 1024

The driver is **read-only** because these images are archival wbak backup media;
there is no write path in the DOMAIN wbak protocol and writing would corrupt the
record framing.  ``flush`` is a no-op for the same reason: with no write path
there is never any dirty state to flush.
"""

import copy
from pathlib import Path
from typing import ClassVar, Optional

from ..apollo_wbak import (
    APOLLO_MAGIC,
    IMAGE_SIZE,
    build_apollo_physical_format,
)
from ..apollo_wbak import (
    CYLINDERS as _CYLINDERS,
)
from ..apollo_wbak import (
    HEADS as _HEADS,
)
from ..apollo_wbak import (
    SECTOR as _BPS,
)
from ..apollo_wbak import (
    SECTORS_PER_TRACK as _SPT,
)
from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

logger = get_logger("ApolloFloppyDriver")


class ApolloFloppyDriver(DiskIODriver):
    """Read-only driver for Apollo DOMAIN floppy images (wbak backup media).

    Geometry: 77 cylinders × 2 heads × 8 sectors × 1024 bytes (MFM, 500 kb/s,
    360 rpm).  Validated by file size (exactly 1,261,568 bytes) and PV-label
    magic (``APOLLO`` at offset 0).
    """

    driver_type: ClassVar[str] = "APOLLO"
    driver_file_extensions: ClassVar[list[str]] = [".img", ".afd"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "Apollo DOMAIN floppy (read-only)"
    driver_priority: ClassVar[int] = 50

    def __init__(self, file_path: str):
        super().__init__()
        self.logger = get_logger(self.__class__.__name__)
        self.file_path = file_path

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        data = path.read_bytes()
        if len(data) != IMAGE_SIZE:
            raise ValueError(
                f"Apollo image must be exactly {IMAGE_SIZE} bytes, "
                f"got {len(data)}: {file_path}"
            )

        self._data = data
        self.physical_format = build_apollo_physical_format()
        self.logger.info(f"Opened Apollo floppy image: {file_path}")

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Read one 1024-byte sector.

        Args:
            cylinder: 0-based cylinder (0–76).
            head: 0-based head (0–1).
            sector: 0-based sector index within the track (0–7).

        Returns:
            1024 bytes of sector data.

        Raises:
            OSError: If any coordinate is out of range.
        """
        self._check_bounds(cylinder, head, sector)
        offset = ((cylinder * _HEADS + head) * _SPT + sector) * _BPS
        return self._data[offset : offset + _BPS]

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        """Always raises OSError — Apollo wbak images are read-only.

        Raises:
            OSError: Always.
        """
        raise OSError("Apollo DOMAIN floppy images are read-only archival media")

    # ------------------------------------------------------------------
    # Image creation / flush
    # ------------------------------------------------------------------

    @property
    def supports_in_place_formatting(self) -> bool:
        """Apollo images are archival; in-place formatting is not supported."""
        return False

    @property
    def supports_new_image_creation(self) -> bool:
        """Apollo images are archival; new image creation is not supported."""
        return False

    def initialize_new_image(
        self, physical_format: Optional[PhysicalFormat] = None, _profile=None
    ) -> None:
        """Always raises NotImplementedError — Apollo images are read-only.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "ApolloFloppyDriver does not support creating new images"
        )

    def flush(self) -> None:
        """No-op: the driver is read-only, so there is never dirty state to flush."""
        # No write path → nothing to persist.

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """Override the stored physical format (external geometry override)."""
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")
        self.physical_format = copy.deepcopy(physical_format)

    @property
    def has_embedded_geometry(self) -> bool:
        """Apollo images carry fixed geometry derived from the PV label size."""
        return True

    def get_format_requirements(self) -> dict:
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": True,
            "can_derive_format": True,
            "preferred_detection_method": "embedded",
        }

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        """Accept the file only if size == IMAGE_SIZE and magic bytes match.

        Only the first 6 bytes are read — the minimal check to gate on content
        without pulling the whole 1.2 MB into memory during auto-detection.

        Args:
            source: Path to the candidate image file.

        Returns:
            ``(True, None)`` if valid; ``(False, reason)`` otherwise.
        """
        path = Path(source)
        if not path.exists():
            return False, f"File not found: {source}"
        size = path.stat().st_size
        if size != IMAGE_SIZE:
            return False, f"Not an Apollo image: size {size} != {IMAGE_SIZE}"
        with path.open("rb") as f:
            magic = f.read(len(APOLLO_MAGIC))
        if magic != APOLLO_MAGIC:
            return False, "Not an Apollo image: missing APOLLO magic at offset 0"
        return True, None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_bounds(self, cylinder: int, head: int, sector: int) -> None:
        if not (0 <= cylinder < _CYLINDERS):
            raise OSError(f"Cylinder {cylinder} out of range (0-{_CYLINDERS - 1})")
        if not (0 <= head < _HEADS):
            raise OSError(f"Head {head} out of range (0-{_HEADS - 1})")
        if not (0 <= sector < _SPT):
            raise OSError(f"Sector {sector} out of range (0-{_SPT - 1})")
