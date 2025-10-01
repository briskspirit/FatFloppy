# src/fatfloppy/core/format_application.py
"""
Format application strategies for different driver types.

Each driver category has its own application strategy that understands how to
apply format information to that specific driver type.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple
import copy

from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .drivers.base_driver import DiskIODriver
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class FormatApplier(ABC):
    """Abstract base class for format application strategies."""

    def __init__(self, driver: DiskIODriver, disk: Any):
        """
        Initializes the format applier.

        Args:
            driver: The DiskIODriver instance to apply format to.
            disk: The Disk object associated with the driver.
        """
        self.driver = driver
        self.disk = disk
        self.logger = get_logger(self.__class__.__name__)

    @abstractmethod
    def apply_format(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Applies format information to the driver and disk.

        Args:
            format_info: Dictionary containing format parameters.
                May contain 'format_name', 'physical_format', or custom geometry params.

        Returns:
            Tuple of (success, error_message). If success is False,
            error_message describes what went wrong.
        """
        pass

    @abstractmethod
    def can_format_new_image(self) -> bool:
        """
        Checks if this applier can format new images.

        Returns:
            True if new image creation is supported.
        """
        pass

    @abstractmethod
    def validate_format_compatibility(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validates that the format is compatible with the driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_compatible, error_message).
        """
        pass

    def _resolve_physical_format(
        self,
        format_info: Dict[str, Any],
        known_formats: Dict[str, FormatProfile]
    ) -> Optional[PhysicalFormat]:
        """
        Resolves a PhysicalFormat from format_info.

        This handles both named formats and custom geometry specifications.

        Args:
            format_info: Dictionary containing format parameters.
            known_formats: Dictionary of known format profiles.

        Returns:
            A PhysicalFormat object, or None if resolution fails.
        """
        # Direct physical_format provided
        if 'physical_format' in format_info:
            return copy.deepcopy(format_info['physical_format'])

        # Named format
        format_name = format_info.get('format_name')
        if format_name and format_name in known_formats:
            profile = known_formats[format_name]
            if profile.physical_format:
                physical_format = copy.deepcopy(profile.physical_format)

                # Attach filesystem config if present
                if profile.filesystem_config:
                    setattr(physical_format, '_associated_filesystem_config',
                           profile.filesystem_config)

                return physical_format

        # Custom geometry parameters - delegate to helper
        if any(k in format_info for k in ['cylinders', 'heads', 'sectors_per_track']):
            return self._build_physical_format_from_params(format_info)

        return None

    def _build_physical_format_from_params(self, format_info: Dict[str, Any]) -> PhysicalFormat:
        """
        Builds a PhysicalFormat from individual geometry parameters.

        Args:
            format_info: Dictionary containing geometry parameters.

        Returns:
            A constructed PhysicalFormat object.
        """
        # Extract parameters with defaults
        cylinders = format_info.get('cylinders', 80)
        heads = format_info.get('heads', 2)
        sectors_per_track = format_info.get('sectors_per_track', 18)
        bytes_per_sector = format_info.get('bytes_per_sector', 512)
        encoding = format_info.get('encoding', 'MFM')
        rate = format_info.get('rate', 500)
        rpm = format_info.get('rpm', 300)
        interleave = format_info.get('interleave', 1)
        id_start = format_info.get('id_start', 1)
        iam_present = format_info.get('iam_present', True)
        gap3_bytes = format_info.get('gap3_bytes', 84)
        heads_inverted = format_info.get('heads_inverted', False)

        track_format = TrackFormat(
            track_start=0,
            track_end=cylinders - 1,
            head_start=0,
            head_end=heads - 1,
            sectors_per_track=sectors_per_track,
            encoding=encoding,
            rate=rate,
            interleave=interleave,
            bytes_per_sector=bytes_per_sector,
            id_start=id_start,
            iam_present=iam_present,
            gap3_bytes=gap3_bytes
        )

        return PhysicalFormat(
            cylinders=cylinders,
            heads=heads,
            rpm=rpm,
            heads_inverted=heads_inverted,
            bytes_per_sector=bytes_per_sector,
            track_formats=[track_format]
        )


class MetadataBasedFormatApplier(FormatApplier):
    """
    Format applier for metadata-based drivers (IMD, H17).

    These drivers have self-describing formats embedded in the file.
    External format application should be rare and typically only for overrides.
    """

    def apply_format(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Applies format to a metadata-based driver.

        For these drivers, format application is usually a warning case since
        the format is embedded in the file itself.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (success, error_message).
        """
        self.logger.info(f"Applying format to {self.driver.__class__.__name__}")

        # Validate compatibility
        is_compatible, error = self.validate_format_compatibility(format_info)
        if not is_compatible:
            return False, error

        # Check if driver can accept the format
        is_ready, warning = self.driver.prepare_for_format_application(format_info)
        if not is_ready:
            return False, warning

        # Log warning if present
        if warning:
            self.logger.warning(warning)

        # Resolve physical format
        from .format_definitions import FLOPPY_FORMATS
        physical_format = self._resolve_physical_format(format_info, FLOPPY_FORMATS)

        if not physical_format:
            return False, "Could not resolve physical format from provided information"

        # Apply to driver and disk
        try:
            self.driver.set_physical_format(physical_format)
            self.disk.set_geometry(physical_format)

            self.logger.info(f"Format applied: {physical_format.cylinders}C x "
                           f"{physical_format.heads}H x "
                           f"{physical_format.track_formats[0].sectors_per_track}S")

            return True, None

        except Exception as e:
            error_msg = f"Failed to apply format: {e}"
            self.logger.error(error_msg)
            return False, error_msg

    def can_format_new_image(self) -> bool:
        """Metadata-based drivers can create new formatted images."""
        return self.driver.supports_new_image_creation

    def validate_format_compatibility(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validates format compatibility for metadata-based drivers.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_compatible, error_message).
        """
        # Check if we have any format information
        has_format = any(k in format_info for k in
                        ['format_name', 'physical_format', 'cylinders'])

        if not has_format:
            return False, "No format information provided"

        # Metadata-based drivers should warn if overriding embedded format
        if self.driver.has_embedded_geometry and not format_info.get('force_override', False):
            warning = (f"{self.driver.__class__.__name__} has embedded geometry. "
                      f"External format will override file metadata.")
            self.logger.warning(warning)

        return True, None


class RawImageFormatApplier(FormatApplier):
    """
    Format applier for raw image drivers (IMG).

    These drivers require external format information since the image file
    is just a flat binary blob.
    """

    def apply_format(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Applies format to a raw image driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (success, error_message).
        """
        self.logger.info(f"Applying format to {self.driver.__class__.__name__}")

        # Validate compatibility
        is_compatible, error = self.validate_format_compatibility(format_info)
        if not is_compatible:
            return False, error

        # Check driver readiness
        is_ready, warning = self.driver.prepare_for_format_application(format_info)
        if not is_ready:
            return False, warning

        if warning:
            self.logger.warning(warning)

        # Resolve physical format
        from .format_definitions import FLOPPY_FORMATS
        physical_format = self._resolve_physical_format(format_info, FLOPPY_FORMATS)

        if not physical_format:
            return False, "Could not resolve physical format from provided information"

        # Apply to driver and disk
        try:
            self.driver.set_physical_format(physical_format)
            self.disk.set_geometry(physical_format)

            self.logger.info(f"Format applied: {physical_format.cylinders}C x "
                           f"{physical_format.heads}H x "
                           f"{physical_format.track_formats[0].sectors_per_track}S")

            return True, None

        except Exception as e:
            error_msg = f"Failed to apply format: {e}"
            self.logger.error(error_msg)
            return False, error_msg

    def can_format_new_image(self) -> bool:
        """Raw image drivers can create new blank images."""
        return self.driver.supports_new_image_creation

    def validate_format_compatibility(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validates format compatibility for raw image drivers.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_compatible, error_message).
        """
        # Raw drivers REQUIRE format information
        has_format = any(k in format_info for k in
                        ['format_name', 'physical_format', 'cylinders'])

        if not has_format:
            return False, "Raw image drivers require format information for I/O operations"

        # Check image size if driver has data loaded
        if hasattr(self.driver, 'image_data') and self.driver.image_data:
            from .format_definitions import FLOPPY_FORMATS
            physical_format = self._resolve_physical_format(format_info, FLOPPY_FORMATS)

            if physical_format:
                expected_size = physical_format.total_bytes
                actual_size = len(self.driver.image_data)

                # Allow some tolerance for alignment
                if abs(expected_size - actual_size) > 1024:
                    warning = (f"Image size ({actual_size}) differs significantly from "
                             f"format size ({expected_size}). This may indicate a mismatch.")
                    self.logger.warning(warning)

        return True, None


class PhysicalDriveFormatApplier(FormatApplier):
    """
    Format applier for physical drive drivers (Greaseweazle).

    These drivers access real hardware and can auto-detect formats,
    but can also accept explicit format specifications.
    """

    def apply_format(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Applies format to a physical drive driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (success, error_message).
        """
        self.logger.info(f"Applying format to {self.driver.__class__.__name__}")

        # Validate compatibility
        is_compatible, error = self.validate_format_compatibility(format_info)
        if not is_compatible:
            return False, error

        # Check driver readiness
        is_ready, warning = self.driver.prepare_for_format_application(format_info)
        if not is_ready:
            return False, warning

        if warning:
            self.logger.warning(warning)

        # Resolve physical format
        from .format_definitions import FLOPPY_FORMATS
        physical_format = self._resolve_physical_format(format_info, FLOPPY_FORMATS)

        if not physical_format:
            return False, "Could not resolve physical format from provided information"

        # Apply to driver and disk
        try:
            self.driver.set_physical_format(physical_format)
            self.disk.set_geometry(physical_format)

            # Greaseweazle-specific: create custom diskdef
            if hasattr(self.driver, '_create_and_set_custom_diskdef'):
                self.driver._create_and_set_custom_diskdef()
                self.logger.debug("Created custom Greaseweazle diskdef")

            self.logger.info(f"Format applied: {physical_format.cylinders}C x "
                           f"{physical_format.heads}H x "
                           f"{physical_format.track_formats[0].sectors_per_track}S")

            return True, None

        except Exception as e:
            error_msg = f"Failed to apply format: {e}"
            self.logger.error(error_msg)
            return False, error_msg

    def can_format_new_image(self) -> bool:
        """Physical drives don't create images, they format physical media."""
        return False

    def validate_format_compatibility(self, format_info: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validates format compatibility for physical drive drivers.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_compatible, error_message).
        """
        has_format = any(k in format_info for k in
                        ['format_name', 'physical_format', 'cylinders'])

        if not has_format:
            # Physical drives can auto-detect, so no format is acceptable
            self.logger.info("No explicit format provided, will rely on auto-detection")
            return True, None

        # Validate physical compatibility with drive
        from .format_definitions import FLOPPY_FORMATS
        physical_format = self._resolve_physical_format(format_info, FLOPPY_FORMATS)

        if physical_format:
            # Check cylinder count vs drive size
            drive_size = getattr(self.driver, 'drive_size', '3.5')
            max_cylinders = {'3.5': 84, '5.25': 84, '8': 80}.get(drive_size, 84)

            if physical_format.cylinders > max_cylinders:
                return False, (f"Format requires {physical_format.cylinders} cylinders, "
                             f"but {drive_size}\" drives support maximum {max_cylinders}")

        return True, None


def create_format_applier(driver: DiskIODriver, disk: Any) -> FormatApplier:
    """
    Factory function to create the appropriate format applier for a driver.

    Args:
        driver: The DiskIODriver instance.
        disk: The Disk object associated with the driver.

    Returns:
        An appropriate FormatApplier subclass instance.

    Raises:
        ValueError: If the driver category is unknown.
    """
    from .format_applier_registry import FormatApplierRegistry

    category = driver.driver_category
    applier_class = FormatApplierRegistry.get(category)

    if not applier_class:
        available = FormatApplierRegistry.list_registered_categories()
        raise ValueError(
            f"No format applier registered for category '{category}'. "
            f"Available: {available}"
        )

    return applier_class(driver, disk)
