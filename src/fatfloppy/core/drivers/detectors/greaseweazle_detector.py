from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
import copy

from ...filesystem_factory import create_filesystem, get_filesystem_class_by_type
from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...utils.logging_config import get_logger


MINIMUM_VALIDITY_SCORE = 30
EXCELLENT_MATCH_SCORE = 95

DEFAULT_CYLS_3_5 = 80
DEFAULT_HEADS_3_5 = 2
DEFAULT_SPT_3_5 = 18
DEFAULT_BPS_3_5 = 512
DEFAULT_RATE_3_5 = 500
DEFAULT_ENCODING_3_5 = "MFM"
DEFAULT_RPM_3_5 = 300

DEFAULT_CYLS_5_25 = 40
DEFAULT_HEADS_5_25 = 2
DEFAULT_SPT_5_25 = 9
DEFAULT_BPS_5_25 = 512
DEFAULT_RATE_5_25 = 250
DEFAULT_ENCODING_5_25 = "MFM"
DEFAULT_RPM_5_25 = 300

DEFAULT_CYLS_8 = 77
DEFAULT_HEADS_8 = 2
DEFAULT_SPT_8 = 26
DEFAULT_BPS_8 = 128
DEFAULT_RATE_8 = 250
DEFAULT_ENCODING_8 = "FM"
DEFAULT_RPM_8 = 360

DRIVE_SIZE_3_5 = "3.5"
DRIVE_SIZE_5_25 = "5.25"
DRIVE_SIZE_8 = "8"

DEFAULT_GAP3_BYTES = 84

SCAN_CYLINDER = 0
SCAN_HEAD_PRIMARY = 0
SCAN_HEAD_SECONDARY = 1
SCAN_SECTOR = 1


logger = get_logger("GreaseweazleFormatDetector")


