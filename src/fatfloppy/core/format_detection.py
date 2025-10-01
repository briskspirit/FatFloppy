# src/fatfloppy/core/format_detection.py
"""
Provides the base classes and factory for format detection strategies.
"""
from abc import ABC, abstractmethod
from typing import Optional, Tuple, Any, Dict, List, ClassVar

from .physical_format import PhysicalFormat
from .format_profile import FormatProfile
from .filesystem_factory import create_filesystem, get_filesystem_class_by_type
from .utils.logging_config import get_logger


class FormatDetector(ABC):
    """Abstract base class for format detection strategies."""
    detector_for_driver: ClassVar[str] = ""

    def __init__(self, disk, driver, known_formats: Dict[str, FormatProfile]):
        if not self.detector_for_driver and self.__class__.__name__ not in ["MetadataBasedDetector"]:
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
    """Base class for drivers that have self-describing metadata (IMD, H17)."""

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Common detection flow for metadata-based formats."""
        if not self.driver.physical_format:
            self.logger.error(f"{self.__class__.__name__}: Driver has no physical format")
            return None, None, None

        physical_format = self.driver.physical_format
        self.disk.set_geometry(physical_format)
        parsed_fs_config = self._parse_filesystem()

        if parsed_fs_config:
            matched_profile = self._match_to_known_profile(physical_format, parsed_fs_config)
            if matched_profile:
                self.logger.info(f"Matched known profile: {matched_profile}")
                return matched_profile, parsed_fs_config, physical_format
            self.logger.info("Filesystem parsed, using driver's physical format")
            return None, parsed_fs_config, physical_format
        return None, None, physical_format

    def _parse_filesystem(self) -> Optional[Any]:
        """Attempts to parse the filesystem using the current geometry."""
        try:
            fs = create_filesystem(self.disk)
            if fs and fs.get_validity_score() >= fs.validity_threshold:
                config = fs.get_specific_config()
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

    def _match_to_known_profile(self, physical_format: PhysicalFormat, fs_config: Any) -> Optional[str]:
        """Attempts to match the detected format to a known profile."""
        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if not self._physical_formats_match(profile.physical_format, physical_format):
                continue
            # CORRECTED: Pass the profile's 'name' to the matching function.
            if self._filesystem_configs_match(name, profile.filesystem_config, fs_config):
                return name
        return None

    def _physical_formats_match(self, profile_pf: PhysicalFormat, detected_pf: PhysicalFormat) -> bool:
        """Checks if two physical formats match."""
        return (profile_pf.cylinders == detected_pf.cylinders and
                profile_pf.heads == detected_pf.heads and
                profile_pf.bytes_per_sector == detected_pf.bytes_per_sector and
                profile_pf.get_sectors_per_track(0, 0) == detected_pf.get_sectors_per_track(0, 0))

    def _filesystem_configs_match(self, profile_name: str, profile_config: Any, detected_config: Any) -> bool:
        """
        Delegates config comparison to the relevant filesystem plugin.
        """
        if type(profile_config) != type(detected_config):
            return False

        # Find the filesystem class associated with the profile using its name.
        fs_type = self.known_formats[profile_name].filesystem_type
        fs_class = get_filesystem_class_by_type(fs_type)

        if fs_class and hasattr(fs_class, 'configs_match'):
            # Let the plugin decide if the configs match
            return fs_class.configs_match(profile_config, detected_config)

        return False


def create_format_detector(disk, driver, known_formats: Dict[str, FormatProfile]) -> FormatDetector:
    """
    Factory function to create the appropriate detector for a driver.
    """
    from .detector_registry import DetectorRegistry
    detector_class = DetectorRegistry.get_detector(driver)

    if not detector_class:
        raise ValueError(f"No format detector registered for driver type: {type(driver).__name__}")

    return detector_class(disk, driver, known_formats)
