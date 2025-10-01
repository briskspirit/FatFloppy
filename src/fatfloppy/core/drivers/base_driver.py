# src/fatfloppy/core/drivers/base_driver.py
"""
Abstract base class for disk I/O drivers.

This module defines the `DiskIODriver` interface, which provides a standard
set of methods for reading from, writing to, and managing disk image files
of various formats. All specific driver implementations (e.g., for .IMG or
.IMD files) must inherit from this class and implement its abstract methods.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Any

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

    # --- Capability Methods ---

    @property
    def driver_category(self) -> str:
        """
        Returns the category of this driver.

        Categories determine how the driver handles format detection and application:
        - 'metadata_based': Driver has self-describing format (IMD, H17)
        - 'raw': Driver requires external format information (IMG)
        - 'physical': Driver accesses physical hardware (Greaseweazle)

        Returns:
            The driver category as a string.
        """
        return "raw"  # Default category

    @property
    def supports_in_place_formatting(self) -> bool:
        """
        Indicates whether this driver can format an already-open disk in place.

        Returns:
            True if the driver supports formatting after being opened.
        """
        return True  # Most drivers support this

    @property
    def supports_new_image_creation(self) -> bool:
        """
        Indicates whether this driver can create new image files.

        Returns:
            True if the driver can create new images from scratch.
        """
        return True  # Most drivers support this

    @property
    def requires_initialization(self) -> bool:
        """
        Indicates whether the driver needs explicit initialization.

        Returns:
            True if initialize() must be called before I/O operations.
        """
        return False  # Most drivers don't need this

    @property
    def has_embedded_geometry(self) -> bool:
        """
        Indicates whether the driver's file format contains geometry information.

        Returns:
            True if the driver can derive geometry from the file itself.
        """
        return False  # Most drivers don't have this

    @property
    def allows_geometry_override(self) -> bool:
        """
        Indicates whether the driver allows external geometry to override embedded geometry.

        Returns:
            True if set_physical_format() can override file-derived geometry.
        """
        return True  # Most drivers allow this

    def validate_state_for_opening(self) -> Tuple[bool, Optional[str]]:
        """
        Validates that the driver is in a valid state after opening.

        This allows drivers to perform post-open validation without the
        controller needing to know driver-specific details.

        Returns:
            Tuple of (is_valid, error_message).
        """
        # Default implementation is permissive - drivers can override
        # if they need stricter validation
        # Note: physical_format is NOT required at open time for all drivers
        return True, None

    def validate_for_opening(self, source: str, **kwargs) -> Tuple[bool, Optional[str]]:
        """
        Validates whether this driver can open the specified source.

        This method checks file existence, format compatibility, and any
        driver-specific requirements before attempting to open.

        Args:
            source: The path to the file or device to open.
            **kwargs: Additional driver-specific parameters.

        Returns:
            A tuple of (is_valid, error_message). If is_valid is False,
            error_message contains a description of why validation failed.
        """
        import os

        # Default implementation just checks file existence
        if not os.path.exists(source):
            return False, f"File not found: {source}"

        return True, None

    def get_format_requirements(self) -> dict:
        """
        Returns information about what format information this driver needs.

        Returns:
            A dictionary describing format requirements:
            {
                'needs_format_for_open': bool,  # Requires format info to open
                'needs_format_for_io': bool,    # Requires format info for read/write
                'can_derive_format': bool,      # Can detect format from file
                'preferred_detection_method': str  # 'embedded', 'auto', 'explicit'
            }
        """
        return {
            'needs_format_for_open': False,
            'needs_format_for_io': True,
            'can_derive_format': False,
            'preferred_detection_method': 'explicit'
        }

    def prepare_for_format_application(self, format_info: dict) -> Tuple[bool, Optional[str]]:
        """
        Prepares the driver for applying a user-specified format.

        This method validates that the format is compatible with the driver
        and performs any necessary pre-application setup.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            A tuple of (is_ready, error_message). If is_ready is False,
            error_message describes the compatibility issue.
        """
        # Default implementation accepts any format
        return True, None

def initialize_new_image(self, physical_format: PhysicalFormat,
                            profile: Optional[Any] = None) -> None:
        """
        Initializes a new blank image with the driver's specific structure.

        This is called when creating a new image file from scratch. Drivers
        that have structured formats (IMD, H17) override this to set up
        their file structure. Raw drivers (IMG) may do nothing.

        Args:
            physical_format: The physical format for the new image.
            profile: Optional FormatProfile with additional metadata.

        Raises:
            NotImplementedError: If the driver doesn't support new image creation.
        """
        if not self.supports_new_image_creation:
            raise NotImplementedError(
                f"{self.__class__.__name__} does not support creating new images"
            )
        # Default implementation does nothing - raw formats don't need initialization