class GreaseweazleFormatDetector(FormatDetector):
    """
    Format detector for Greaseweazle physical drives.

    This detector uses a multi-stage approach to identify disk formats:
    1. Uses drive size hint to get default geometry
    2. Scans track 0 to refine geometry based on actual disk
    3. Checks for presence of second head
    4. Groups format profiles by base geometry
    5. Tests all filesystem variants within each geometry group
    6. Returns the best scoring variant based on filesystem validity

    The detector prioritizes profiles that match the detected sectors per track
    and uses filesystem validity scores to distinguish between variants with
    the same base geometry.
    """

    detector_for_driver = "GreaseweazleDriver"

    def __init__(self, disk, driver, known_formats: Dict[str, FormatProfile]):
        """
        Initializes the Greaseweazle format detector.

        Args:
            disk: The disk object to detect format for.
            driver: The GreaseweazleDriver instance.
            known_formats: Dictionary of known format profiles.

        Raises:
            ValueError: If driver does not have 'drive_size' attribute.
        """
        super().__init__(disk, driver, known_formats)
        if not hasattr(driver, "drive_size"):
            raise ValueError(
                "GreaseweazleFormatDetector requires a driver with a 'drive_size' attribute."
            )
        self.drive_size = driver.drive_size

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects the disk format using multi-stage detection.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format).
        """
        default_geom = self._get_default_geometry()
        temp_profile = FormatProfile(
            "temp", "Temporary", default_geom, filesystem_config=None
        )
        self.disk.set_geometry(default_geom)

        if hasattr(self.driver, "initialize") and not getattr(
            self.driver, "initialized", False
        ):
            self.driver.initialize()

        self._scan_initial_track()

        self.disk.filesystem = create_filesystem(self.disk)

        has_second_head = self._check_second_head(temp_profile)

        filtered = self._filter_profiles_by_size_and_heads(has_second_head)
        matched = self._find_matching_profile(filtered)

        if matched:
            logger.info(f"Detected profile: {matched.name}")
            return matched.name, matched.filesystem_config, self.disk.physical_format

        return self._construct_fallback(has_second_head)

    def _check_second_head(self, temp_profile: FormatProfile) -> bool:
        """
        Checks if a second head is present on the disk.

        Args:
            temp_profile: Temporary profile for testing.

        Returns:
            True if second head is detected, False otherwise.
        """
        if self.disk.filesystem:
            config = self.disk.filesystem.get_specific_config()
            if (
                hasattr(config, "num_heads")
                and isinstance(config.num_heads, int)
                and config.num_heads > 0
            ):
                return config.num_heads > 1

        current_format = copy.deepcopy(self.disk.physical_format)
        self.disk.set_geometry(temp_profile.physical_format)

        try:
            if hasattr(self.driver, "_read_track"):
                result = bool(self.driver._read_track(SCAN_CYLINDER, SCAN_HEAD_SECONDARY))
            else:
                self.disk.read_sector(SCAN_CYLINDER, SCAN_HEAD_SECONDARY, SCAN_SECTOR)
                result = True
        except Exception:
            result = False
        finally:
            if current_format:
                self.disk.set_geometry(current_format)

        return result

    def _construct_fallback(
        self, has_second_head: bool
    ) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Constructs a fallback format based on detected parameters.

        Args:
            has_second_head: Whether second head was detected.

        Returns:
            Tuple of (None, filesystem_config, physical_format).
        """
        base_format = (
            self.disk.physical_format
            if self.disk.physical_format
            else self._get_default_geometry()
        )

        fs_config = None
        if self.disk.filesystem:
            fs_config = self.disk.filesystem.get_specific_config()
            if (
                hasattr(fs_config, "num_heads")
                and hasattr(fs_config, "sectors_per_track")
                and hasattr(fs_config, "bytes_per_sector")
                and hasattr(fs_config, "total_sectors")
                and hasattr(fs_config, "is_valid")
                and callable(fs_config.is_valid)
                and fs_config.is_valid()
            ):

                heads = (
                    fs_config.num_heads
                    if fs_config.num_heads > 0
                    else (2 if has_second_head else 1)
                )
                spt = (
                    fs_config.sectors_per_track
                    if fs_config.sectors_per_track > 0
                    else base_format.track_formats[0].sectors_per_track
                )
                bps = (
                    fs_config.bytes_per_sector
                    if fs_config.bytes_per_sector > 0
                    else base_format.bytes_per_sector
                )

                if heads > 0 and spt > 0 and fs_config.total_sectors > 0:
                    cyls = fs_config.total_sectors // (heads * spt)

                    tf = TrackFormat(
                        0,
                        cyls - 1,
                        0,
                        heads - 1,
                        spt,
                        base_format.track_formats[0].encoding,
                        base_format.track_formats[0].rate,
                        bytes_per_sector=bps,
                    )
                    fallback_pf = PhysicalFormat(
                        cyls, heads, base_format.rpm, base_format.heads_inverted, bps, [tf]
                    )
                    self.disk.set_geometry(fallback_pf)

                    logger.info("Constructed fallback from filesystem hints")
                    return None, fs_config, fallback_pf

        logger.info("Using detected geometry as fallback")
        return None, fs_config, base_format

    def _filter_profiles_by_size_and_heads(
        self, has_second_head: bool
    ) -> List[FormatProfile]:
        """
        Filters profiles by drive size and head count.

        Args:
            has_second_head: Whether second head was detected.

        Returns:
            List of matching profiles.
        """
        size_str = f'{self.drive_size}"'
        filtered = []

        for profile in self.known_formats.values():
            if size_str not in profile.description:
                continue
            if not has_second_head and profile.physical_format.heads > 1:
                continue
            filtered.append(profile)

        logger.debug(f"Filtered to {len(filtered)} profiles")
        return filtered

    def _find_matching_profile(
        self, profiles: List[FormatProfile]
    ) -> Optional[FormatProfile]:
        """
        Finds the best matching profile using variant testing.

        Groups profiles by base geometry and tests all variants within each group.

        Args:
            profiles: List of candidate profiles to test.

        Returns:
            Best matching profile or None if no match above threshold.
        """
        original_format = (
            copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None
        )

        detected_spt = (
            original_format.track_formats[0].sectors_per_track if original_format else None
        )

        geometry_groups = self._group_profiles_by_geometry(profiles)
        logger.debug(f"Grouped into {len(geometry_groups)} geometry groups")

        best_score = 0
        best_profile = None

        for geometry_key, group_profiles in geometry_groups.items():
            cyls, heads, spt, bps, encoding, rate = geometry_key

            logger.debug(
                f"Testing geometry group: {cyls}C x {heads}H x {spt}S x {bps}B "
                f"({len(group_profiles)} variants)"
            )

            if detected_spt and detected_spt == spt:
                logger.debug(f"This group matches detected SPT={detected_spt}")

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

                    if score < fs.validity_threshold:
                        continue

                    if profile.filesystem_config and hasattr(fs_class, "configs_match"):
                        current_config = fs.get_specific_config()
                        if current_config and not fs_class.configs_match(
                            profile.filesystem_config, current_config
                        ):
                            logger.debug(
                                f"Profile {profile.name}: config consistency check failed"
                            )
                            continue

                    if score > best_score:
                        best_score = score
                        best_profile = profile
                        logger.debug(f"New best match: {profile.name} (score={score})")

                    if score >= EXCELLENT_MATCH_SCORE:
                        logger.info(f"Excellent match: {profile.name} (score={score})")
                        return profile

                except Exception as e:
                    logger.debug(f"Profile {profile.name} failed: {e}")

                if original_format:
                    self.disk.set_geometry(original_format)

        if best_score >= MINIMUM_VALIDITY_SCORE:
            logger.info(f"Best match: {best_profile.name} (score={best_score})")
            return best_profile

        return None

    def _get_default_geometry(self) -> PhysicalFormat:
        """
        Returns default geometry based on drive size.

        Returns:
            PhysicalFormat with default geometry for the drive size.
        """
        size_defaults = {
            DRIVE_SIZE_3_5: (
                DEFAULT_CYLS_3_5,
                DEFAULT_HEADS_3_5,
                DEFAULT_SPT_3_5,
                DEFAULT_BPS_3_5,
                DEFAULT_RATE_3_5,
                DEFAULT_ENCODING_3_5,
                DEFAULT_RPM_3_5,
            ),
            DRIVE_SIZE_5_25: (
                DEFAULT_CYLS_5_25,
                DEFAULT_HEADS_5_25,
                DEFAULT_SPT_5_25,
                DEFAULT_BPS_5_25,
                DEFAULT_RATE_5_25,
                DEFAULT_ENCODING_5_25,
                DEFAULT_RPM_5_25,
            ),
            DRIVE_SIZE_8: (
                DEFAULT_CYLS_8,
                DEFAULT_HEADS_8,
                DEFAULT_SPT_8,
                DEFAULT_BPS_8,
                DEFAULT_RATE_8,
                DEFAULT_ENCODING_8,
                DEFAULT_RPM_8,
            ),
        }

        cyls, heads, spt, bps, rate, enc, rpm = size_defaults.get(
            self.drive_size, size_defaults[DRIVE_SIZE_3_5]
        )

        tf = TrackFormat(
            0, cyls - 1, 0, heads - 1, spt, enc, rate, bytes_per_sector=bps, gap3_bytes=DEFAULT_GAP3_BYTES
        )
        return PhysicalFormat(cyls, heads, rpm, False, bps, [tf])

    def _group_profiles_by_geometry(
        self, profiles: List[FormatProfile]
    ) -> Dict[Tuple, List[FormatProfile]]:
        """
        Groups profiles by base geometry for variant testing.

        Args:
            profiles: List of profiles to group.

        Returns:
            Dictionary mapping (cyls, heads, spt, bps, encoding, rate) to list of profiles.
        """
        groups = defaultdict(list)

        for profile in profiles:
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

    def _scan_initial_track(self):
        """
        Scans track 0 to detect actual geometry.

        This method temporarily disables custom diskdef to allow scanning,
        then updates the disk geometry based on scan results and recreates
        the diskdef with the refined geometry.
        """
        if not hasattr(self.driver, "_read_track"):
            return

        try:
            original_fmt_cls = getattr(self.driver, "fmt_cls", None)
            original_custom = getattr(self.driver, "using_custom_diskdef", False)

            self.driver.fmt_cls = None
            self.driver.using_custom_diskdef = False

            if self.driver._read_track(SCAN_CYLINDER, SCAN_HEAD_PRIMARY):
                if (
                    self.driver.physical_format
                    and self.driver.physical_format != self.disk.physical_format
                ):
                    logger.info(f"Track scan refined geometry: {self.driver.physical_format}")
                    self.disk.set_geometry(self.driver.physical_format)

                    self.driver._create_and_set_custom_diskdef()
                else:
                    self.driver.fmt_cls = original_fmt_cls
                    self.driver.using_custom_diskdef = original_custom
                    if self.driver.fmt_cls and self.driver.using_custom_diskdef:
                        self.driver._create_and_set_custom_diskdef()
            else:
                self.driver.fmt_cls = original_fmt_cls
                self.driver.using_custom_diskdef = original_custom
                if self.driver.fmt_cls and self.driver.using_custom_diskdef:
                    self.driver._create_and_set_custom_diskdef()

        except Exception as e:
            logger.error(f"Track scan failed: {e}")
