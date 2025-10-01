# src/fatfloppy/core/drivers/detectors/img.py
import copy
import os
from typing import Optional, Tuple, Any, Dict

from ...filesystems.fat12fs import FATVolumeInfo, FATFilesystem
from ...format_detection import FormatDetector
from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...filesystem_factory import get_filesystem_class_by_type


class IMGFormatDetector(FormatDetector):
    """
    Format detector for raw IMG files.

    Strategy:
    1. Try direct BPB parse with canonical geometry
    2. Iterate through known profiles by size
    3. Fall back to generic geometry
    """
    detector_for_driver = "IMGImageDriver"

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
            if fs.get_validity_score() < fs.validity_threshold:
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
                    if fs.get_validity_score() >= fs.validity_threshold:
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
