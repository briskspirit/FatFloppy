import copy
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from ...filesystem_factory import get_filesystem_class_by_type
from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...utils.logging_config import get_logger

MINIMUM_VALIDITY_SCORE = 30
EXCELLENT_MATCH_SCORE = 95
SIZE_TOLERANCE_BYTES = 1024

GENERIC_CYLINDERS = 80
GENERIC_HEADS = 2
GENERIC_SPT = 18
GENERIC_BPS = 512
GENERIC_RATE = 500
GENERIC_ENCODING = "MFM"
GENERIC_RPM = 300
GENERIC_INTERLEAVE = 1
GENERIC_GAP3_BYTES = 84

GENERIC_CYLINDERS_RANGE_START = 0
GENERIC_CYLINDERS_RANGE_END = 79
GENERIC_HEADS_RANGE_START = 0
GENERIC_HEADS_RANGE_END = 1

FALLBACK_FORMAT_1_44M = "ibm_3.5_1.44m"
FALLBACK_FORMAT_720K = "ibm_3.5_720k"
FALLBACK_FORMAT_360K = "ibm_5.25_360k"

FILESYSTEM_TYPE_FAT12 = "FAT12"


logger = get_logger("IMGFormatDetector")


class IMGFormatDetector(FormatDetector):
    """
    Format detector for raw IMG files with physical format variant testing.

    This detector implements a three-phase detection strategy:

    Phase 1: Direct BPB Parse
        - Sets a generic geometry
        - Attempts to parse FAT BPB directly
        - Matches parsed config to known profiles
        - Refines geometry from BPB if valid

    Phase 2: Variant Testing
        - Groups formats by base geometry (C/H/S/BPS/encoding/rate)
        - For each size-matched geometry group, tests all sector translation variants
        - Scores each variant via filesystem validation
        - Returns best scoring variant above threshold

    Phase 3: Generic Fallback
        - Matches file size to standard formats (1.44M, 720K, 360K)
        - Falls back to generic 1.44M geometry if no match

    The detector prioritizes profiles with filesystem_config and uses filesystem
    validity scores to distinguish between variants with the same base geometry.
    """

    detector_for_driver = "IMGImageDriver"

    def detect(self) -> tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects the format of a raw IMG file.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format).
        """
        logger.info("Starting IMG format detection")

        initial_format = (
            copy.deepcopy(self.disk.physical_format)
            if self.disk.physical_format
            else None
        )

        logger.debug("Phase 1: Direct BPB parse")
        result = self._try_direct_bpb_parse()
        if result[0] or result[1]:
            logger.info(f"Phase 1 succeeded: {result[0] or 'filesystem detected'}")
            return result

        if initial_format:
            self.disk.set_geometry(initial_format)

        logger.debug("Phase 2: Variant testing")
        result = self._try_variant_testing()
        if result[0] or result[1]:
            logger.info(f"Phase 2 succeeded: {result[0] or 'filesystem detected'}")
            return result

        logger.debug("Phase 3: Generic fallback")
        result = self._apply_generic_fallback(initial_format)
        logger.warning(f"Using fallback: {result[0] or 'generic geometry'}")
        return result

    def _apply_generic_fallback(
        self, _initial_format: Optional[PhysicalFormat]
    ) -> tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Applies a generic fallback geometry based on standard sizes.

        Args:
            _initial_format: The initial format before detection, unused.

        Returns:
            Tuple of (format_name, None, physical_format).
        """
        if hasattr(self.driver, "file_path") and Path(self.driver.file_path).exists():
            file_size = Path(self.driver.file_path).stat().st_size

            for name in [
                FALLBACK_FORMAT_1_44M,
                FALLBACK_FORMAT_720K,
                FALLBACK_FORMAT_360K,
            ]:
                profile = self.known_formats.get(name)
                if profile and profile.physical_format.total_bytes == file_size:
                    self.disk.set_geometry(profile.physical_format)
                    logger.info(f"Generic fallback matched: {name}")
                    return name, None, profile.physical_format

        tf = TrackFormat(
            GENERIC_CYLINDERS_RANGE_START,
            GENERIC_CYLINDERS_RANGE_END,
            GENERIC_HEADS_RANGE_START,
            GENERIC_HEADS_RANGE_END,
            GENERIC_SPT,
            GENERIC_ENCODING,
            GENERIC_RATE,
            GENERIC_INTERLEAVE,
        )
        pf = PhysicalFormat(
            GENERIC_CYLINDERS, GENERIC_HEADS, GENERIC_RPM, False, GENERIC_BPS, [tf]
        )
        self.disk.set_geometry(pf)
        logger.warning("Using generic 1.44M fallback geometry")
        return None, None, pf

    def _group_formats_by_base_geometry(self) -> dict[tuple, list[FormatProfile]]:
        """
        Groups formats by base geometry, ignoring sector translation details.

        This allows testing multiple sector translation variants for the same
        physical geometry (e.g., different interleave patterns).

        Returns:
            Dictionary mapping (cyls, heads, spt, bps, encoding, rate) to list of profiles.
        """
        groups = defaultdict(list)

        for _name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue

            pf = profile.physical_format
            tf = pf.track_formats[0] if pf.track_formats else None
            if not tf:
                continue

            key = (
                pf.cylinders,
                pf.heads,
                tf.sectors_per_track,
                pf.bytes_per_sector,
                tf.encoding,
                tf.rate,
            )
            groups[key].append(profile)

        for key in groups:
            groups[key].sort(key=lambda p: (p.filesystem_config is None, p.name))

        return dict(groups)

    def _try_direct_bpb_parse(
        self,
    ) -> tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Tries to parse a FAT BPB directly using a generic geometry.

        This is a quick check that works for many FAT12 images.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format) or (None, None, None).
        """
        if not self.disk.physical_format:
            generic_tf = TrackFormat(
                GENERIC_CYLINDERS_RANGE_START,
                GENERIC_CYLINDERS_RANGE_END,
                GENERIC_HEADS_RANGE_START,
                GENERIC_HEADS_RANGE_END,
                GENERIC_SPT,
                GENERIC_ENCODING,
                GENERIC_RATE,
                GENERIC_INTERLEAVE,
                gap3_bytes=GENERIC_GAP3_BYTES,
            )
            generic_pf = PhysicalFormat(
                GENERIC_CYLINDERS,
                GENERIC_HEADS,
                GENERIC_RPM,
                False,
                GENERIC_BPS,
                [generic_tf],
            )
            self.disk.set_geometry(generic_pf)

        fat_class = get_filesystem_class_by_type(FILESYSTEM_TYPE_FAT12)
        if fat_class:
            try:
                fs = fat_class(self.disk)
                if fs.get_validity_score() < fs.validity_threshold:
                    logger.debug("Direct BPB parse: score below threshold")
                    return None, None, None

                logger.debug("Direct BPB parse successful")
                parsed_config = fs.get_specific_config()

                for name, profile in self.known_formats.items():
                    if (
                        profile.filesystem_config
                        and hasattr(profile.filesystem_config, "total_sectors")
                        and hasattr(profile.filesystem_config, "num_heads")
                        and hasattr(parsed_config, "total_sectors")
                        and hasattr(parsed_config, "num_heads")
                        and profile.physical_format
                        and profile.filesystem_config.total_sectors
                        == parsed_config.total_sectors
                        and profile.filesystem_config.num_heads
                        == parsed_config.num_heads
                    ):
                        logger.info(f"Matched profile from BPB: {name}")
                        self.disk.set_geometry(profile.physical_format)
                        return name, parsed_config, self.disk.physical_format

                if (
                    hasattr(parsed_config, "bytes_per_sector")
                    and hasattr(parsed_config, "num_heads")
                    and hasattr(parsed_config, "sectors_per_track")
                    and hasattr(parsed_config, "total_sectors")
                ) and all(
                    v > 0
                    for v in [
                        parsed_config.bytes_per_sector,
                        parsed_config.num_heads,
                        parsed_config.sectors_per_track,
                    ]
                ):
                    cyls = parsed_config.total_sectors // (
                        parsed_config.num_heads * parsed_config.sectors_per_track
                    )
                    current_pf = self.disk.physical_format

                    refined_tf = TrackFormat(
                        0,
                        cyls - 1,
                        0,
                        parsed_config.num_heads - 1,
                        parsed_config.sectors_per_track,
                        current_pf.track_formats[0].encoding,
                        current_pf.track_formats[0].rate,
                        current_pf.track_formats[0].interleave,
                    )
                    refined_pf = PhysicalFormat(
                        cyls,
                        parsed_config.num_heads,
                        current_pf.rpm,
                        current_pf.heads_inverted,
                        parsed_config.bytes_per_sector,
                        [refined_tf],
                    )
                    self.disk.set_geometry(refined_pf)
                    logger.debug("Refined geometry from BPB")
                    return None, parsed_config, refined_pf

            except Exception as e:
                logger.debug(f"Direct BPB parse failed: {e}")

        return None, None, None

    def _try_variant_testing(
        self,
    ) -> tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Iterates through geometry groups, testing all variants for each.

        Returns the best scoring variant above threshold.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format) or (None, None, None).
        """
        if not hasattr(self.driver, "image_data"):
            logger.error("Driver has no image_data")
            return None, None, None

        image_size = len(self.driver.image_data)
        logger.debug(f"Image size: {image_size} bytes")

        geometry_groups = self._group_formats_by_base_geometry()
        logger.debug(f"Found {len(geometry_groups)} geometry groups")

        best_score = 0
        best_match = None

        for geometry_key, profiles in geometry_groups.items():
            cyls, heads, spt, bps, encoding, rate = geometry_key
            expected_size = cyls * heads * spt * bps

            if abs(image_size - expected_size) > SIZE_TOLERANCE_BYTES:
                continue

            logger.debug(
                f"Testing geometry group: {cyls}C x {heads}H x {spt}S x {bps}B "
                f"({len(profiles)} variants)"
            )

            for profile in profiles:
                try:
                    temp_format = copy.deepcopy(profile.physical_format)
                    self.disk.set_geometry(temp_format)

                    fs_type = profile.get_filesystem_type()
                    if not fs_type:
                        logger.debug(f"Profile {profile.name}: no filesystem type")
                        continue

                    fs_class = get_filesystem_class_by_type(fs_type)
                    if not fs_class:
                        logger.debug(
                            f"Profile {profile.name}: no filesystem class for {fs_type}"
                        )
                        continue

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
                        best_match = (
                            profile.name,
                            fs.get_specific_config(),
                            temp_format,
                        )
                        logger.debug(f"New best match: {profile.name} (score={score})")

                except Exception as e:
                    logger.debug(f"Profile {profile.name} failed: {e}")
                    continue

        if best_score >= MINIMUM_VALIDITY_SCORE:
            logger.info(f"Best match: {best_match[0]} (score={best_score})")
            return best_match

        logger.debug(f"No match above threshold (best score: {best_score})")
        return None, None, None
