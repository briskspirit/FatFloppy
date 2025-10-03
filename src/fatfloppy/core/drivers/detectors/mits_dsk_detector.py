# src/fatfloppy/core/drivers/detectors/mits_dsk_detector.py
"""
Format detector for MITS Altair .DSK disk images.

Uses profile iteration identical to IMG detector.
"""

import copy
from typing import Optional, Tuple, Any

from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat
from ...filesystem_factory import get_filesystem_class_by_type
from ...utils.logging_config import get_logger

logger = get_logger(__name__)


class MITSDSKDetector(FormatDetector):
    """Format detector for MITS Altair .DSK format."""

    detector_for_driver = "MITSDSKDriver"

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Detects format by iterating through known profiles."""
        logger.info("Starting MITS DSK format detection")

        if not hasattr(self.driver, 'image_data'):
            logger.error("Driver has no image_data")
            return None, None, None

        # MITS DSK stores 137-byte physical sectors but exposes 128-byte logical sectors
        # Calculate logical size for profile matching
        physical_size = len(self.driver.image_data)
        MITS_PHYSICAL_SECTOR_SIZE = 137
        MITS_LOGICAL_SECTOR_SIZE = 128

        num_sectors = physical_size // MITS_PHYSICAL_SECTOR_SIZE
        logical_size = num_sectors * MITS_LOGICAL_SECTOR_SIZE

        logger.debug(f"Physical file size: {physical_size} bytes")
        logger.debug(f"Logical data size: {logical_size} bytes ({num_sectors} sectors)")
        logger.debug(f"Known formats count: {len(self.known_formats)}")

        # Priority list of candidate formats
        # candidate_formats = [,] + [name for name in self.known_formats if name not in [,]]
        candidate_formats = [name for name in self.known_formats ]

        logger.debug(f"Candidate formats count: {len(candidate_formats)}")

        profiles_checked = 0
        for name in candidate_formats:
            profile = self.known_formats.get(name)
            if not profile or not profile.physical_format or not profile.filesystem_type:
                continue

            profiles_checked += 1

            # Size check with tolerance - use logical size
            expected_size = profile.physical_format.total_bytes
            size_diff = abs(logical_size - expected_size)
            if size_diff > 1024:
                logger.debug(f"Skipping {name}: size mismatch ({expected_size} vs {logical_size}, diff={size_diff})")
                continue

            logger.debug(f"Trying profile: {name}")

            try:
                temp_format = copy.deepcopy(profile.physical_format)
                if profile.filesystem_config:
                    setattr(temp_format, '_associated_filesystem_config', profile.filesystem_config)

                self.disk.set_geometry(temp_format)

                fs_class = get_filesystem_class_by_type(profile.filesystem_type)
                if fs_class:
                    fs = fs_class(self.disk)
                    if fs.get_validity_score() >= fs.validity_threshold:
                        logger.info(f"Matched profile: {name}")
                        return name, fs.get_specific_config(), temp_format

            except Exception as e:
                logger.debug(f"Profile {name} failed: {e}")
                continue

        logger.warning(f"No match found after checking {profiles_checked} profiles")
        return None, None, None
