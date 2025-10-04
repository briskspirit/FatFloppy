# src/fatfloppy/core/drivers/detectors/greaseweazle_detector.py
import copy
from typing import Optional, Tuple, Any, Dict, List
from collections import defaultdict

from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...filesystem_factory import create_filesystem, get_filesystem_class_by_type
from ...utils.logging_config import get_logger

logger = get_logger(__name__)

# Thresholds for variant testing
MINIMUM_VALIDITY_SCORE = 30
EXCELLENT_MATCH_SCORE = 95


class GreaseweazleFormatDetector(FormatDetector):
    """
    Format detector for Greaseweazle physical drives.

    Strategy:
    1. Use size hint to get default geometry
    2. Scan track 0 to refine geometry
    3. Check for second head
    4. Group profiles by base geometry
    5. Test all variants within each group
    6. Return best scoring variant
    """
    detector_for_driver = "GreaseweazleDriver"

    def __init__(self, disk, driver, known_formats: Dict[str, FormatProfile]):
        super().__init__(disk, driver, known_formats)
        # Get drive_size from the driver
        if not hasattr(driver, 'drive_size'):
            raise ValueError("GreaseweazleFormatDetector requires a driver with a 'drive_size' attribute.")
        self.drive_size = driver.drive_size

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        # Start with a default geometry based on drive size
        default_geom = self._get_default_geometry()
        temp_profile = FormatProfile("temp", "Temporary", default_geom, filesystem_config=None)
        self.disk.set_geometry(default_geom)

        # Initialize driver if needed
        if hasattr(self.driver, 'initialize') and not getattr(self.driver, 'initialized', False):
            self.driver.initialize()

        # Try track scan to refine geometry
        self._scan_initial_track()

        # Create filesystem to check for geometry hints
        self.disk.filesystem = create_filesystem(self.disk)

        # Check for second head
        has_second_head = self._check_second_head(temp_profile)

        # Filter and iterate through matching profiles with variant testing
        filtered = self._filter_profiles_by_size_and_heads(has_second_head)
        matched = self._find_matching_profile(filtered)

        if matched:
            logger.info(f"Detected profile: {matched.name}")
            return matched.name, matched.filesystem_config, self.disk.physical_format

        # Fallback to detected geometry with filesystem hints
        return self._construct_fallback(has_second_head)

    def _get_default_geometry(self) -> PhysicalFormat:
        """Returns default geometry based on drive size."""
        size_defaults = {
            "3.5": (80, 2, 18, 512, 500, "MFM", 300),
            "5.25": (40, 2, 9, 512, 250, "MFM", 300),
            "8": (77, 2, 26, 128, 250, "FM", 360),
        }

        cyls, heads, spt, bps, rate, enc, rpm = size_defaults.get(
            self.drive_size, size_defaults["3.5"]
        )

        tf = TrackFormat(0, cyls - 1, 0, heads - 1, spt, enc, rate, bytes_per_sector=bps, gap3_bytes=84)
        return PhysicalFormat(cyls, heads, rpm, False, bps, [tf])

    def _scan_initial_track(self):
        """Scans track 0 to detect actual geometry."""
        if not hasattr(self.driver, '_read_track'):
            return

        try:
            # Temporarily disable custom diskdef for scanning
            original_fmt_cls = getattr(self.driver, 'fmt_cls', None)
            original_custom = getattr(self.driver, 'using_custom_diskdef', False)

            self.driver.fmt_cls = None
            self.driver.using_custom_diskdef = False

            if self.driver._read_track(0, 0):
                if self.driver.physical_format and self.driver.physical_format != self.disk.physical_format:
                    logger.info(f"Track scan refined geometry: {self.driver.physical_format}")
                    self.disk.set_geometry(self.driver.physical_format)

                    # IMPORTANT: Create a NEW diskdef based on refined geometry
                    # Don't restore the old one!
                    self.driver._create_and_set_custom_diskdef()
                else:
                    # No geometry change - restore original settings
                    self.driver.fmt_cls = original_fmt_cls
                    self.driver.using_custom_diskdef = original_custom
                    if self.driver.fmt_cls and self.driver.using_custom_diskdef:
                        self.driver._create_and_set_custom_diskdef()
            else:
                # Scan failed - restore original settings
                self.driver.fmt_cls = original_fmt_cls
                self.driver.using_custom_diskdef = original_custom
                if self.driver.fmt_cls and self.driver.using_custom_diskdef:
                    self.driver._create_and_set_custom_diskdef()

        except Exception as e:
            logger.error(f"Track scan failed: {e}")

    def _check_second_head(self, temp_profile: FormatProfile) -> bool:
        """Checks if a second head is present."""
        # Check filesystem config first
        if self.disk.filesystem:
            config = self.disk.filesystem.get_specific_config()
            if hasattr(config, 'num_heads') and isinstance(config.num_heads, int) and config.num_heads > 0:
                return config.num_heads > 1

        # Physical test
        current_format = copy.deepcopy(self.disk.physical_format)
        self.disk.set_geometry(temp_profile.physical_format)

        try:
            if hasattr(self.driver, '_read_track'):
                result = bool(self.driver._read_track(0, 1))
            else:
                self.disk.read_sector(0, 1, 1)
                result = True
        except Exception:
            result = False
        finally:
            if current_format:
                self.disk.set_geometry(current_format)

        return result

    def _filter_profiles_by_size_and_heads(self, has_second_head: bool) -> List[FormatProfile]:
        """Filters profiles by drive size and head count."""
        size_str = f"{self.drive_size}\""
        filtered = []

        for profile in self.known_formats.values():
            if size_str not in profile.description:
                continue
            if not has_second_head and profile.physical_format.heads > 1:
                continue
            filtered.append(profile)

        logger.debug(f"Filtered to {len(filtered)} profiles")
        return filtered

    def _find_matching_profile(self, profiles: List[FormatProfile]) -> Optional[FormatProfile]:
        """
        Finds the best matching profile using variant testing.

        Groups profiles by base geometry and tests all variants within each group.
        """
        original_format = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        # Get detected sectors per track to prioritize matching profiles
        detected_spt = original_format.track_formats[0].sectors_per_track if original_format else None

        # Group profiles by base geometry
        geometry_groups = self._group_profiles_by_geometry(profiles)
        logger.debug(f"Grouped into {len(geometry_groups)} geometry groups")

        # Track overall best match
        best_score = 0
        best_profile = None

        # Try each geometry group
        for geometry_key, group_profiles in geometry_groups.items():
            cyls, heads, spt, bps, encoding, rate = geometry_key

            logger.debug(f"Testing geometry group: {cyls}C x {heads}H x {spt}S x {bps}B "
                        f"({len(group_profiles)} variants)")

            # Prioritize exact SPT matches within this group
            if detected_spt and detected_spt == spt:
                logger.debug(f"This group matches detected SPT={detected_spt}")

            # Try each variant in this group
            for profile in group_profiles:
                logger.debug(f"Trying profile: {profile.name}")
                self.disk.set_geometry(profile.physical_format)

                try:
                    fs_type = profile.get_filesystem_type()
                    if not fs_type:
                        logger.debug(f"Profile {profile.name}: no filesystem type")
                        continue

                    fs_class = get_filesystem_class_by_type(fs_type)
                    if not fs_class:
                        logger.debug(f"Profile {profile.name}: no filesystem class for {fs_type}")
                        continue

                    # Create filesystem with config if available
                    if profile.filesystem_config:
                        try:
                            fs = fs_class(self.disk, config=profile.filesystem_config)
                        except TypeError:
                            fs = fs_class(self.disk)
                    else:
                        fs = fs_class(self.disk)

                    score = fs.get_validity_score()
                    logger.debug(f"Profile {profile.name}: score={score}")

                    if score < fs.validity_threshold:
                        continue

                    # Generic config consistency check using the filesystem's own method
                    if profile.filesystem_config and hasattr(fs_class, 'configs_match'):
                        current_config = fs.get_specific_config()
                        if current_config and not fs_class.configs_match(profile.filesystem_config, current_config):
                            logger.debug(f"Profile {profile.name}: config consistency check failed")
                            continue

                    # Track best score
                    if score > best_score:
                        best_score = score
                        best_profile = profile
                        logger.debug(f"New best match: {profile.name} (score={score})")

                    # Short-circuit on excellent match
                    if score >= EXCELLENT_MATCH_SCORE:
                        logger.info(f"Excellent match: {profile.name} (score={score})")
                        return profile

                except Exception as e:
                    logger.debug(f"Profile {profile.name} failed: {e}")

                # Restore state after failed match
                if original_format:
                    self.disk.set_geometry(original_format)

        # Return best match if above threshold
        if best_score >= MINIMUM_VALIDITY_SCORE:
            logger.info(f"Best match: {best_profile.name} (score={best_score})")
            return best_profile

        return None

    def _group_profiles_by_geometry(self, profiles: List[FormatProfile]) -> Dict[Tuple, List[FormatProfile]]:
        """
        Groups profiles by base geometry for variant testing.

        Returns:
            Dictionary mapping (cyls, heads, spt, bps, encoding, rate) to list of profiles
        """
        groups = defaultdict(list)

        for profile in profiles:
            if not profile.physical_format:
                continue

            pf = profile.physical_format
            tf = pf.track_formats[0] if pf.track_formats else None
            if not tf:
                continue

            # Key by base geometry
            key = (
                pf.cylinders,
                pf.heads,
                tf.sectors_per_track,
                pf.bytes_per_sector,
                tf.encoding,
                tf.rate
            )
            groups[key].append(profile)

        # Sort variants within each group
        for key in groups:
            groups[key].sort(key=lambda p: (
                p.filesystem_config is None,  # Profiles with config first
                p.name
            ))

        return dict(groups)

    def _construct_fallback(self, has_second_head: bool) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Constructs a fallback format based on detected parameters."""
        base_format = self.disk.physical_format if self.disk.physical_format else self._get_default_geometry()

        # Get hints from filesystem if available
        fs_config = None
        if self.disk.filesystem:
            fs_config = self.disk.filesystem.get_specific_config()
            # Use duck typing to check for FAT-like config
            if (hasattr(fs_config, 'num_heads') and hasattr(fs_config, 'sectors_per_track') and
                hasattr(fs_config, 'bytes_per_sector') and hasattr(fs_config, 'total_sectors') and
                hasattr(fs_config, 'is_valid') and callable(fs_config.is_valid) and fs_config.is_valid()):

                heads = fs_config.num_heads if fs_config.num_heads > 0 else (2 if has_second_head else 1)
                spt = fs_config.sectors_per_track if fs_config.sectors_per_track > 0 else base_format.track_formats[0].sectors_per_track
                bps = fs_config.bytes_per_sector if fs_config.bytes_per_sector > 0 else base_format.bytes_per_sector

                if heads > 0 and spt > 0 and fs_config.total_sectors > 0:
                    cyls = fs_config.total_sectors // (heads * spt)

                    tf = TrackFormat(
                        0, cyls - 1, 0, heads - 1, spt,
                        base_format.track_formats[0].encoding,
                        base_format.track_formats[0].rate,
                        bytes_per_sector=bps
                    )
                    fallback_pf = PhysicalFormat(cyls, heads, base_format.rpm, base_format.heads_inverted, bps, [tf])
                    self.disk.set_geometry(fallback_pf)

                    logger.info("Constructed fallback from filesystem hints")
                    return None, fs_config, fallback_pf

        logger.info("Using detected geometry as fallback")
        return None, fs_config, base_format
