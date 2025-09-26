# src/fatfloppy/core/drivers/img.py
"""
Raw disk image (.IMG) file format driver.

This module provides a DiskIODriver for handling raw, sector-by-sector
disk images. It treats the image file as a contiguous block of data and
relies on a PhysicalFormat definition to map Cylinder/Head/Sector (CHS)
coordinates to a byte offset within the file.
"""

import copy
import os
from typing import Optional

from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

logger = get_logger("IMGImageDriver")


class IMGImageDriver(DiskIODriver):
    """
    Disk I/O driver for raw flat-file image formats (e.g., .IMG, .IMA).

    This driver interprets the image file as a simple binary blob representing
    the disk's sectors laid out sequentially. A PhysicalFormat must be provided
    to describe the disk geometry and sector ordering.
    """

    def __init__(self, file_path: str, image_data: Optional[bytes] = None):
        """
        Initializes the IMGImageDriver.

        If `image_data` is provided, the driver operates on it in memory.
        Otherwise, it loads the image from the specified `file_path`.

        Args:
            file_path: The path to the raw image file. Required if `image_data`
                       is None.
            image_data: Optional byte array to initialize the driver with,
                        bypassing file I/O for loading.

        Raises:
            ValueError: If `file_path` is not provided and `image_data` is None,
                        or if a structured (non-raw) DSK format is detected.
            FileNotFoundError: If the specified file does not exist.
            IOError: If there is an error reading the file.
        """
        super().__init__()
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None
        self.physical_format_set: bool = False
        self.uses_physical_heads: bool = False
        self.dirty: bool = False
        self.image_data: bytearray

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
                    # Check for common non-raw formats that might be mistaken for .IMG
                    header_peek = f.read(34)
                    f.seek(0)
                    if header_peek.startswith(b'EXTENDED CPC DSK'):
                        msg = (f"EDSK (Extended DSK) format detected in '{self.file_path}'. "
                               "This format is structured and not a raw image.")
                        logger.error(msg)
                        raise ValueError(msg)
                    elif header_peek.startswith(b'MV - CPC'):
                        msg = (f"Standard Amstrad DSK format detected in '{self.file_path}'. "
                               "This structured format is not a raw image.")
                        logger.error(msg)
                        raise ValueError(msg)

                    self.image_data = bytearray(f.read())
                logger.info(f"Loaded image file {self.file_path} as raw image, size {len(self.image_data)}")
            except FileNotFoundError:
                logger.error(f"Image file not found: {self.file_path}")
                raise
            except ValueError as ve:
                # Re-raise ValueError to propagate format detection errors
                logger.debug(f"ValueError during IMG driver init for {self.file_path}: {ve}")
                raise
            except Exception as e:
                logger.error(f"Failed to read image file {self.file_path}: {e}")
                # If the error is an IOError/OSError, re-raise it to preserve its
                # specific message. Otherwise, wrap it in a generic IOError.
                if isinstance(e, OSError):
                    raise
                raise IOError(f"Failed to read image file {self.file_path}") from e

    # --- Public Methods ---

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the disk image.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            The sector data as a bytes object.

        Raises:
            ValueError: If the physical format has not been set.
            IOError: If the calculated sector offset is out of the image bounds.
        """
        if not self.physical_format:
            raise ValueError("Physical format not set")

        bytes_per_sector = self.physical_format.get_bytes_per_sector(cylinder, head)
        offset = self._calculate_sector_offset(cylinder, head, sector)

        if offset + bytes_per_sector > len(self.image_data):
            raise IOError(f"Sector C:{cylinder} H:{head} S:{sector} out of bounds")

        logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        return bytes(self.image_data[offset:offset + bytes_per_sector])

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the in-memory image data.

        The data is marked as "dirty" and will be written to the file
        when `flush()` is called.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.
            data: The sector data to write.

        Raises:
            ValueError: If physical format is not set or data size is incorrect.
            IOError: If the write operation is outside the image bounds.
        """
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
        """
        Writes the in-memory image data back to the file if it is dirty.

        Raises:
            IOError: If the file cannot be written.
        """
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
        """
        Sets the physical format of the disk.

        This is a required step to enable sector I/O, as it defines the
        disk geometry and CHS-to-offset mapping.

        Args:
            physical_format: The PhysicalFormat object to apply.

        Raises:
            TypeError: If the provided object is not a PhysicalFormat.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")

        self.physical_format = copy.deepcopy(physical_format)
        self.physical_format_set = True

        if self.image_data and self.physical_format.total_bytes != len(self.image_data):
            logger.warning(
                f"Format size {self.physical_format.total_bytes} != "
                f"image size {len(self.image_data)}"
            )
        logger.info(f"Physical format set with total bytes: {self.physical_format.total_bytes}")

    # --- Private Methods ---

    def _calculate_sector_offset(self, cylinder: int, head: int, sector: int) -> int:
        """
        Calculates the byte offset for a given CHS address.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            The byte offset from the beginning of the image data.

        Raises:
            ValueError: If the physical format is not set or the CHS is invalid.
        """
        if not self.physical_format:
            raise ValueError("No physical format set")

        try:
            return self.physical_format.chs_to_byte_offset(cylinder, head, sector)
        except (ValueError, NotImplementedError) as e:
            logger.error(f"Invalid CHS C:{cylinder} H:{head} S:{sector}: {e}")
            raise ValueError(f"Invalid sector access: {e}") from e
