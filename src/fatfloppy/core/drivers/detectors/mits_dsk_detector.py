import copy
from collections import defaultdict
from typing import Any, Optional

from ...filesystem_factory import get_filesystem_class_by_type
from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat
from ...utils.logging_config import get_logger

MINIMUM_VALIDITY_SCORE = 30
EXCELLENT_MATCH_SCORE = 95
SIZE_TOLERANCE_BYTES = 1024

MITS_PHYSICAL_SECTOR_SIZE = 137
MITS_LOGICAL_SECTOR_SIZE = 128


logger = get_logger("MITSDSKDetector")


class MITSDSKDetector(FormatDetector):
    """
    Format detector for MITS Altair .DSK disk images.

    This detector implements variant testing similar to the IMG detector, but
    accounts for the MITS-specific sector format where 137-byte physical sectors
    contain 128 bytes of logical data plus metadata.

    Strategy:
    1. Calculate logical size (physical sectors contain metadata overhead)
    2. Group known formats by base geometry (C/H/S/BPS/encoding/rate)
    3. For each size-matched geometry group, test all filesystem variants
    4. Score each variant via filesystem validation
    5. Return best scoring variant above threshold

    The detector uses logical size (128 bytes per sector) for size matching since
    the MITS driver exposes 128-byte logical sectors to the filesystem layer.
    """

    detector_for_driver = "MITSDSKDriver"

    def detect(self) -> tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects format by iterating through known profiles with variant testing.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format).
        """
        logger.info("Starting MITS DSK format detection")

        if not hasattr(self.driver, "image_data"):
            logger.error("Driver has no image_data")
            return None, None, None

        physical_size = len(self.driver.image_data)
        num_sectors = physical_size // MITS_PHYSICAL_SECTOR_SIZE
        logical_size = num_sectors * MITS_LOGICAL_SECTOR_SIZE

        logger.debug(f"Physical file size: {physical_size} bytes")
        logger.debug(f"Logical data size: {logical_size} bytes ({num_sectors} sectors)")
        logger.debug(f"Known formats count: {len(self.known_formats)}")

        geometry_groups = self._group_formats_by_base_geometry()
        logger.debug(f"Found {len(geometry_groups)} geometry groups")

        best_score = 0
        best_match = None

        for geometry_key, profiles in geometry_groups.items():
            cyls, heads, spt, bps, encoding, rate = geometry_key
            expected_size = cyls * heads * spt * bps

            size_diff = abs(logical_size - expected_size)
            if size_diff > SIZE_TOLERANCE_BYTES:
                logger.debug(
                    f"Skipping geometry {cyls}C x {heads}H x {spt}S x {bps}B: "
                    f"size mismatch ({expected_size} vs {logical_size}, diff={size_diff})"
                )
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

                    if score >= EXCELLENT_MATCH_SCORE:
                        logger.info(
                            f"Excellent match found: {profile.name} (score={score})"
                        )
                        return best_match

                except Exception as e:
                    logger.debug(f"Profile {profile.name} failed: {e}")
                    continue

        if best_score >= MINIMUM_VALIDITY_SCORE:
            logger.info(f"Best match: {best_match[0]} (score={best_score})")
            return best_match

        # No shipped DPB matched. Try a config-less filesystem (the no-DPB CP/M
        # scan) on each size-matched geometry. This reads Altair CP/M disks whose
        # layout differs from every profiled DPB (e.g. Lifeboat CP/M, off=2 with a
        # non-standard block/skew config), while leaving disks that a profiled DPB
        # already detects completely unchanged.
        nodpb_match = self._detect_without_profile_dpb(geometry_groups, logical_size)
        if nodpb_match:
            logger.info(f"Config-less CP/M match: {nodpb_match[0]}")
            return nodpb_match

        logger.warning(f"No match found after checking (best score: {best_score})")
        return None, None, None

    def _detect_without_profile_dpb(
        self, geometry_groups: dict[tuple, list[FormatProfile]], logical_size: int
    ) -> Optional[tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]]:
        """
        Detects a filesystem without using any profile's fixed config.

        For each size-matched geometry, sets it and runs config-less filesystem
        auto-detection (which triggers the CP/M no-DPB scan). Returns the best
        validating match, or None.

        Args:
            geometry_groups: Geometry-keyed profile groups.
            logical_size: The disk's logical (de-framed) size in bytes.

        Returns:
            (format_name, filesystem_config, physical_format), or None.
        """
        from ...filesystem_factory import create_filesystem

        best_score = 0
        best_match = None
        for geometry_key, profiles in geometry_groups.items():
            cyls, heads, spt, bps, _encoding, _rate = geometry_key
            if abs(logical_size - cyls * heads * spt * bps) > SIZE_TOLERANCE_BYTES:
                continue
            pf = copy.deepcopy(profiles[0].physical_format)
            try:
                self.disk.set_geometry(pf)
                fs = create_filesystem(self.disk)
                if not fs:
                    continue
                score = fs.get_validity_score()
                if score >= fs.validity_threshold and score > best_score:
                    best_score = score
                    best_match = (profiles[0].name, fs.get_specific_config(), pf)
            except Exception as e:
                logger.debug(f"Config-less detection failed for {geometry_key}: {e}")
        return best_match

    def _group_formats_by_base_geometry(self) -> dict[tuple, list[FormatProfile]]:
        """
        Groups formats by base geometry, ignoring sector translation details.

        This allows testing multiple sector translation variants (e.g., different
        interleave patterns or skew tables) for the same physical geometry.

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
