# src/fatfloppy/core/drivers/detectors/greaseweazle_detector.py
import copy
from typing import Optional, Tuple, Any, Dict, List

from ...filesystems.cpm_fs import CPMDiskParameterBlock, CPMFilesystem
from ...filesystems.fat12fs import FATVolumeInfo, FATFilesystem
from ...format_detection import FormatDetector, MetadataBasedDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...filesystem_factory import create_filesystem, get_filesystem_class_by_type


class GreaseweazleFormatDetector(FormatDetector):
    """
    Format detector for Greaseweazle physical drives.

    Strategy:
    1. Use size hint to get default geometry
    2. Scan track 0 to refine geometry
    3. Check for second head
    4. Iterate through size-matched profiles
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

        # Get detected sectors per track to prioritize matching profiles
        detected_spt = original_format.track_formats[0].sectors_per_track if original_format else None

        # Sort profiles: exact SPT matches first, then others
        if detected_spt:
            profiles = sorted(profiles, key=lambda p: (
                p.physical_format.track_formats[0].sectors_per_track != detected_spt,
                p.name
            ))
            self.logger.debug(f"Prioritizing profiles matching detected SPT={detected_spt}")

        for profile in profiles:
            self.logger.debug(f"Trying profile: {profile.name} (SPT={profile.physical_format.track_formats[0].sectors_per_track})")
            self.disk.set_geometry(profile.physical_format)

            try:
                fs_class = get_filesystem_class_by_type(profile.filesystem_type)
                if not fs_class:
                    continue

                fs = fs_class(self.disk)
                if fs.get_validity_score() < fs.validity_threshold:
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

            # Restore state after failed match
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
