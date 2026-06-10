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

        # Try parsing filesystem with detected geometry
        parsed_fs_config = self._parse_filesystem()

        if parsed_fs_config:
            # Try lenient matching with filesystem config
            matched_profile = self._match_to_known_profile(
                physical_format, parsed_fs_config, strict=False
            )
            if matched_profile:
                self.logger.info(f"Matched known profile: {matched_profile}")
                return matched_profile, parsed_fs_config, physical_format
            self.logger.info("Filesystem parsed, using driver's physical format")
            return None, parsed_fs_config, physical_format

        # Filesystem parsing failed, try geometry-only matching
        self.logger.info("Filesystem parsing failed, attempting lenient geometry match")
        matched_profile_name = self._match_geometry_only(physical_format)

        if matched_profile_name:
            matched_profile = self.known_formats[matched_profile_name]
            self.logger.info(f"Matched profile by geometry: {matched_profile_name}")

            # Confirm the matched profile's geometry yields a valid filesystem,
            # then return the PROFILE's own config. The profile is authoritative
            # for special no-BPB layouts (e.g. the DEC Rainbow's interleaved
            # FAT12): a generic re-synthesis on the profile geometry can derive a
            # plausible-but-wrong layout (e.g. too small a root directory), so we
            # validate but do not adopt the re-derived config.
            if matched_profile.filesystem_config:
                try:
                    self.disk.set_geometry(matched_profile.physical_format)

                    fs = create_filesystem(self.disk)
                    if fs and fs.get_validity_score() >= fs.validity_threshold:
                        self.logger.info(
                            "Filesystem validated with matched profile's config"
                        )
                        return (
                            matched_profile_name,
                            matched_profile.filesystem_config,
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
        # Try strict matching first
        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if self._physical_formats_match(
                profile.physical_format, physical_format, strict=True
            ):
                self.logger.info(f"Exact geometry match: {name}")
                return name

        # Fall back to lenient matching (ignore rate)
        for name, profile in self.known_formats.items():
            if not profile.physical_format:
                continue
            if self._physical_formats_match(
                profile.physical_format, physical_format, strict=False
            ):
                self.logger.info(f"Lenient geometry match: {name}")
                return name

        return None
