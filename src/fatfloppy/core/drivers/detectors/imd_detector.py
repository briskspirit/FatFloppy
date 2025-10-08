from typing import Any, Optional

from ...filesystem_factory import create_filesystem
from ...format_detection import MetadataBasedDetector
from ...physical_format import PhysicalFormat


class IMDFormatDetector(MetadataBasedDetector):
    """
    Format detector for ImageDisk (.IMD) files.

    This detector uses the embedded metadata in IMD files to determine the disk
    format. IMD files contain track-by-track format information including sector
    numbering, encoding (FM/MFM), and data rate, making format detection
    straightforward through metadata inspection.

    The detector uses lenient matching that tolerates rate differences, as IMD
    files may encode the same logical format with different physical rates.
    """

    detector_for_driver = "IMDImageDriver"

    def detect(self) -> tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Detects the format of an IMD file.

        First tries filesystem parsing with the derived geometry. If that fails,
        attempts lenient geometry matching to find the best known format, then
        re-validates with that format's config.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format).
        """
        if not self.driver.physical_format:
            self.logger.error("IMD driver has no physical format")
            return None, None, None

        physical_format = self.driver.physical_format
        self.disk.set_geometry(physical_format)

        parsed_fs_config = self._parse_filesystem()

        if parsed_fs_config:
            matched_profile = self._match_to_known_profile_lenient(
                physical_format, parsed_fs_config
            )
            if matched_profile:
                self.logger.info(f"Matched known profile: {matched_profile}")
                return matched_profile, parsed_fs_config, physical_format
            self.logger.info("Filesystem parsed, using driver's physical format")
            return None, parsed_fs_config, physical_format

        self.logger.info("Filesystem parsing failed, attempting lenient geometry match")
        matched_profile_name = self._match_geometry_only(physical_format)

        if matched_profile_name:
            matched_profile = self.known_formats[matched_profile_name]
            self.logger.info(f"Matched profile by geometry: {matched_profile_name}")

            if matched_profile.filesystem_config:
                try:
                    self.disk.set_geometry(matched_profile.physical_format)

                    fs = create_filesystem(self.disk)
                    if fs and fs.get_validity_score() >= fs.validity_threshold:
                        fs_config = fs.get_specific_config()
                        self.logger.info(
                            "Filesystem validated with matched profile's config"
                        )
                        return (
                            matched_profile_name,
                            fs_config,
                            matched_profile.physical_format,
                        )
                except Exception as e:
                    self.logger.warning(f"Failed to validate with matched config: {e}")

            return (
                matched_profile_name,
                matched_profile.filesystem_config,
                matched_profile.physical_format,
            )

        return None, None, physical_format

    def _match_geometry_only(self, physical_format: PhysicalFormat) -> Optional[str]:
        """
        Matches only by geometry, ignoring filesystem config.

        Uses lenient matching that allows rate differences.

        Args:
            physical_format: The detected physical format

        Returns:
            The name of the matching profile, or None if no match found
        """
        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if self._physical_formats_match(profile.physical_format, physical_format):
                self.logger.info(f"Exact geometry match: {name}")
                return name

        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if self._physical_formats_match_lenient(
                profile.physical_format, physical_format
            ):
                self.logger.info(f"Lenient geometry match: {name}")
                return name

        return None

    def _match_to_known_profile_lenient(
        self, physical_format: PhysicalFormat, fs_config: Any
    ) -> Optional[str]:
        """
        Attempts to match with both geometry and filesystem config.

        Uses lenient physical format matching that allows rate differences.

        Args:
            physical_format: The detected physical format
            fs_config: The detected filesystem configuration

        Returns:
            The name of the matching profile, or None if no match found
        """
        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if not self._physical_formats_match(
                profile.physical_format, physical_format
            ):
                continue
            if self._filesystem_configs_match(
                profile, profile.filesystem_config, fs_config
            ):
                self.logger.info(f"Exact match found: {name}")
                return name

        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if not self._physical_formats_match_lenient(
                profile.physical_format, physical_format
            ):
                continue
            if self._filesystem_configs_match(
                profile, profile.filesystem_config, fs_config
            ):
                self.logger.info(f"Lenient match found (rate difference): {name}")
                return name

        return None

    def _physical_formats_match_lenient(
        self, profile_pf: PhysicalFormat, detected_pf: PhysicalFormat
    ) -> bool:
        """
        Checks if two physical formats match, allowing rate differences.

        This is necessary because IMD files may have different rates than
        the canonical format definition, but represent the same logical format.

        Args:
            profile_pf: Physical format from the profile
            detected_pf: Detected physical format

        Returns:
            True if the formats match (ignoring rate)
        """
        # Check basic geometry
        if (
            profile_pf.cylinders != detected_pf.cylinders
            or profile_pf.heads != detected_pf.heads
        ):
            return False

        # For variable BPS formats, compare at track level
        if profile_pf.has_variable_bps or detected_pf.has_variable_bps:
            if len(profile_pf.track_formats) != len(detected_pf.track_formats):
                return False
        else:
            # Uniform BPS - quick check
            if profile_pf.bytes_per_sector != detected_pf.bytes_per_sector:
                return False

        # Compare track formats (lenient on rate)
        if len(profile_pf.track_formats) != len(detected_pf.track_formats):
            return False

        for tf_profile, tf_detected in zip(
            profile_pf.track_formats, detected_pf.track_formats
        ):
            # Check track ranges
            if (
                tf_profile.track_start != tf_detected.track_start
                or tf_profile.track_end != tf_detected.track_end
            ):
                return False

            # Check sectors per track
            if tf_profile.sectors_per_track != tf_detected.sectors_per_track:
                return False

            # Check bytes per sector (important for variable BPS)
            if tf_profile.bytes_per_sector != tf_detected.bytes_per_sector:
                return False

            # Check encoding (case-insensitive)
            if tf_profile.encoding.upper() != tf_detected.encoding.upper():
                return False

            # NOTE: We deliberately skip rate comparison - that's the "lenient" part

            # For custom sector ordering, check translation tables match
            if (
                not profile_pf.image_in_sector_id_order
                and tf_profile.sector_translation_table
                != tf_detected.sector_translation_table
            ):
                return False

        return True
