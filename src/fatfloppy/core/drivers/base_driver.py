# src/fatfloppy/core/drivers/base_driver.py
"""
Abstract base class for disk I/O drivers.

This module defines the `DiskIODriver` interface, which provides a standard
set of methods for reading from, writing to, and managing disk image files
of various formats. All specific driver implementations (e.g., for .IMG or
.IMD files) must inherit from this class and implement its abstract methods.
"""

from abc import ABC, abstractmethod
from typing import Optional

from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger


class DiskIODriver(ABC):
    """
    An abstract base class for disk image I/O drivers.

    Subclasses must implement the abstract methods to provide format-specific
    logic for accessing sector data.
    """

    def __init__(self):
        """Initializes the base driver."""
        self.logger = get_logger(self.__class__.__name__)
        self.physical_format: Optional[PhysicalFormat] = None

    @abstractmethod
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
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError

    @abstractmethod
    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to the disk image.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.
            data: The sector data to write.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError

    def flush(self) -> None:
        """
        Writes any buffered changes to the disk image file.

        This method can be overridden by subclasses if the driver uses
        in-memory caching or buffering. The default implementation does nothing.
        """
        pass

    @abstractmethod
    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical format of the disk.

        This provides the driver with the necessary geometry (cylinders, heads,
        sectors per track, etc.) to interpret the disk image data.

        Args:
            physical_format: The PhysicalFormat object to apply.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError
