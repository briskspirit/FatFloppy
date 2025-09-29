# src/fatfloppy/core/format_detection.py
"""
Format detection strategies for different driver types.

Each driver type has its own detection strategy that understands how to
extract format information from that specific source.
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple, Any, Dict, List
import copy

from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .filesystems.fs_base import Filesystem
from .filesystems.fat12fs import FATVolumeInfo, FATFilesystem
from .filesystems.cpm_fs import CPMDiskParameterBlock, CPMFilesystem
from .filesystems.hdos_fs import HDOSLabelRecord, HDOSFilesystem
from .filesystem_factory import create_filesystem, get_filesystem_class_by_type
from .utils.logging_config import get_logger


class FormatDetector(ABC):
    """Abstract base class for format detection strategies."""

    def __init__(self, disk, driver, known_formats: Dict[str, FormatProfile]):
        self.disk = disk
        self.driver = driver
        self.known_formats = known_formats
        self.logger = get_logger(self.__class__.__name__)

    @abstractmethod
    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects the disk format.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format)
            - format_name: Name of matched FormatProfile, or None
            - filesystem_config: Parsed filesystem config (BPB, DPB, etc.), or None
            - physical_format: The physical geometry to use
        """
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
            if fs and fs.get_validity_score() >= getattr(fs, 'VALIDITY_THRESHOLD', 30):
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


class IMDFormatDetector(MetadataBasedDetector):
    """Format detector for ImageDisk (.IMD) files."""
    pass  # Uses base implementation


class H17FormatDetector(MetadataBasedDetector):
    """Format detector for H17 (.h17disk) files."""
    pass  # Uses base implementation


