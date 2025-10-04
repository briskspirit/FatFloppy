# src/fatfloppy/core/drivers/detectors/mits_dsk_detector.py
"""
Format detector for MITS Altair .DSK disk images.

Uses variant testing identical to IMG detector, but accounts for the
MITS-specific 137-byte physical sector / 128-byte logical sector format.
"""

import copy
from typing import Optional, Tuple, Any, Dict, List
from collections import defaultdict

from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat
from ...filesystem_factory import get_filesystem_class_by_type
from ...utils.logging_config import get_logger

logger = get_logger(__name__)

# Thresholds for variant testing
MINIMUM_VALIDITY_SCORE = 30
EXCELLENT_MATCH_SCORE = 95

# MITS DSK sector size constants
MITS_PHYSICAL_SECTOR_SIZE = 137
MITS_LOGICAL_SECTOR_SIZE = 128


class MITSDSKDetector(FormatDetector):
    """
    Format detector for MITS Altair .DSK format.

    Strategy:
    1. Calculate logical size (physical sectors contain metadata)
    2. Group formats by base geometry
    3. Try all variants for size-matched geometry groups
    4. Return best scoring variant
    """

    detector_for_driver = "MITSDSKDriver"

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects format by iterating through known profiles with variant testing.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format)
        """
        logger.info("Starting MITS DSK format detection")

        if not hasattr(self.driver, 'image_data'):
            logger.error("Driver has no image_data")
            return None, None, None

        # MITS DSK stores 137-byte physical sectors but exposes 128-byte logical sectors
        # Calculate logical size for profile matching
        physical_size = len(self.driver.image_data)
        num_sectors = physical_size // MITS_PHYSICAL_SECTOR_SIZE
        logical_size = num_sectors * MITS_LOGICAL_SECTOR_SIZE

        logger.debug(f"Physical file size: {physical_size} bytes")
        logger.debug(f"Logical data size: {logical_size} bytes ({num_sectors} sectors)")
        logger.debug(f"Known formats count: {len(self.known_formats)}")

        # Group formats by base geometry
        geometry_groups = self._group_formats_by_base_geometry()
        logger.debug(f"Found {len(geometry_groups)} geometry groups")

        # Track overall best match
        best_score = 0
        best_match = None  # (profile_name, fs_config, physical_format)

        # Try each geometry group
        for geometry_key, profiles in geometry_groups.items():
            cyls, heads, spt, bps, encoding, rate = geometry_key
            expected_size = cyls * heads * spt * bps

            # Size check with tolerance - use logical size
            size_diff = abs(logical_size - expected_size)
            if size_diff > 1024:
                logger.debug(f"Skipping geometry {cyls}C x {heads}H x {spt}S x {bps}B: "
                           f"size mismatch ({expected_size} vs {logical_size}, diff={size_diff})")
                continue

            logger.debug(f"Testing geometry group: {cyls}C x {heads}H x {spt}S x {bps}B "
                        f"({len(profiles)} variants)")

            # Try each variant in this geometry group
            for profile in profiles:
                try:
                    # Set geometry
                    temp_format = copy.deepcopy(profile.physical_format)
                    self.disk.set_geometry(temp_format)

                    # Get filesystem type and create instance
                    fs_type = profile.get_filesystem_type()
                    if not fs_type:
                        logger.debug(f"Profile {profile.name}: no filesystem type")
                        continue

                    fs_class = get_filesystem_class_by_type(fs_type)
                    if not fs_class:
                        logger.debug(f"Profile {profile.name}: no filesystem class for {fs_type}")
                        continue

                    # Score this variant - pass config if available
                    if profile.filesystem_config:
                        try:
                            fs = fs_class(self.disk, config=profile.filesystem_config)
                        except TypeError:
                            # Fallback if constructor doesn't accept config
                            fs = fs_class(self.disk)
                    else:
                        fs = fs_class(self.disk)

                    score = fs.get_validity_score()
                    logger.debug(f"Profile {profile.name}: score={score}")

                    if score > best_score:
                        best_score = score
                        best_match = (profile.name, fs.get_specific_config(), temp_format)
                        logger.debug(f"New best match: {profile.name} (score={score})")

                    # Short-circuit on excellent match
                    if score >= EXCELLENT_MATCH_SCORE:
                        logger.info(f"Excellent match found: {profile.name} (score={score})")
                        return best_match

                except Exception as e:
                    logger.debug(f"Profile {profile.name} failed: {e}")
                    continue

        # Return best match if above threshold
        if best_score >= MINIMUM_VALIDITY_SCORE:
            logger.info(f"Best match: {best_match[0]} (score={best_score})")
            return best_match

        logger.warning(f"No match found after checking (best score: {best_score})")
        return None, None, None

    def _group_formats_by_base_geometry(self) -> Dict[Tuple, List[FormatProfile]]:
        """
        Groups formats by base geometry, ignoring sector translation details.

        Returns:
            Dictionary mapping (cyls, heads, spt, bps, encoding, rate) to list of profiles
        """
        groups = defaultdict(list)

        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue

            pf = profile.physical_format
            tf = pf.track_formats[0] if pf.track_formats else None
            if not tf:
                continue

            # Key by base geometry (ignoring interleave/sector_translation)
            key = (
                pf.cylinders,
                pf.heads,
                tf.sectors_per_track,
                pf.bytes_per_sector,
                tf.encoding,
                tf.rate
            )
            groups[key].append(profile)

        # Sort variants within each group for consistency
        # Priority: profiles with filesystem_config first
        for key in groups:
            groups[key].sort(key=lambda p: (
                p.filesystem_config is None,  # Profiles with config first
                p.name
            ))

        return dict(groups)
