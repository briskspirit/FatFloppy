# src/fatfloppy/core/drivers/img.py
import copy
from typing import List, Optional, Tuple, Dict

from .base_driver import DiskIODriver
from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger

logger = get_logger()


class IMGImageDriver(DiskIODriver):
    def __init__(self, file_path, image_data=None):
        super().__init__()
        self.file_path = file_path
        self.physical_format = None
        self.physical_format_set = False
        self.uses_physical_heads = False  # Explicitly indicate logical heads are used
        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
        else:
            try:
                with open(file_path, "rb") as f:
                    self.image_data = bytearray(f.read())
                self.dirty = False
            except FileNotFoundError:
                 self.logger.error(f"Image file not found: {file_path}")
                 self.image_data = bytearray()
                 self.dirty = False

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        if not self.physical_format:
            raise ValueError("Physical format not set, cannot read sector")
        bytes_per_sector = self.physical_format.bytes_per_sector
        try:
            offset = self._calculate_sector_offset(cylinder, head, sector, bytes_per_sector)
        except ValueError as e:
            raise IOError(f"Invalid sector access: {e}")
        if offset + bytes_per_sector > len(self.image_data):
            raise IOError(f"Sector C:{cylinder} H:{head} S:{sector} is out of bounds")
        return bytes(self.image_data[offset:offset + bytes_per_sector])

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("Physical format not set, cannot write sector")
        bytes_per_sector = self.physical_format.bytes_per_sector
        if bytes_per_sector <= 0:
            raise ValueError(f"Invalid sector size ({bytes_per_sector}) in physical format.")
        if len(data) != bytes_per_sector:
            raise ValueError(f"Data length ({len(data)}) does not match sector size ({bytes_per_sector})")
        try:
            offset = self._calculate_sector_offset(cylinder, head, sector, bytes_per_sector)
        except ValueError as e:
            raise IOError(f"Invalid sector access: {e}")
        if offset + bytes_per_sector > len(self.image_data):
            raise IOError(f"Cannot write sector C:{cylinder} H:{head} S:{sector}: out of bounds")
        self.image_data[offset:offset + bytes_per_sector] = data
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            self.logger.debug(f"Flushing {len(self.image_data)} bytes to {self.file_path}")
            with open(self.file_path, "wb") as f:
                f.write(self.image_data)
            self.logger.debug("Flush completed")
            self.dirty = False
        else:
            self.logger.debug("No changes to flush")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")
        self.physical_format = copy.deepcopy(physical_format)
        # If image data exists, verify size compatibility
        if self.image_data and self.physical_format.total_bytes != len(self.image_data):
             self.logger.warning(f"Setting physical format with total size {self.physical_format.total_bytes} "
                                 f"which differs from existing image data size {len(self.image_data)}.")
             # Option 1: Resize image data (potentially dangerous if truncating)
             # Option 2: Pad image data if smaller
             # Option 3: Just warn (current approach)
             pass

    def _calculate_sector_offset(self, cylinder: int, head: int, sector: int, bytes_per_sector: int) -> int:
        if not self.physical_format:
            raise ValueError("Physical format not set")
        self.physical_format.validate_chs(cylinder, head, sector)
        try:
            lba = self.physical_format.chs_to_lba(cylinder, head, sector)
            return lba * bytes_per_sector
        except ValueError as e:
            raise ValueError(f"Cannot calculate offset for C:{cylinder} H:{head} S:{sector}: {e}") from e
