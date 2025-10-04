# src/fatfloppy/core/drivers/detectors/img_detector.py
"""
Format detector for raw IMG files with physical format variant testing.

This detector implements the new architecture where multiple physical format
variants (different sector translations) are tested for the same base geometry.
"""
import copy
from typing import Optional, Tuple, Any, Dict, List
from collections import defaultdict

from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...filesystem_factory import get_filesystem_class_by_type
from ...utils.logging_config import get_logger

logger = get_logger(__name__)

# Thresholds for variant testing
MINIMUM_VALIDITY_SCORE = 30  # Minimum score to consider a match
EXCELLENT_MATCH_SCORE = 95   # Score that short-circuits further testing


class IMGFormatDetector(FormatDetector):
    """
    Format detector for raw IMG files.

    Strategy:
    1. Try direct BPB parse with canonical geometry
    2. Group formats by base geometry (C/H/S/BPS/encoding/rate)
    3. For each size-matched geometry group:
       - Try all sector translation variants
       - Score each via filesystem validation
       - Pick best scoring variant
    4. Fall back to generic geometry
    """
    detector_for_driver = "IMGImageDriver"

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects the format of a raw IMG file.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format)
        """
        logger.info("Starting IMG format detection")

        initial_format = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        # Phase 1: Try direct BPB parse
        logger.debug("Phase 1: Direct BPB parse")
        result = self._try_direct_bpb_parse()
        if result[0] or result[1]:  # Got profile name or filesystem config
            logger.info(f"Phase 1 succeeded: {result[0] or 'filesystem detected'}")
            return result

        # Restore state after Phase 1
        if initial_format:
            self.disk.set_geometry(initial_format)

        # Phase 2: Try variant testing with geometry groups
        logger.debug("Phase 2: Variant testing")
        result = self._try_variant_testing()
        if result[0] or result[1]:
            logger.info(f"Phase 2 succeeded: {result[0] or 'filesystem detected'}")
            return result

        # Phase 3: Fall back to generic geometry
        logger.debug("Phase 3: Generic fallback")
        result = self._apply_generic_fallback(initial_format)
        logger.warning(f"Using fallback: {result[0] or 'generic geometry'}")
        return result

    def _try_direct_bpb_parse(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Tries to parse a FAT BPB directly using a generic geometry.

        This is a quick check that works for many FAT12 images.
        """
        # Set a generic geometry for parsing
        if not self.disk.physical_format:
            generic_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1, gap3_bytes=84)
            generic_pf = PhysicalFormat(80, 2, 300, False, 512, [generic_tf])
            self.disk.set_geometry(generic_pf)

        # Try FAT filesystem first (most common for IMG files)
        fat_class = get_filesystem_class_by_type("FAT12")
        if fat_class:
            try:
                fs = fat_class(self.disk)
                if fs.get_validity_score() < fs.validity_threshold:
                    logger.debug("Direct BPB parse: score below threshold")
                    return None, None, None

                logger.debug("Direct BPB parse successful")
                parsed_config = fs.get_specific_config()

                # Try to match to a known profile using duck typing
                for name, profile in self.known_formats.items():
                    if (profile.filesystem_config and
                        hasattr(profile.filesystem_config, 'total_sectors') and
                        hasattr(profile.filesystem_config, 'num_heads') and
                        hasattr(parsed_config, 'total_sectors') and
                        hasattr(parsed_config, 'num_heads') and
                        profile.physical_format and
                        profile.filesystem_config.total_sectors == parsed_config.total_sectors and
                        profile.filesystem_config.num_heads == parsed_config.num_heads):

                        logger.info(f"Matched profile from BPB: {name}")
                        self.disk.set_geometry(profile.physical_format)
                        return name, parsed_config, self.disk.physical_format

                # Valid config but no profile match - refine geometry
                if (hasattr(parsed_config, 'bytes_per_sector') and
                    hasattr(parsed_config, 'num_heads') and
                    hasattr(parsed_config, 'sectors_per_track') and
                    hasattr(parsed_config, 'total_sectors')):

                    if all(v > 0 for v in [parsed_config.bytes_per_sector, parsed_config.num_heads,
                                           parsed_config.sectors_per_track]):
                        cyls = parsed_config.total_sectors // (parsed_config.num_heads * parsed_config.sectors_per_track)
                        current_pf = self.disk.physical_format

                        refined_tf = TrackFormat(
                            0, cyls - 1, 0, parsed_config.num_heads - 1,
                            parsed_config.sectors_per_track,
                            current_pf.track_formats[0].encoding,
                            current_pf.track_formats[0].rate,
                            current_pf.track_formats[0].interleave
                        )
                        refined_pf = PhysicalFormat(
                            cyls, parsed_config.num_heads, current_pf.rpm,
                            current_pf.heads_inverted, parsed_config.bytes_per_sector,
                            [refined_tf]
                        )
                        self.disk.set_geometry(refined_pf)
                        logger.debug("Refined geometry from BPB")
                        return None, parsed_config, refined_pf

            except Exception as e:
                logger.debug(f"Direct BPB parse failed: {e}")

        return None, None, None

    def _try_variant_testing(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Iterates through geometry groups, testing all variants for each.

        Returns the best scoring variant above threshold.
        """
        if not hasattr(self.driver, 'image_data'):
            logger.error("Driver has no image_data")
            return None, None, None

        image_size = len(self.driver.image_data)
        logger.debug(f"Image size: {image_size} bytes")

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

            # Quick size check with tolerance
            if abs(image_size - expected_size) > 1024:
                continue

            logger.debug(f"Testing geometry group: {cyls}C x {heads}H x {spt}S x {bps}B "
                        f"({len(profiles)} variants)")

            # Try EVERY variant in this geometry group - don't short-circuit!
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

                    # Score this variant
                    if profile.filesystem_config:
                        try:
                            fs = fs_class(self.disk, config=profile.filesystem_config)
                        except TypeError:
                            fs = fs_class(self.disk)
                    else:
                        fs = fs_class(self.disk)

                    score = fs.get_validity_score()

                    logger.debug(f"Profile {profile.name}: score={score}")

                    if score > best_score:
                        best_score = score
                        best_match = (profile.name, fs.get_specific_config(), temp_format)
                        logger.debug(f"New best match: {profile.name} (score={score})")

                    # DON'T short-circuit - test all variants in this group!

                except Exception as e:
                    logger.debug(f"Profile {profile.name} failed: {e}")
                    continue

        # Return best match if above threshold
        if best_score >= MINIMUM_VALIDITY_SCORE:
            logger.info(f"Best match: {best_match[0]} (score={best_score})")
            return best_match

        logger.debug(f"No match above threshold (best score: {best_score})")
        return None, None, None

    def _group_formats_by_base_geometry(self) -> Dict[Tuple, List[FormatProfile]]:
        """
        Groups formats by base geometry, ignoring sector translation details.

        This allows testing multiple sector translation variants for the same
        physical geometry (e.g., different interleave patterns).

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

    def _apply_generic_fallback(self, initial_format: Optional[PhysicalFormat]) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Applies a generic fallback geometry based on standard sizes.
        """
        # Try to match standard sizes
        if hasattr(self.driver, 'file_path'):
            import os
            if os.path.exists(self.driver.file_path):
                file_size = os.path.getsize(self.driver.file_path)

                # Try common formats in order of likelihood
                for name in ["ibm_3.5_1.44m", "ibm_3.5_720k", "ibm_5.25_360k"]:
                    profile = self.known_formats.get(name)
                    if profile and profile.physical_format.total_bytes == file_size:
                        self.disk.set_geometry(profile.physical_format)
                        logger.info(f"Generic fallback matched: {name}")
                        return name, None, profile.physical_format

        # Final fallback: 1.44M geometry
        tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1)
        pf = PhysicalFormat(80, 2, 300, False, 512, [tf])
        self.disk.set_geometry(pf)
        logger.warning("Using generic 1.44M fallback geometry")
        return None, None, pf
