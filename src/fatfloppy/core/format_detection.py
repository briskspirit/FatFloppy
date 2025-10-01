# src/fatfloppy/core/format_detection.py
# src/fatfloppy/core/format_detection.py
"""
Provides the base classes and factory for format detection strategies.
"""
from abc import ABC, abstractmethod
from typing import Optional, Tuple, Any, Dict, ClassVar

from .physical_format import PhysicalFormat
from .format_profile import FormatProfile
from .filesystem_factory import create_filesystem
from .filesystems.fat12fs import FATVolumeInfo
from .filesystems.cpm_fs import CPMDiskParameterBlock
from .filesystems.hdos_fs import HDOSLabelRecord
from .utils.logging_config import get_logger


class FormatDetector(ABC):
    """Abstract base class for format detection strategies."""
    detector_for_driver: ClassVar[str] = ""

    def __init__(self, disk, driver, known_formats: Dict[str, FormatProfile]):
        if not self.detector_for_driver and self.__class__ != MetadataBasedDetector:
            raise ValueError(f"{self.__class__.__name__} must define detector_for_driver")
        self.disk = disk
        self.driver = driver
        self.known_formats = known_formats
        self.logger = get_logger(self.__class__.__name__)

    @abstractmethod
    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Detects the disk format."""
        pass


class MetadataBasedDetector(FormatDetector):
    """
    Base class for drivers that have self-describing metadata (IMD, H17).

    These drivers already know their physical format from the file structure,
    so detection is primarily about matching to known profiles and parsing
    the filesystem.
    """

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Common detection flow for metadata-based formats."""
        if not self.driver.physical_format:
            self.logger.error(f"{self.__class__.__name__}: Driver has no physical format")
            return None, None, None

        # Use the driver's physical format as the base
        physical_format = self.driver.physical_format
        self.disk.set_geometry(physical_format)

        # Try to parse the filesystem
        parsed_fs_config = self._parse_filesystem()

        if parsed_fs_config:
            # Try to match against known profiles
            matched_profile = self._match_to_known_profile(physical_format, parsed_fs_config)
            if matched_profile:
                self.logger.info(f"Matched known profile: {matched_profile}")
                return matched_profile, parsed_fs_config, physical_format

            # No exact match, but we have valid filesystem data
            self.logger.info("Filesystem parsed, using driver's physical format")
            return None, parsed_fs_config, physical_format

        return None, None, physical_format

    def _parse_filesystem(self) -> Optional[Any]:
        """Attempts to parse the filesystem using the current geometry."""
        try:
            fs = create_filesystem(self.disk)
            if fs and fs.get_validity_score() >= fs.validity_threshold:
                config = fs.get_specific_config()

                # Apply driver-specific volume setup if needed
                if hasattr(fs, 'apply_volume_to_driver'):
                    try:
                        fs.apply_volume_to_driver()
                        self.logger.info("Applied filesystem-specific volume scheme")
                    except Exception as e:
                        self.logger.warning(f"Could not apply volumes: {e}")

                return config
        except Exception as e:
            self.logger.warning(f"Filesystem parsing failed: {e}")
        return None

    def _match_to_known_profile(self, physical_format: PhysicalFormat,
                                 fs_config: Any) -> Optional[str]:
        """Attempts to match the detected format to a known profile."""
        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue

            # Check physical format match
            if not self._physical_formats_match(profile.physical_format, physical_format):
                continue

            # Check filesystem config match
            if self._filesystem_configs_match(profile.filesystem_config, fs_config):
                return name

        return None

    def _physical_formats_match(self, profile_pf: PhysicalFormat,
                                 detected_pf: PhysicalFormat) -> bool:
        """Checks if two physical formats match."""
        return (profile_pf.cylinders == detected_pf.cylinders and
                profile_pf.heads == detected_pf.heads and
                profile_pf.bytes_per_sector == detected_pf.bytes_per_sector and
                profile_pf.get_sectors_per_track(0, 0) == detected_pf.get_sectors_per_track(0, 0))

    def _filesystem_configs_match(self, profile_config: Any, detected_config: Any) -> bool:
        """Checks if two filesystem configs match."""
        if type(profile_config) != type(detected_config):
            return False

        if isinstance(profile_config, FATVolumeInfo):
            return profile_config.total_sectors == detected_config.total_sectors

        elif isinstance(profile_config, CPMDiskParameterBlock):
            return (profile_config.spt == detected_config.spt and
                    profile_config.bsh == detected_config.bsh and
                    profile_config.dsm == detected_config.dsm and
                    profile_config.off == detected_config.off)

        elif isinstance(profile_config, HDOSLabelRecord):
            # HDOS match is primarily based on physical format
            return True

        return False


def create_format_detector(disk, driver, known_formats: Dict[str, FormatProfile],
                           **kwargs) -> FormatDetector:
    """
    Factory function to create the appropriate detector for a driver.
    """
    from .detector_registry import DetectorRegistry
    detector_class = DetectorRegistry.get_detector(driver)

    if not detector_class:
        raise ValueError(f"No format detector registered for driver type: {type(driver).__name__}")

    # Handle special constructor for Greaseweazle detector
    if detector_class.__name__ == 'GreaseweazleFormatDetector':
        drive_size = kwargs.get('drive_size', '3.5')
        return detector_class(disk, driver, known_formats, drive_size)

    return detector_class(disk, driver, known_formats)