class IMGFormatDetector(FormatDetector):
    """
    Format detector for raw IMG files.

    Strategy:
    1. Try direct BPB parse with canonical geometry
    2. Iterate through known profiles by size
    3. Fall back to generic geometry
    """

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        initial_format = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        # Phase 1: Try direct BPB parse
        result = self._try_direct_bpb_parse()
        if result[0] or result[1]:  # Got profile name or filesystem config
            return result

        # Restore state after Phase 1
        if initial_format:
            self.disk.set_geometry(initial_format)

        # Phase 2: Iterate through size-matched profiles
        result = self._try_profile_iteration()
        if result[0] or result[1]:
            return result

        # Phase 3: Fall back to generic geometry
        return self._apply_generic_fallback(initial_format)

    def _try_direct_bpb_parse(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Tries to parse a FAT BPB directly using a generic geometry."""
        # Set a generic geometry for parsing
        if not self.disk.physical_format:
            generic_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1, gap3_bytes=84)
            generic_pf = PhysicalFormat(80, 2, 300, False, 512, [generic_tf])
            self.disk.set_geometry(generic_pf)

        try:
            fs = FATFilesystem(self.disk)
            if fs.get_validity_score() < FATFilesystem.VALIDITY_THRESHOLD:
                return None, None, None

            self.logger.info("Direct BPB parse successful")
            parsed_bpb = fs.get_specific_config()

            # Try to match to a known profile
            for name, profile in self.known_formats.items():
                if (profile.filesystem_type == "FAT12" and
                    isinstance(profile.filesystem_config, FATVolumeInfo) and
                    profile.physical_format and
                    profile.filesystem_config.total_sectors == parsed_bpb.total_sectors and
                    profile.filesystem_config.num_heads == parsed_bpb.num_heads):

                    self.logger.info(f"Matched profile from BPB: {name}")
                    self.disk.set_geometry(profile.physical_format)
                    return name, parsed_bpb, self.disk.physical_format

            # Valid BPB but no profile match - refine geometry from BPB
            if all(v > 0 for v in [parsed_bpb.bytes_per_sector, parsed_bpb.num_heads,
                                   parsed_bpb.sectors_per_track]):
                cyls = parsed_bpb.total_sectors // (parsed_bpb.num_heads * parsed_bpb.sectors_per_track)
                current_pf = self.disk.physical_format

                refined_tf = TrackFormat(
                    0, cyls - 1, 0, parsed_bpb.num_heads - 1,
                    parsed_bpb.sectors_per_track,
                    current_pf.track_formats[0].encoding,
                    current_pf.track_formats[0].rate,
                    current_pf.track_formats[0].interleave
                )
                refined_pf = PhysicalFormat(
                    cyls, parsed_bpb.num_heads, current_pf.rpm,
                    current_pf.heads_inverted, parsed_bpb.bytes_per_sector,
                    [refined_tf]
                )
                self.disk.set_geometry(refined_pf)
                return None, parsed_bpb, refined_pf

        except Exception as e:
            self.logger.debug(f"Direct BPB parse failed: {e}")

        return None, None, None

    def _try_profile_iteration(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Iterates through known profiles, matching by image size."""
        import os

        if not hasattr(self.driver, 'image_data'):
            return None, None, None

        image_size = len(self.driver.image_data)

        # Prioritize mixed-density formats for 8" disks
        candidate_formats = [
            "cpm_8_ssdd_imsai_mixed_idorder",
            "cpm_8_ssdd_imsai_mixed",
            "cpm_8_sssd_250k",
        ] + [name for name in self.known_formats if name not in [
            "cpm_8_ssdd_imsai_mixed_idorder", "cpm_8_ssdd_imsai_mixed", "cpm_8_sssd_250k"
        ]]

        for name in candidate_formats:
            profile = self.known_formats.get(name)
            if not profile or not profile.physical_format or not profile.filesystem_type:
                continue

            # Size check with tolerance
            if abs(image_size - profile.physical_format.total_bytes) > 1024:
                continue

            self.logger.debug(f"Trying profile: {name}")

            try:
                temp_format = copy.deepcopy(profile.physical_format)
                if profile.filesystem_config:
                    setattr(temp_format, '_associated_filesystem_config', profile.filesystem_config)

                self.disk.set_geometry(temp_format)

                fs_class = get_filesystem_class_by_type(profile.filesystem_type)
                if fs_class:
                    fs = fs_class(self.disk)
                    if fs.get_validity_score() >= getattr(fs, 'VALIDITY_THRESHOLD', 30):
                        self.logger.info(f"Matched profile: {name}")
                        return name, fs.get_specific_config(), temp_format

            except Exception as e:
                self.logger.debug(f"Profile {name} failed: {e}")
                continue

        return None, None, None

    def _apply_generic_fallback(self, initial_format: Optional[PhysicalFormat]) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Applies a generic fallback geometry."""
        import os

        # Try to match standard sizes
        if hasattr(self.driver, 'file_path') and os.path.exists(self.driver.file_path):
            file_size = os.path.getsize(self.driver.file_path)
            for name in ["ibm_3.5_1.44m", "ibm_5.25_360k", "ibm_3.5_720k"]:
                profile = self.known_formats.get(name)
                if profile and profile.physical_format.total_bytes == file_size:
                    self.disk.set_geometry(profile.physical_format)
                    self.logger.info(f"Generic fallback matched: {name}")
                    return name, None, profile.physical_format

        # Final fallback: 1.44M geometry
        tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1)
        pf = PhysicalFormat(80, 2, 300, False, 512, [tf])
        self.disk.set_geometry(pf)
        self.logger.warning("Using generic 1.44M fallback geometry")
        return None, None, pf


class GreaseweazleFormatDetector(FormatDetector):
    """
    Format detector for Greaseweazle physical drives.

    Strategy:
    1. Use size hint to get default geometry
    2. Scan track 0 to refine geometry
    3. Check for second head
    4. Iterate through size-matched profiles
    """

    def __init__(self, disk, driver, known_formats: Dict[str, FormatProfile], drive_size: str):
        super().__init__(disk, driver, known_formats)
        self.drive_size = drive_size

    def detect(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        # Start with a default geometry based on drive size
        default_geom = self._get_default_geometry()
        temp_profile = FormatProfile("temp", "Temporary", default_geom, "Unknown", None)
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

        # Filter and iterate through matching profiles
        filtered = self._filter_profiles_by_size_and_heads(has_second_head)
        matched = self._find_matching_profile(filtered)

        if matched:
            self.logger.info(f"Detected profile: {matched.name}")
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

        tf = TrackFormat(0, cyls - 1, 0, heads - 1, spt, enc, rate, 1, gap3_bytes=84)
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
                    self.logger.info(f"Track scan refined geometry: {self.driver.physical_format}")

                    # Preserve filesystem config if present
                    current_config = getattr(self.disk.physical_format, '_associated_filesystem_config', None)
                    if current_config:
                        setattr(self.driver.physical_format, '_associated_filesystem_config', current_config)

                    self.disk.set_geometry(self.driver.physical_format)

            # Restore original settings
            self.driver.fmt_cls = original_fmt_cls
            self.driver.using_custom_diskdef = original_custom
            if self.driver.fmt_cls and self.driver.using_custom_diskdef:
                self.driver._create_and_set_custom_diskdef()

        except Exception as e:
            self.logger.error(f"Track scan failed: {e}")

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

        self.logger.debug(f"Filtered to {len(filtered)} profiles")
        return filtered

    def _find_matching_profile(self, profiles: List[FormatProfile]) -> Optional[FormatProfile]:
        """Finds the first matching profile from the filtered list."""
        original_format = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        for profile in profiles:
            self.logger.debug(f"Trying profile: {profile.name}")
            self.disk.set_geometry(profile.physical_format)

            try:
                fs_class = get_filesystem_class_by_type(profile.filesystem_type)
                if not fs_class:
                    continue

                fs = fs_class(self.disk)
                if fs.get_validity_score() < getattr(fs, 'VALIDITY_THRESHOLD', 30):
                    continue

                # Consistency check
                if isinstance(fs, FATFilesystem) and isinstance(profile.filesystem_config, FATVolumeInfo):
                    current_bpb = fs.boot_sector
                    profile_bpb = profile.filesystem_config

                    if not (current_bpb and
                            profile_bpb.sectors_per_track == current_bpb.sectors_per_track and
                            profile_bpb.num_heads == current_bpb.num_heads and
                            profile_bpb.total_sectors == current_bpb.total_sectors):
                        continue

                elif isinstance(fs, CPMFilesystem) and isinstance(profile.filesystem_config, CPMDiskParameterBlock):
                    current_dpb = fs.get_specific_config()
                    profile_dpb = profile.filesystem_config

                    if not (current_dpb and
                            profile_dpb.spt == current_dpb.spt and
                            profile_dpb.bsh == current_dpb.bsh and
                            profile_dpb.dsm == current_dpb.dsm):
                        continue

                # Match found
                if hasattr(fs, '_check_and_adjust_geometry'):
                    fs._check_and_adjust_geometry(self.driver, False)

                self.logger.info(f"Matched profile: {profile.name}")
                return profile

            except Exception as e:
                self.logger.debug(f"Profile {profile.name} failed: {e}")

            # Restore state
            if original_format:
                self.disk.set_geometry(original_format)

        return None

    def _construct_fallback(self, has_second_head: bool) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """Constructs a fallback format based on detected parameters."""
        base_format = self.disk.physical_format if self.disk.physical_format else self._get_default_geometry()

        # Get hints from filesystem if available
        fs_config = None
        if self.disk.filesystem:
            fs_config = self.disk.filesystem.get_specific_config()
            if isinstance(fs_config, FATVolumeInfo) and fs_config.is_valid():
                heads = fs_config.num_heads if fs_config.num_heads > 0 else (2 if has_second_head else 1)
                spt = fs_config.sectors_per_track if fs_config.sectors_per_track > 0 else base_format.track_formats[0].sectors_per_track
                bps = fs_config.bytes_per_sector if fs_config.bytes_per_sector > 0 else base_format.bytes_per_sector

                if heads > 0 and spt > 0 and fs_config.total_sectors > 0:
                    cyls = fs_config.total_sectors // (heads * spt)

                    tf = TrackFormat(
                        0, cyls - 1, 0, heads - 1, spt,
                        base_format.track_formats[0].encoding,
                        base_format.track_formats[0].rate,
                        base_format.track_formats[0].interleave
                    )
                    fallback_pf = PhysicalFormat(cyls, heads, base_format.rpm, base_format.heads_inverted, bps, [tf])
                    self.disk.set_geometry(fallback_pf)

                    self.logger.info("Constructed fallback from filesystem hints")
                    return None, fs_config, fallback_pf

        self.logger.info("Using detected geometry as fallback")
        return None, fs_config, base_format


def create_format_detector(disk, driver, known_formats: Dict[str, FormatProfile],
                           **kwargs) -> FormatDetector:
    """
    Factory function to create the appropriate detector for a driver.

    Args:
        disk: The Disk object
        driver: The DiskIODriver instance
        known_formats: Dictionary of known format profiles
        **kwargs: Additional arguments (e.g., drive_size for Greaseweazle)

    Returns:
        An appropriate FormatDetector subclass instance
    """
    from .drivers import IMDImageDriver, H17ImageDriver, IMGImageDriver, GreaseweazleDriver

    if isinstance(driver, IMDImageDriver):
        return IMDFormatDetector(disk, driver, known_formats)
    elif isinstance(driver, H17ImageDriver):
        return H17FormatDetector(disk, driver, known_formats)
    elif isinstance(driver, IMGImageDriver):
        return IMGFormatDetector(disk, driver, known_formats)
    elif isinstance(driver, GreaseweazleDriver):
        drive_size = kwargs.get('drive_size', '3.5')
        return GreaseweazleFormatDetector(disk, driver, known_formats, drive_size)
    else:
        raise ValueError(f"No detector available for driver type: {type(driver).__name__}")
