# src/fatfloppy/core/drivers/img.py
import copy

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
        self.dirty = False
        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
            logger.debug(f"Initialized with provided image data of size {len(self.image_data)}")
        else:
            try:
                with open(file_path, "rb") as f:
                    self.image_data = bytearray(f.read())
                logger.info(f"Loaded image file {file_path} with size {len(self.image_data)}")
            except FileNotFoundError:
                logger.error(f"Image file not found: {file_path}")
                self.image_data = bytearray()
            except Exception as e:
                logger.error(f"Failed to read image file {file_path}: {e}")
                raise

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        if not self.physical_format:
            raise ValueError("Physical format not set")
        bytes_per_sector = self.physical_format.bytes_per_sector
        offset = self._calculate_sector_offset(cylinder, head, sector, bytes_per_sector)
        if offset + bytes_per_sector > len(self.image_data):
            raise IOError(f"Sector C:{cylinder} H:{head} S:{sector} out of bounds")
        logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        return bytes(self.image_data[offset:offset + bytes_per_sector])

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("Physical format not set")
        bytes_per_sector = self.physical_format.bytes_per_sector
        if bytes_per_sector <= 0:
            raise ValueError(f"Invalid sector size: {bytes_per_sector}")
        if len(data) != bytes_per_sector:
            raise ValueError(f"Data size mismatch: {len(data)} vs {bytes_per_sector}")
        offset = self._calculate_sector_offset(cylinder, head, sector, bytes_per_sector)
        if offset + bytes_per_sector > len(self.image_data):
            raise IOError(f"Write out of bounds for C:{cylinder} H:{head} S:{sector}")
        self.image_data[offset:offset + bytes_per_sector] = data
        self.dirty = True
        logger.debug(f"Wrote sector C:{cylinder} H:{head} S:{sector}")

    def flush(self) -> None:
        if not self.dirty:
            logger.debug("No changes to flush")
            return
        try:
            with open(self.file_path, "wb") as f:
                f.write(self.image_data)
            logger.info(f"Flushed {len(self.image_data)} bytes to {self.file_path}")
            self.dirty = False
        except Exception as e:
            logger.error(f"Failed to flush image data to {self.file_path}: {e}")
            raise IOError(f"Flush failed: {e}") from e

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")
        self.physical_format = copy.deepcopy(physical_format)
        self.physical_format_set = True
        if self.image_data and self.physical_format.total_bytes != len(self.image_data):
            logger.warning(f"Format size {self.physical_format.total_bytes} != image size {len(self.image_data)}")
        logger.info(f"Physical format set with total bytes: {self.physical_format.total_bytes}")

    def _calculate_sector_offset(self, cylinder: int, head: int, sector: int, bytes_per_sector: int) -> int:
        if not self.physical_format:
            raise ValueError("No physical format set")
        try:
            lba = self.physical_format.chs_to_lba(cylinder, head, sector)
            return lba * bytes_per_sector
        except ValueError as e:
            logger.error(f"Invalid CHS C:{cylinder} H:{head} S:{sector}: {e}")
            raise ValueError(f"Invalid sector access: {e}") from e
