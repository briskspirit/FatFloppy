# src/fatfloppy/core/drivers/img.py
import os
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
        self.uses_physical_heads = False
        self.dirty = False

        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
            logger.debug(f"Initialized IMG driver with provided image data of size {len(self.image_data)}")
        else:
            if not self.file_path:
                logger.error("IMG driver initialized without file_path and no image_data.")
                raise ValueError("File path must be provided for IMG driver if image_data is not given.")

            try:
                with open(self.file_path, "rb") as f:
                    header_peek = f.read(34)
                    f.seek(0)
                    if header_peek.startswith(b'EXTENDED CPC DSK'):
                        logger.error(f"EDSK (Extended DSK) format detected in '{self.file_path}'. This format is structured and not a raw image. Full EDSK support is not yet available.")
                        raise ValueError(f"EDSK format in '{self.file_path}' is not supported by the raw image driver.")
                    elif header_peek.startswith(b'MV - CPC'):
                        logger.error(f"Standard Amstrad DSK format detected in '{self.file_path}'. This format is structured (contains headers and track info blocks) and not a simple raw sector image. It cannot be loaded by the raw image driver.")
                        raise ValueError(f"Standard Amstrad DSK format in '{self.file_path}' is not supported by the raw image driver. A dedicated DSK parser is required.")
                    self.image_data = bytearray(f.read())
                logger.info(f"Loaded image file {self.file_path} as raw image, size {len(self.image_data)}")
            except FileNotFoundError:
                logger.error(f"Image file not found: {self.file_path}")
                raise
            except ValueError as ve:
                logger.debug(f"ValueError during IMG driver init for {self.file_path}: {ve}")
                raise ve
            except Exception as e:
                logger.error(f"Failed to read image file {self.file_path}: {e}")
                raise

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        if not self.physical_format:
            raise ValueError("Physical format not set")
        bytes_per_sector = self.physical_format.get_bytes_per_sector(cylinder, head)
        offset = self._calculate_sector_offset(cylinder, head, sector)
        if offset + bytes_per_sector > len(self.image_data):
            raise IOError(f"Sector C:{cylinder} H:{head} S:{sector} out of bounds")
        logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        return bytes(self.image_data[offset:offset + bytes_per_sector])

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("Physical format not set")
        bytes_per_sector = self.physical_format.get_bytes_per_sector(cylinder, head)
        if bytes_per_sector <= 0:
            raise ValueError(f"Invalid sector size: {bytes_per_sector}")
        if len(data) != bytes_per_sector:
            raise ValueError(f"Data size mismatch: {len(data)} vs {bytes_per_sector}")
        offset = self._calculate_sector_offset(cylinder, head, sector)
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

    def _calculate_sector_offset(self, cylinder: int, head: int, sector: int) -> int:
        if not self.physical_format:
            raise ValueError("No physical format set")
        try:
            return self.physical_format.chs_to_byte_offset(cylinder, head, sector)
        except (ValueError, NotImplementedError) as e:
            logger.error(f"Invalid CHS C:{cylinder} H:{head} S:{sector}: {e}")
            raise ValueError(f"Invalid sector access: {e}") from e
