# src/fatfloppy/core/controller.py
import os
import copy
from typing import List, Optional, Tuple, Dict, Any

from .utils.logging_config import get_logger
from .disk import Disk
from .drivers import (
    DiskIODriver, GreaseweazleDriver, IMGImageDriver, IMDImageDriver
)
from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .filesystems.fs_base import Filesystem, FileInfo
from .filesystems.fat12fs import FATFilesystem, FATVolumeInfo, FAT12_MAX_CLUSTERS
from .format_definitions import FLOPPY_FORMATS
from .filesystem_factory import create_filesystem, get_filesystem_class_by_type

logger = get_logger()


class DiskController:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        self.known_formats = FLOPPY_FORMATS
        self.driver: Optional[DiskIODriver] = None
        self.explicit_format_set = False
        self.physical_format: Optional[PhysicalFormat] = None
        self.active_filesystem_config: Optional[Any] = None

    def _create_physical_driver(self, source: str, drive_letter: str, drive_size: str) -> GreaseweazleDriver:
        device_name = source if source else None
        driver = GreaseweazleDriver(device_name=device_name, drive=drive_letter, drive_size=drive_size)
        try:
            driver.initialize() # RPM measurement happens here
            self.logger.debug(f"Initialized Greaseweazle driver for {source}")
        except Exception as e:
            self.logger.error(f"Error initializing Greaseweazle driver: {e}")
            raise
        return driver

    def _create_img_driver(self, source: str) -> IMGImageDriver:
        driver = IMGImageDriver(file_path=source)
        self.logger.debug(f"Created IMG driver for {source}")
        return driver

    def _create_imd_driver(self, source: str) -> IMDImageDriver:
        driver = IMDImageDriver(file_path=source)
        self.logger.debug(f"Created IMD driver for {source}")
        return driver

    def _create_driver(self, disk_type: str, source: str, drive_letter: str, drive_size: str) -> DiskIODriver:
        if disk_type == "physical":
            return self._create_physical_driver(source, drive_letter, drive_size)
        elif disk_type == "IMG":
            return self._create_img_driver(source)
        elif disk_type == "IMD":
            return self._create_imd_driver(source)
        else:
            raise ValueError(f"Unsupported disk type: {disk_type}")

    def _create_physical_format(self, format_info: Dict[str, any], base_profile: Optional[FormatProfile] = None) -> PhysicalFormat:
        default_track_format = TrackFormat(
            track_start=0, track_end=79, head_start=0, head_end=1,
            sectors_per_track=18, encoding="MFM", rate=500, interleave=1,
            id_start=1, iam_present=True, gap3_bytes=84
        )
        default_phys = PhysicalFormat(
            cylinders=80, heads=2, rpm=300, heads_inverted=False,
            bytes_per_sector=512, track_formats=[default_track_format]
        )
        if base_profile and base_profile.physical_format:
            default_phys = base_profile.physical_format
            if default_phys.track_formats: 
                default_track_format = default_phys.track_formats[0]

        track_format = TrackFormat(
            track_start=0,
            track_end=format_info.get("cylinders", default_phys.cylinders) - 1,
            head_start=0,
            head_end=format_info.get("heads", default_phys.heads) - 1,
            sectors_per_track=format_info.get("sectors_per_track", default_track_format.sectors_per_track),
            encoding=format_info.get("encoding", default_track_format.encoding),
            rate=format_info.get("rate", default_track_format.rate),
            interleave=format_info.get("interleave", default_track_format.interleave),
            id_start=format_info.get("id_start", default_track_format.id_start),
            iam_present=format_info.get("iam_present", default_track_format.iam_present),
            gap1_bytes=format_info.get("gap1_bytes", default_track_format.gap1_bytes),
            gap2_bytes=format_info.get("gap2_bytes", default_track_format.gap2_bytes),
            gap3_bytes=format_info.get("gap3_bytes", default_track_format.gap3_bytes),
            cskew=format_info.get("cskew", default_track_format.cskew),
            hskew=format_info.get("hskew", default_track_format.hskew)
        )

        return PhysicalFormat(
            cylinders=format_info.get("cylinders", default_phys.cylinders),
            heads=format_info.get("heads", default_phys.heads),
            rpm=format_info.get("rpm", default_phys.rpm),
            heads_inverted=format_info.get("heads_inverted", default_phys.heads_inverted),
            bytes_per_sector=format_info.get("bytes_per_sector", default_phys.bytes_per_sector),
            track_formats=[track_format]
        )

    def _apply_user_format(self, format_info: Dict[str, any]) -> None:
        self.explicit_format_set = True
        format_name = format_info.get("format_name")
        base_profile = self.get_format_by_name(format_name) if format_name else None

        if base_profile and len(format_info) == 1 and "format_name" in format_info:
            physical_format = copy.deepcopy(base_profile.physical_format)
            # If using a named profile, ensure its filesystem_config is attached
            if base_profile.filesystem_config:
                 setattr(physical_format, '_associated_filesystem_config', base_profile.filesystem_config)
        else:
            physical_format = self._create_physical_format(format_info, base_profile)
            # If custom format_info provides filesystem_config, attach it
            if "filesystem_config" in format_info and format_info["filesystem_config"]:
                 setattr(physical_format, '_associated_filesystem_config', format_info["filesystem_config"])


        self.driver.set_physical_format(physical_format)
        self.disk.set_geometry(physical_format)
        self.physical_format = physical_format 

        if isinstance(self.driver, GreaseweazleDriver):
            self.driver._create_and_set_custom_diskdef()
            self.logger.debug("Applied custom diskdef for Greaseweazle driver")

    def _auto_detect_for_image(self) -> bool:
        """Auto-detect format for IMG driver based on image size and filesystem validation."""
        if not isinstance(self.driver, IMGImageDriver):
            return False
        image_size = len(self.driver.image_data)
        self.logger.debug(f"Auto-detecting format for image of size {image_size} bytes")

        # Prioritize mixed-density formats for 8" disks
        candidate_formats = [
            "cpm_8_ssdd_imsai_mixed_idorder",
            "cpm_8_ssdd_imsai_mixed",
            "cpm_8_sssd_250k",
        ] + [name for name in self.known_formats if name not in candidate_formats]

        for name in candidate_formats:
            profile = self.known_formats.get(name)
            if not profile:
                continue
            try:
                # Check if image size matches expected total bytes
                expected_size = profile.physical_format.total_bytes
                if abs(image_size - expected_size) > 1024:  # Allow small size mismatch
                    self.logger.debug(f"Format {name} size {expected_size} does not match image size {image_size}")
                    continue

                temp_format = copy.deepcopy(profile.physical_format)
                if profile.filesystem_config:
                    setattr(temp_format, '_associated_filesystem_config', profile.filesystem_config)
                self.disk.set_geometry(temp_format)
                fs = create_filesystem(self.disk)
                if fs and fs.is_valid():
                    self.physical_format = temp_format
                    self.active_filesystem_config = profile.filesystem_config
                    self.logger.info(f"Auto-detected image format: {name}")
                    return True
                else:
                    self.logger.debug(f"Format {name} filesystem validation failed.")
            except Exception as e:
                self.logger.debug(f"Format {name} did not match: {e}")
    
        self.logger.warning("No matching format detected for image")
        return False
    
    def _handle_format(self, format_info: dict, drive_size: str) -> bool:
        if isinstance(self.driver, IMDImageDriver):
            if not self.driver.physical_format:
                self.logger.error("IMD driver loaded but failed to derive physical format.")
                return False
            self.disk.set_geometry(self.driver.physical_format)
            self.physical_format = self.driver.physical_format 
            if format_info:
                self.logger.warning("Applying user format_info to an IMD disk, this may override IMD metadata.")
                try:
                    self._apply_user_format(format_info) 
                except Exception as e:
                    self.logger.error(f"Failed to apply user format override to IMD: {e}")
                    return False
            return True
        elif format_info: # User provided explicit format
            try:
                self._apply_user_format(format_info)
                self.logger.debug(f"Applied user format. Geometry: {self.disk.physical_format}")
                return True
            except Exception as e:
                self.logger.error(f"Failed applying user format: {e}")
                return False
        else: # Auto-detection needed
            if isinstance(self.driver, GreaseweazleDriver):
                success = self._detect_physical_disk_format(drive_size)
            elif isinstance(self.driver, IMGImageDriver):
                success = self._detect_image_file_format(self.driver.file_path)
            else: 
                success = False # Should not happen if IMD handled above
            
            if success:
                self.logger.info(f"Auto-detection successful. Final disk geometry: {self.disk.physical_format}")
            else:
                self.logger.warning("Auto-detection of format failed.")
            return success

    def open_disk(self, source: str, disk_type: str = "IMG", drive_letter: str = "A", drive_size: str = "3.5", format_info: dict = None) -> bool:
        if self.disk:
            self.close_disk()
        self.explicit_format_set = bool(format_info)

        try:
            self.driver = self._create_driver(disk_type, source, drive_letter, drive_size)
            if isinstance(self.driver, IMGImageDriver) and not self.driver.file_path and not hasattr(self.driver, 'image_data'):
                if not (disk_type == "IMG" and not os.path.exists(source)):
                    self.logger.error(f"Driver for {disk_type} at {source} has no path or data.")
                    self.driver = None
                    return False
            elif isinstance(self.driver, IMDImageDriver) and not self.driver.file_path:
                 self.logger.error(f"IMD Driver for {source} has no file path.")
                 self.driver = None
                 return False

            self.disk = Disk(self.driver)
            
            success = self._handle_format(format_info, drive_size) 
            
            if not success or not self.disk or not self.disk.physical_format:
                # For IMG, if auto-detection failed but file exists, try a generic default.
                # This helps test_04_detect_format_no_match.
                if disk_type == "IMG" and os.path.exists(source) and not self.disk.physical_format and not format_info:
                     self.logger.warning("Auto-detection failed for IMG, trying a generic default geometry to allow BPB parsing.")
                     generic_tf = TrackFormat(0,79,0,1,18,"MFM",500,1) # Default 1.44M like
                     generic_pf = PhysicalFormat(80,2,300,False,512,[generic_tf])
                     self.disk.set_geometry(generic_pf)
                     if hasattr(self.driver, "set_physical_format"):
                         self.driver.set_physical_format(generic_pf)
                     # self.physical_format will be updated after filesystem check
                else:
                    self.logger.error(f"Failed to establish valid format/geometry for {source}")
                    self.close_disk()
                    return False

            self.filesystem = create_filesystem(self.disk) 
            
            if self.filesystem and hasattr(self.filesystem, '_check_and_adjust_geometry'):
                 self.logger.debug(f"Pre-adjustment disk geometry: {self.disk.physical_format}")
                 self.filesystem._check_and_adjust_geometry(self.driver, self.explicit_format_set)
                 self.logger.debug(f"Post-adjustment disk geometry: {self.disk.physical_format}")
            
            self.physical_format = self.disk.physical_format 
            self.active_filesystem_config = self.filesystem.get_specific_config() if self.filesystem else None
            self.logger.info(f"Disk '{source}' opened successfully. Type: {disk_type}. Final Geometry: {self.physical_format}")
            return True
        except (FileNotFoundError, ValueError, TypeError, Exception) as e:
            self.logger.exception(f"Error opening disk '{source}' (Type: {disk_type}): {e}")
            self.close_disk()
            return False

    def close_disk(self) -> None:
        if self.disk and self.driver and hasattr(self.driver, 'dirty') and self.driver.dirty:
            try:
                self.logger.info("Flushing changes before closing disk.")
                self.flush()
            except Exception as e:
                self.logger.error(f"Error flushing driver during close: {e}")
        self.disk = None
        self.filesystem = None
        self.driver = None
        self.physical_format = None
        self.active_filesystem_config = None
        self.explicit_format_set = False
        self.logger.debug("Disk closed and controller state reset.")

    def flush(self) -> None:
        if self.driver and hasattr(self.driver, "flush"):
            try:
                self.driver.flush()
                self.logger.debug("Driver flushed successfully")
            except Exception as e:
                self.logger.error(f"Error flushing driver: {e}")
        else:
            self.logger.warning("Flush called but no active driver or driver lacks flush method.")

    def detect_geometry(self) -> Optional[PhysicalFormat]:
        if not self.disk:
            self.logger.error("No disk opened to detect geometry")
            return None
        # Use the controller's current physical_format if available, as it's the most up-to-date
        if self.physical_format:
            return self.physical_format
        # Fallback if controller's physical_format is not set for some reason
        format_name, _ = self.detect_format() # This might trigger detection again
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile and profile.physical_format:
                return profile.physical_format
        return None


    def set_geometry(self, geometry: PhysicalFormat) -> None:
        if not self.disk:
            self.logger.error("No disk opened to set geometry")
            raise ValueError("No disk opened")
        self.disk.set_geometry(geometry)
        self.physical_format = geometry 
        if hasattr(self.driver, "set_physical_format"): # Also update driver
            self.driver.set_physical_format(copy.deepcopy(geometry))
        if isinstance(self.driver, GreaseweazleDriver): # Re-create diskdef if GW
            self.driver._create_and_set_custom_diskdef()
        self.logger.debug(f"Geometry set on controller, disk, and driver: {geometry}")


    def detect_format(self) -> Optional[Tuple[str, Any]]:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format (DiskController.detect_format)")
            return None, None

        true_initial_disk_physical_format = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None
        
        def _restore_state_if_no_match_found():
            if true_initial_disk_physical_format:
                if not self.disk.physical_format or self.disk.physical_format != true_initial_disk_physical_format:
                    self.logger.debug(f"Restoring true_initial_disk_physical_format {true_initial_disk_physical_format} after failed detection attempts.")
                    self.set_geometry(true_initial_disk_physical_format)
            elif self.disk.physical_format is not None: 
                self.logger.debug("Clearing temporary geometry as no initial format existed and detection failed.")
                self.disk.physical_format = None
                self.physical_format = None
                # Driver's format will be updated if set_geometry(None) was possible, or by next explicit set.

        if isinstance(self.driver, IMDImageDriver):
            if not self.driver.physical_format:
                self.logger.warning("IMD: No derived physical format available for detection.")
                return None, None
            # For IMD, its derived format is authoritative for the physical layout.
            self.set_geometry(self.driver.physical_format) 
            
            parsed_fs_config: Optional[Any] = None
            boot_sector_bytes = self.driver.read_boot_sector_data()
            if boot_sector_bytes:
                try:
                    # For IMD, we need to try creating a filesystem instance to parse its specific "boot sector"
                    # or equivalent system area.
                    # The generic FATVolumeInfo.from_bytes might not be suitable for all FS on IMD.
                    # We rely on the FS specific is_valid() to parse.
                    temp_fs = create_filesystem(self.disk) # This will try FAT, then CPM etc.
                    if temp_fs and temp_fs.is_valid():
                        parsed_fs_config = temp_fs.get_specific_config()
                        fs_type_name = temp_fs.__class__.__name__
                        self.logger.info(f"IMD: Parsed system area using {fs_type_name}.")

                except ValueError: # Catch errors from specific FS parsing
                    self.logger.warning("IMD: Failed to parse system area for any known FS type.")
            
            if parsed_fs_config:
                # Match against known profiles based on physical and logical parameters
                for pf_name, pf_profile in self.known_formats.items():
                    # Check if filesystem types match (e.g. both are CPM or both are FAT12)
                    is_profile_fat = pf_profile.filesystem_type == "FAT12" and isinstance(pf_profile.filesystem_config, FATVolumeInfo)
                    is_parsed_fat = isinstance(parsed_fs_config, FATVolumeInfo)
                    is_profile_cpm = pf_profile.filesystem_type == "CPM" and isinstance(pf_profile.filesystem_config, CPMDiskParameterBlock) # Assuming CPMDiskParameterBlock exists
                    is_parsed_cpm = isinstance(parsed_fs_config, CPMDiskParameterBlock)


                    physical_match = (
                        pf_profile.physical_format and
                        pf_profile.physical_format.cylinders == self.driver.physical_format.cylinders and
                        pf_profile.physical_format.heads == self.driver.physical_format.heads and
                        pf_profile.physical_format.bytes_per_sector == self.driver.physical_format.bytes_per_sector and
                        pf_profile.physical_format.get_sectors_per_track(0,0) == self.driver.physical_format.get_sectors_per_track(0,0)
                    )

                    if not physical_match:
                        continue

                    logical_match = False
                    if is_profile_fat and is_parsed_fat:
                        logical_match = (
                            pf_profile.filesystem_config.total_sectors == parsed_fs_config.total_sectors and
                            pf_profile.filesystem_config.sectors_per_fat == parsed_fs_config.sectors_per_fat
                        )
                    elif is_profile_cpm and is_parsed_cpm:
                        # Add relevant DPB comparisons for CP/M
                        logical_match = (
                           pf_profile.filesystem_config.spt == parsed_fs_config.spt and
                           pf_profile.filesystem_config.bsh == parsed_fs_config.bsh and
                           pf_profile.filesystem_config.dsm == parsed_fs_config.dsm and
                           pf_profile.filesystem_config.drm == parsed_fs_config.drm and
                           pf_profile.filesystem_config.off == parsed_fs_config.off
                        )
                    
                    if logical_match:
                        self.logger.info(f"IMD: Matched known profile '{pf_name}'")
                        # self.set_geometry(pf_profile.physical_format) # Already set to IMD's
                        return pf_name, parsed_fs_config

            # If no profile match, but we parsed something (e.g. a custom FAT format on IMD)
            if parsed_fs_config:
                 self.logger.info(f"IMD: System area parsed, but no exact profile match. Using derived parameters.")
                 return None, parsed_fs_config # Return generic with parsed config

            return None, None # No parse, no match for IMD

        # Non-IMD drivers:
        # Attempt 1: Direct parse of boot sector
        parsed_fs_config_direct: Optional[Any] = None
        geom_for_direct_parse = self.disk.physical_format

        if not geom_for_direct_parse: # If no geometry set by _handle_format (e.g. new IMG)
            self.logger.debug("Detect_format (non-IMD): No initial geometry, setting temporary default for direct parse.")
            temp_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1, gap3_bytes=84)
            geom_for_direct_parse = PhysicalFormat(80, 2, 300, False, 512, [temp_tf])
            self.set_geometry(geom_for_direct_parse) # Temporarily set for the read

        if geom_for_direct_parse:
            try:
                # Attempt to validate using FATFilesystem first, as it's common
                fs_instance = FATFilesystem(self.disk) # FAT specific init
                if fs_instance.is_valid():
                    self.logger.info("Direct parse: Successfully validated as FAT filesystem.")
                    parsed_fs_config_direct = fs_instance.get_specific_config()

                    for pf_name, pf_profile in self.known_formats.items():
                        if pf_profile.filesystem_type == "FAT12" and \
                           isinstance(pf_profile.filesystem_config, FATVolumeInfo) and \
                           pf_profile.physical_format and \
                           isinstance(parsed_fs_config_direct, FATVolumeInfo): # Ensure parsed_fs_config is FAT
                            bpb_cyls = parsed_fs_config_direct.total_sectors // (parsed_fs_config_direct.num_heads * parsed_fs_config_direct.sectors_per_track) if parsed_fs_config_direct.num_heads * parsed_fs_config_direct.sectors_per_track > 0 else 0
                            if (pf_profile.filesystem_config.bytes_per_sector == parsed_fs_config_direct.bytes_per_sector and
                                pf_profile.filesystem_config.sectors_per_track == parsed_fs_config_direct.sectors_per_track and
                                pf_profile.filesystem_config.num_heads == parsed_fs_config_direct.num_heads and
                                pf_profile.filesystem_config.total_sectors == parsed_fs_config_direct.total_sectors and
                                pf_profile.physical_format.cylinders == bpb_cyls and
                                pf_profile.physical_format.heads == parsed_fs_config_direct.num_heads):
                                self.logger.info(f"Direct parse strongly matched FAT profile: '{pf_name}'")
                                self.set_geometry(pf_profile.physical_format)
                                # Ensure the _associated_filesystem_config is on the active physical_format
                                if hasattr(self.disk.physical_format, '_associated_filesystem_config') and isinstance(getattr(self.disk.physical_format, '_associated_filesystem_config'), FATVolumeInfo):
                                     pass # Already set by set_geometry if physical_format was annotated
                                elif pf_profile.filesystem_config: # Attach it if not already there
                                     setattr(self.disk.physical_format, '_associated_filesystem_config', pf_profile.filesystem_config)

                                return pf_name, parsed_fs_config_direct

                    self.logger.info("Direct parse (FAT) valid, but no exact named profile. Using parsed BPB to refine geometry.")
                    if parsed_fs_config_direct.bytes_per_sector > 0 and parsed_fs_config_direct.num_heads > 0 and parsed_fs_config_direct.sectors_per_track > 0:
                        cyls = parsed_fs_config_direct.total_sectors // (parsed_fs_config_direct.num_heads * parsed_fs_config_direct.sectors_per_track)
                        # Refine geometry based on parsed BPB
                        current_pf = self.disk.physical_format
                        updated_tf = TrackFormat(
                            track_start=0, track_end=cyls -1,
                            head_start=0, head_end=parsed_fs_config_direct.num_heads -1,
                            sectors_per_track=parsed_fs_config_direct.sectors_per_track,
                            encoding=current_pf.track_formats[0].encoding, # Keep original encoding/rate unless BPB implies otherwise
                            rate=current_pf.track_formats[0].rate,
                            interleave=current_pf.track_formats[0].interleave,
                            id_start=current_pf.track_formats[0].id_start,
                            iam_present=current_pf.track_formats[0].iam_present,
                            gap3_bytes=current_pf.track_formats[0].gap3_bytes
                        )
                        refined_pf = PhysicalFormat(
                            cylinders=cyls, heads=parsed_fs_config_direct.num_heads,
                            rpm=current_pf.rpm, heads_inverted=current_pf.heads_inverted,
                            bytes_per_sector=parsed_fs_config_direct.bytes_per_sector,
                            track_formats=[updated_tf]
                        )
                        # Attach the parsed config to this refined physical format
                        setattr(refined_pf, '_associated_filesystem_config', parsed_fs_config_direct)
                        self.set_geometry(refined_pf)
                        return None, parsed_fs_config_direct
                else:
                     self.logger.debug("Direct parse: The disk is not a valid FAT filesystem.")
            except Exception as e:
                self.logger.warning(f"Direct FAT parse attempt failed with an error: {e}")

        # If direct FAT parse didn't yield a result, restore initial state before profile iteration
        _restore_state_if_no_match_found()


        # Attempt 2: Iterate through known_formats (includes FAT and CP/M)
        self.logger.debug("Attempting full profile iteration for detection (if direct parse inconclusive).")
        for profile_name, profile in self.known_formats.items():
            if not profile.physical_format or not profile.filesystem_type: continue
            self.logger.debug(f"Detect_format (profile iteration): Trying '{profile_name}'")

            self.set_format(profile) # This will set geometry and _associated_filesystem_config

            try:
                fs_class = get_filesystem_class_by_type(profile.filesystem_type)
                if fs_class:
                    # Filesystem constructor will use self.disk which has physical_format
                    # with _associated_filesystem_config from the profile (due to self.set_format(profile) above)
                    fs_instance = fs_class(self.disk)
                    if fs_instance.is_valid():
                        self.logger.info(f"Profile iteration: Detected format '{profile_name}' with filesystem {profile.filesystem_type}")
                        # fs_instance.get_specific_config() should return the config derived from disk
                        # which should ideally match profile.filesystem_config if the profile is accurate.
                        # We return the one from the disk as it's "live".
                        return profile_name, fs_instance.get_specific_config()
            except Exception as e:
                self.logger.debug(f"Profile '{profile_name}' not a match (iteration) or error: {e}")
            
            _restore_state_if_no_match_found() # Restore before trying next profile


        self.logger.warning("detect_format: No format detected after all attempts.")
        _restore_state_if_no_match_found() 
        return None, None

    def set_format(self, profile: FormatProfile) -> None:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to set format")
            raise ValueError("No disk opened")
        if not profile or not profile.physical_format:
            raise ValueError("Invalid FormatProfile provided")

        if isinstance(self.driver, IMDImageDriver):
            self.logger.warning("Calling set_format with an IMD driver. This will overwrite the format derived from the file.")
        
        self.logger.info(f"Setting format using profile: {profile.name}")

        # Create a working copy of the physical_format from the profile.
        # This working copy will be set on the disk and potentially the driver.
        physical_format_to_set = copy.deepcopy(profile.physical_format)

        # Attach the filesystem_config from the profile to this working copy
        # so that filesystem classes can access it via disk.physical_format.
        if profile.filesystem_config:
            setattr(physical_format_to_set, '_associated_filesystem_config', profile.filesystem_config)
        
        # Set the (potentially annotated) physical format on the disk object.
        # self.disk.physical_format will now be physical_format_to_set.
        self.disk.set_geometry(physical_format_to_set)
        
        # If the driver supports setting physical format, pass the same (annotated) object.
        if hasattr(self.driver, "set_physical_format"):
            self.driver.set_physical_format(physical_format_to_set) # Pass the same instance
            if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
                self.logger.debug("Applying Greaseweazle custom diskdef after set_format.")
                self.driver._create_and_set_custom_diskdef()
        else:
            self.logger.warning(f"Driver type {type(self.driver).__name__} does not support set_physical_format.")
        
        # Update the controller's cache of the physical format.
        self.physical_format = self.disk.physical_format


    def get_allocated_units(self) -> List[int]:
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_units"):
            return []
        try:
            units = self.filesystem.get_allocated_units()
            self.logger.debug(f"Retrieved {len(units)} allocated units")
            return units
        except Exception as e:
            self.logger.error(f"Error getting allocated units: {e}")
            return []

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            return None
        try:
            space_info = self.filesystem.get_free_space()
            self.logger.debug(f"Free space: {space_info}")
            return space_info
        except Exception as e:
            self.logger.error(f"Error getting free space: {e}")
            return None

    def list_directory(self, path: str = "/") -> List[dict]:
        if not self.filesystem:
            return []
        try:
            items = self.filesystem.list_directory(path)
            self.logger.debug(f"Listed directory {path} with {len(items)} items")
            return [
                {
                    "name": item.name,
                    "size": item.size,
                    "is_dir": item.is_dir,
                    "datetime": item.datetime,
                    "attributes": item.attributes,
                    "starting_cluster": item.starting_cluster
                }
                for item in items
            ]
        except Exception as e:
            self.logger.error(f"Error listing directory {path}: {e}")
            return []

    def read_file(self, path: str) -> Optional[bytes]:
        if not self.filesystem:
            return None
        try:
            data = self.filesystem.read_file(path)
            self.logger.debug(f"Read file {path} with size {len(data)} bytes")
            return data
        except ValueError as e:
            self.logger.error(f"ValueError reading file {path}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error reading file {path}: {e}")
            return None

    def write_file(self, path: str, data: bytes) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.write_file(path, data)
            self.logger.debug(f"Wrote file {path} with size {len(data)} bytes")
            return True
        except Exception as e:
            self.logger.error(f"Error writing file {path}: {e}")
            return False

    def create_directory(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.create_directory(path)
            self.logger.debug(f"Created directory {path}")
            return True
        except Exception as e:
            self.logger.error(f"Error creating directory {path}: {e}")
            return False

    def delete_item(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.delete(path)
            self.logger.debug(f"Deleted item {path}")
            return True
        except Exception as e:
            self.logger.error(f"Error deleting item {path}: {e}")
            return False

    def delete_item_recursive(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            success = self.filesystem.delete_recursive(path)
            if success:
                self.logger.debug(f"Deleted item recursively {path}")
            return success
        except Exception as e:
            self.logger.error(f"Error deleting item recursively {path}: {e}")
            return False

    def format_disk(self, format_name: str, volume_label: str = "NO NAME") -> bool:
        if isinstance(self.driver, IMDImageDriver):
            self.logger.error("Formatting is not supported directly via the IMDImageDriver.")
            return False
        if not self.disk or not self.driver:
            self.logger.error("Cannot format disk: Disk or driver not initialized.")
            return False

        profile = self.known_formats.get(format_name)
        if not profile:
            if self.disk.physical_format:
                for name, prof in self.known_formats.items():
                    if prof.physical_format == self.disk.physical_format:
                        profile = prof
                        format_name = name 
                        self.logger.info(f"Using format profile '{format_name}' matching current geometry.")
                        break
            if not profile : 
                if hasattr(self.driver, 'physical_format') and self.driver.physical_format and hasattr(self.driver.physical_format, '_associated_filesystem_config'):
                    fs_cfg = getattr(self.driver.physical_format, '_associated_filesystem_config')
                    fs_type = "FAT12" if isinstance(fs_cfg, FATVolumeInfo) else "Unknown" 
                    # Check if CPMDiskParameterBlock exists before isinstance, adjust type as needed
                    if 'CPMDiskParameterBlock' in globals() and isinstance(fs_cfg, CPMDiskParameterBlock):
                        fs_type = "CPM"

                    profile = FormatProfile(name="custom_runtime", description="Custom (Runtime)",
                                            physical_format=self.driver.physical_format,
                                            filesystem_type=fs_type, filesystem_config=fs_cfg)
                    format_name = "custom_runtime"
                else:
                    self.logger.error(f"Cannot format with unknown profile '{format_name}' and no fallback.")
                    return False
        
        if not profile or not profile.physical_format or not profile.filesystem_config: # Check filesystem_config for formatting
            self.logger.error(f"Format profile '{format_name}' is invalid or missing required filesystem_config information for formatting.")
            return False

        fs_class = get_filesystem_class_by_type(profile.filesystem_type)
        if not fs_class:
            self.logger.error(f"No filesystem handler found for type '{profile.filesystem_type}' during format.")
            return False

        self.logger.info(f"Starting format process with profile: {profile.name}")
        
        try:
            if self.disk.physical_format != profile.physical_format:
                self.logger.warning(f"Disk geometry differs from profile '{profile.name}' before format. Setting format now.")
                self.set_format(profile)
            elif hasattr(self.driver, "physical_format") and self.driver.physical_format != profile.physical_format:
                self.logger.warning(f"Driver physical format differs from profile '{profile.name}'. Setting format now.")
                self.set_format(profile)
            # Ensure _associated_filesystem_config is set on the active physical_format
            # This might be redundant if set_format already does it correctly for self.disk.physical_format
            if not hasattr(self.disk.physical_format, '_associated_filesystem_config') and profile.filesystem_config:
                 setattr(self.disk.physical_format, '_associated_filesystem_config', profile.filesystem_config)


            filesystem_handler = fs_class(self.disk)
            filesystem_handler.format_fs(profile, volume_label=volume_label) 
            self.filesystem = filesystem_handler
            self.physical_format = self.disk.physical_format
            self.active_filesystem_config = self.filesystem.get_specific_config() if self.filesystem else None
            self.flush()
            
            final_vol_label = volume_label
            if isinstance(self.active_filesystem_config, FATVolumeInfo) and self.active_filesystem_config.volume_label:
                 final_vol_label = self.active_filesystem_config.volume_label.strip()

            self.logger.info(f"Disk formatting complete for profile '{profile.name}'. Volume: '{final_vol_label}'")
            return True
        except Exception as e:
            self.logger.exception(f"Error formatting disk with profile '{profile.name}': {e}")
            self.filesystem = None
            self.active_filesystem_config = None
            return False

    def create_and_format_image(self, file_path: str, profile: FormatProfile, volume_label: str = "NO NAME", disk_type: str = "IMG") -> bool:
        if self.disk:
            self.close_disk()
        if not profile or not profile.physical_format or not profile.filesystem_config:
            self.logger.error("Invalid or incomplete profile provided for image creation.")
            return False

        fs_class = get_filesystem_class_by_type(profile.filesystem_type)
        if not fs_class:
            self.logger.error(f"No filesystem handler for type '{profile.filesystem_type}' during image creation.")
            return False
        
        try:
            if disk_type == "IMG":
                total_bytes = profile.physical_format.total_bytes
                self.logger.info(f"Creating raw image file '{file_path}' with size {total_bytes} bytes.")
                with open(file_path, 'wb') as f:
                    f.truncate(total_bytes) 
                self.driver = self._create_img_driver(file_path)
                if not self.driver or len(self.driver.image_data) != total_bytes: 
                    raise IOError(f"Failed to create or correctly size raw image file '{file_path}'")
            elif disk_type == "IMD":
                self.driver = self._create_imd_driver(file_path) 
                 # For IMD, after creating the driver (which might be empty), format it in memory
                if hasattr(self.driver, 'format_imd') and isinstance(self.driver, IMDImageDriver):
                    fill_byte = 0xE5 # Common fill byte for unformatted disks
                    self.driver.format_imd(profile, fill_byte=fill_byte)
                    # The IMD driver's physical_format should now be set from the profile
            else:
                self.logger.error(f"Unsupported disk type: {disk_type} for image creation.")
                return False

            self.disk = Disk(self.driver)
            self.logger.info(f"Setting format for new {disk_type} image using profile: {profile.name}")
            self.set_format(profile) # This will use the modified set_format

            self.logger.info(f"Formatting new {disk_type} image with volume label: '{volume_label}'")
            # Ensure the filesystem handler gets the correct config (via _associated_filesystem_config)
            filesystem_handler = fs_class(self.disk) # Constructor should pick up config via disk.physical_format
            filesystem_handler.format_fs(profile, volume_label=volume_label) 
            self.filesystem = filesystem_handler
            self.physical_format = self.disk.physical_format
            self.active_filesystem_config = self.filesystem.get_specific_config() if self.filesystem else None
            
            self.flush() 
            final_vol_label = volume_label
            if isinstance(self.active_filesystem_config, FATVolumeInfo) and self.active_filesystem_config.volume_label:
                final_vol_label = self.active_filesystem_config.volume_label.strip()
            self.logger.info(f"Successfully created and formatted {disk_type} image '{file_path}'. Volume: '{final_vol_label}'")
            return True

        except Exception as e:
            self.logger.exception(f"Error during create_and_format_image for {file_path}: {e}")
            self.close_disk()
            if os.path.exists(file_path):
                try:
                    os.remove(file_path) 
                except Exception as rm_e:
                    self.logger.warning(f"Could not remove partially created image file '{file_path}': {rm_e}")
            return False

    def list_formats(self) -> List[Tuple[str, str]]:
        formats = [(name, profile.description) for name, profile in self.known_formats.items()]
        self.logger.debug(f"Listed {len(formats)} known formats")
        return formats

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        profile = self.known_formats.get(name)
        if profile:
            self.logger.debug(f"Retrieved format profile: {name}")
        else:
            self.logger.warning(f"Format profile not found: {name}")
        return profile

    def _get_default_geometry_and_physical(self, drive_size: str) -> Tuple[PhysicalFormat, PhysicalFormat]:
        if drive_size == "3.5":
            cylinders = 80; sectors_per_track = 18; bytes_per_sector = 512
            rate = 500; encoding = "MFM"; rpm = 300; heads = 2
        elif drive_size == "5.25":
            cylinders = 40; sectors_per_track = 9; bytes_per_sector = 512
            rate = 250; encoding = "MFM"; rpm = 300; heads = 2
        elif drive_size == "8":
            cylinders = 77; sectors_per_track = 26; bytes_per_sector = 128 
            rate = 250; encoding = "FM"; rpm = 360; heads = 2 
        else:
            raise ValueError(f"Unsupported drive size: {drive_size}")
        track_format = TrackFormat(0, cylinders - 1, 0, heads -1, sectors_per_track, encoding, rate, 1, gap3_bytes=84)
        geometry = PhysicalFormat(cylinders, heads, rpm, False, bytes_per_sector, [track_format])
        self.logger.debug(f"Created default geometry for drive size {drive_size}")
        return geometry, geometry 

    def _check_second_head(self, temp_profile: FormatProfile) -> bool:
        if self.filesystem and (specific_config := self.filesystem.get_specific_config()):
            if hasattr(specific_config, 'num_heads') and isinstance(specific_config.num_heads, int) and specific_config.num_heads > 0 :
                return specific_config.num_heads > 1
        
        current_physical_format_before_check = copy.deepcopy(self.disk.physical_format)
        self.set_format(temp_profile) 
        has_second_head_result = False
        try:
            if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_read_track'):
                has_second_head_result = self.driver._read_track(0, 1)
            else: 
                self.disk.read_sector(0, 1, 1) 
                has_second_head_result = True
        except Exception:
            has_second_head_result = False
        finally:
            if current_physical_format_before_check: 
                self.set_format(current_physical_format_before_check)
            elif self.disk.physical_format != temp_profile.physical_format : 
                 self.set_format(temp_profile) 
        return has_second_head_result


    def _filter_known_formats(self, drive_size: str, has_second_head: bool) -> List[FormatProfile]:
        filtered = []
        size_str = f"{drive_size}\""
        for profile in self.known_formats.values():
            if size_str not in profile.description:
                continue
            if not has_second_head and profile.physical_format.heads > 1:
                continue
            filtered.append(profile)
        self.logger.debug(f"Filtered {len(filtered)} formats for drive size {drive_size} and second head {has_second_head}")
        return filtered

    def _find_matching_format(self, filtered_formats: List[FormatProfile]) -> Optional[FormatProfile]:
        original_disk_format_on_entry = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        for profile in filtered_formats:
            self.logger.debug(f"_find_matching_format: Trying profile '{profile.name}'")
            self.set_format(profile) # Sets self.disk.physical_format to profile.physical_format (annotated)

            try:
                # create_filesystem will now use the _associated_filesystem_config from disk.physical_format
                # if the FS class (e.g. CPMFilesystem) is designed to pick it up.
                fs = create_filesystem(self.disk)
                if fs and fs.is_valid():
                    # For FAT, fs.is_valid() checks the BPB signature etc. from disk.
                    # For CP/M, fs.is_valid() uses the DPB from profile (via _associated_filesystem_config)
                    # to try and read system/directory tracks.
                    
                    # Perform consistency check if applicable
                    perform_bpb_check = True # Default to true, specific checks might set to false
                    
                    # If it's a FAT filesystem, compare the profile's BPB with the one read from disk by fs_instance.
                    if isinstance(fs, FATFilesystem) and isinstance(profile.filesystem_config, FATVolumeInfo):
                        profile_bpb = profile.filesystem_config
                        current_bpb_from_fs_obj = fs.boot_sector # FATFilesystem loads this in its __init__

                        if not (profile_bpb.sectors_per_track == current_bpb_from_fs_obj.sectors_per_track and
                                profile_bpb.num_heads == current_bpb_from_fs_obj.num_heads and
                                profile_bpb.total_sectors == current_bpb_from_fs_obj.total_sectors and
                                profile_bpb.bytes_per_sector == current_bpb_from_fs_obj.bytes_per_sector and
                                profile_bpb.sectors_per_fat == current_bpb_from_fs_obj.sectors_per_fat and
                                profile.physical_format.get_sectors_per_track(0,0) == current_bpb_from_fs_obj.sectors_per_track and
                                profile.physical_format.heads == current_bpb_from_fs_obj.num_heads and
                                profile.physical_format.bytes_per_sector == current_bpb_from_fs_obj.bytes_per_sector):
                            self.logger.debug(
                                f"Profile '{profile.name}' (FAT) BPB/Geom mismatch with current disk BPB. Skipping."
                            )
                            perform_bpb_check = False
                    # Add similar check for CP/M if fs.get_specific_config() can be compared against profile.filesystem_config
                    elif 'CPMFilesystem' in str(type(fs)) and isinstance(profile.filesystem_config, CPMDiskParameterBlock):
                         profile_dpb = profile.filesystem_config
                         current_dpb_from_fs_obj = fs.get_specific_config()
                         if not (profile_dpb.spt == current_dpb_from_fs_obj.spt and
                                 profile_dpb.bsh == current_dpb_from_fs_obj.bsh and
                                 profile_dpb.dsm == current_dpb_from_fs_obj.dsm and
                                 profile_dpb.off == current_dpb_from_fs_obj.off
                                 # Add more critical DPB fields if necessary
                                ):
                            self.logger.debug(
                                f"Profile '{profile.name}' (CP/M) DPB mismatch with current disk DPB. Skipping."
                            )
                            perform_bpb_check = False


                    if perform_bpb_check: 
                        self.filesystem = fs 
                        if hasattr(fs, '_check_and_adjust_geometry'):
                            self.logger.debug(f"Calling _check_and_adjust_geometry for profile '{profile.name}' within _find_matching_format.")
                            fs._check_and_adjust_geometry(self.driver, self.explicit_format_set)
                        
                        self.logger.info(f"_find_matching_format: Found and validated profile '{profile.name}'. Final geometry for this match: {self.disk.physical_format}")
                        return profile
                    else: 
                        if original_disk_format_on_entry:
                            if self.disk.physical_format != original_disk_format_on_entry:
                                self.set_geometry(original_disk_format_on_entry)
                        elif self.disk.physical_format is not None:
                            self.disk.physical_format = None
                            self.physical_format = None
                        continue 

            except Exception as e:
                self.logger.debug(f"Profile {profile.name} check failed during _find_matching_format: {e}")
            
            if original_disk_format_on_entry:
                if self.disk.physical_format != original_disk_format_on_entry:
                    self.set_geometry(original_disk_format_on_entry)
            elif self.disk.physical_format is not None:
                self.disk.physical_format = None
                self.physical_format = None

        if original_disk_format_on_entry and self.disk.physical_format != original_disk_format_on_entry:
            self.set_geometry(original_disk_format_on_entry)
        elif not original_disk_format_on_entry and self.disk.physical_format is not None:
             self.disk.physical_format = None
             self.physical_format = None

        return None

    def _detect_physical_disk_format(self, drive_size: str = "3.5") -> bool:
        true_initial_controller_pf = copy.deepcopy(self.physical_format) if self.physical_format else None
        temp_geometry, _ = self._get_default_geometry_and_physical(drive_size)
        temp_profile = FormatProfile("temp_detect", "Temporary for detection", temp_geometry, "Unknown", None) # Use Unknown FS type for temp
        
        self.set_format(temp_profile) # This applies temp_geometry
        
        if hasattr(self.driver, 'initialize') and not getattr(self.driver, 'initialized', False):
            self.driver.initialize()

        if isinstance(self.driver, GreaseweazleDriver):
            self.logger.debug("Attempting Greaseweazle initial track scan for geometry detection.")
            try:
                original_fmt_cls = self.driver.fmt_cls
                original_using_custom_diskdef = self.driver.using_custom_diskdef
                self.driver.fmt_cls = None
                self.driver.using_custom_diskdef = False
                
                if self.driver._read_track(0, 0): 
                    if self.driver.physical_format and self.driver.physical_format != self.disk.physical_format:
                        self.logger.info(f"Greaseweazle scan updated physical format to: {self.driver.physical_format}")
                        # The driver's physical_format might not have _associated_filesystem_config
                        # We need to preserve it if it was on self.disk.physical_format before this call
                        current_associated_config = getattr(self.disk.physical_format, '_associated_filesystem_config', None)
                        pf_from_driver = self.driver.physical_format
                        if current_associated_config:
                            setattr(pf_from_driver, '_associated_filesystem_config', current_associated_config)
                        self.set_geometry(pf_from_driver)
                else:
                    self.logger.warning("Greaseweazle initial track scan did not yield a format.")
                
                self.driver.fmt_cls = original_fmt_cls
                self.driver.using_custom_diskdef = original_using_custom_diskdef
                if self.driver.fmt_cls and self.driver.using_custom_diskdef:
                    self.driver._create_and_set_custom_diskdef()
            except Exception as e:
                self.logger.error(f"Error during Greaseweazle initial scan: {e}")
        
        # Create a temporary filesystem instance based on the current disk geometry (which might have been updated by GW scan)
        # This filesystem instance is primarily for _check_second_head's potential use of BPB num_heads.
        # It's not guaranteed to be a valid or final filesystem.
        self.filesystem = create_filesystem(self.disk) # Uses the current self.disk.physical_format

        current_disk_pf_for_head_check = self.disk.physical_format if self.disk.physical_format else temp_profile.physical_format
        # Ensure temp profile for head check has an FS type if filesystem expects one
        temp_profile_for_head_check = FormatProfile("head_check_temp", "", current_disk_pf_for_head_check, 
                                                    self.filesystem.get_specific_config().__class__.__name__ if self.filesystem and self.filesystem.get_specific_config() else "Unknown",
                                                    self.filesystem.get_specific_config() if self.filesystem else None)

        has_second_head = self._check_second_head(temp_profile_for_head_check)
        
        filtered_formats = self._filter_known_formats(drive_size, has_second_head)
        matching_profile = self._find_matching_format(filtered_formats)

        if matching_profile:
            self.logger.info(f"Physical disk format detected and set to: {matching_profile.name} with geometry {self.disk.physical_format}")
            # self.set_format(matching_profile) # Already done by _find_matching_format
            return True
        
        self.logger.warning("No known format profile matched physical disk after all checks. Using a fallback geometry.")
        base_geom_for_fallback = true_initial_controller_pf if true_initial_controller_pf else self.physical_format
        if not base_geom_for_fallback: base_geom_for_fallback = temp_geometry

        # Re-set geometry to the base_geom_for_fallback to ensure clean state for deriving final fallback
        # This base_geom_for_fallback might or might not have _associated_filesystem_config.
        self.set_geometry(base_geom_for_fallback) # self.disk.physical_format is now base_geom_for_fallback
        
        # Attempt to re-create filesystem based on this base_geom_for_fallback to get hints for SPT, BPS from BPB if FAT-like
        # The self.disk.physical_format passed to create_filesystem here is base_geom_for_fallback
        self.filesystem = create_filesystem(self.disk)

        final_default_heads = 2 if has_second_head else 1
        # Use current disk state (base_geom_for_fallback) for SPT, BPS defaults
        final_fallback_spt = self.disk.physical_format.track_formats[0].sectors_per_track
        final_fallback_bps = self.disk.physical_format.bytes_per_sector
        final_fallback_cyls = self.disk.physical_format.cylinders
        final_fallback_encoding = self.disk.physical_format.track_formats[0].encoding
        final_fallback_rate = self.disk.physical_format.track_formats[0].rate
        final_fallback_interleave = self.disk.physical_format.track_formats[0].interleave
        final_fallback_gap3 = self.disk.physical_format.track_formats[0].gap3_bytes
        final_fallback_rpm = self.disk.physical_format.rpm
        final_fallback_inverted = self.disk.physical_format.heads_inverted

        fs_config_from_fallback_base = None
        if self.filesystem and isinstance(self.filesystem.get_specific_config(), FATVolumeInfo):
            bs = self.filesystem.get_specific_config()
            if bs and bs.is_valid(): # Check if a valid BPB was parsed from base_geom_for_fallback
                final_default_heads = bs.num_heads if bs.num_heads > 0 else final_default_heads
                final_fallback_spt = bs.sectors_per_track if bs.sectors_per_track > 0 else final_fallback_spt
                final_fallback_bps = bs.bytes_per_sector if bs.bytes_per_sector > 0 else final_fallback_bps
                if bs.num_heads > 0 and bs.sectors_per_track > 0 and bs.total_sectors > 0:
                     final_fallback_cyls = bs.total_sectors // (bs.num_heads * bs.sectors_per_track)
                fs_config_from_fallback_base = bs # This is a FATVolumeInfo

        final_fallback_tf = TrackFormat(
            0, final_fallback_cyls - 1, 0, final_default_heads - 1,
            final_fallback_spt, final_fallback_encoding, final_fallback_rate, 
            final_fallback_interleave, gap3_bytes=final_fallback_gap3
        )
        final_fallback_geom = PhysicalFormat(
            final_fallback_cyls, final_default_heads, final_fallback_rpm,
            final_fallback_inverted, final_fallback_bps, [final_fallback_tf]
        )
        # Determine fallback FS type based on what fs_config_from_fallback_base is
        fallback_fs_type = "FAT12" if isinstance(fs_config_from_fallback_base, FATVolumeInfo) else "Unknown"

        final_fallback_profile = FormatProfile(
            name="fallback_detected", description="Fallback based on physical detection",
            physical_format=final_fallback_geom, 
            filesystem_type=fallback_fs_type, 
            filesystem_config=fs_config_from_fallback_base
        )
        self.set_format(final_fallback_profile) # This applies the fallback profile with its config
        self.logger.info(f"Physical disk format set to fallback: {final_fallback_profile.description} with geometry {self.disk.physical_format}")
        return True


    def _detect_image_file_format(self, file_path: str) -> bool:
        # detect_format now handles geometry setting and _associated_filesystem_config internally
        # It returns (profile_name_or_None, fs_config_derived_from_disk_or_None)
        format_name, fs_config = self.detect_format() 

        if format_name and fs_config:
            self.logger.info(f"Image file '{file_path}' matched profile: {format_name}. Current geometry: {self.disk.physical_format}. FS Config: {type(fs_config)}")
            return True
        elif fs_config: # No named profile, but fs_config was derived (e.g. direct FAT parse or IMD parse)
            self.logger.info(f"Image file '{file_path}': Parsed filesystem data, using constructed/set geometry: {self.disk.physical_format}. FS Config: {type(fs_config)}")
            return True
        
        self.logger.warning(f"Could not determine a suitable format for image file: {file_path}.")
        if not self.disk.physical_format: # If detect_format failed and left no geometry
            self.logger.debug(f"Setting a generic default geometry for {file_path} as last resort for IMG after failed detection.")
            # Try common FAT formats first before a completely generic one
            for default_prof_name in ["ibm_3.5_1.44m", "ibm_5.25_360k", "ibm_3.5_720k"]:
                profile_default = self.get_format_by_name(default_prof_name)
                if profile_default and profile_default.physical_format.total_bytes == os.path.getsize(file_path):
                    self.set_format(profile_default)
                    # Try to create FS again with this profile
                    self.filesystem = create_filesystem(self.disk)
                    if self.filesystem and self.filesystem.is_valid():
                        self.logger.info(f"Last resort for IMG: Matched profile {default_prof_name} by size.")
                        return True
            
            # Absolute last resort generic geometry
            tf = TrackFormat(0,79,0,1,18,"MFM", 500, 1)
            pf = PhysicalFormat(80,2,300,False,512,[tf])
            # Create a temporary profile for this generic geometry
            generic_profile = FormatProfile("generic_fallback", "Generic Fallback", pf, "Unknown", None)
            self.set_format(generic_profile)
            return True # Return true as we've set *a* geometry, even if FS is unknown
        return False # Failed to determine or set any format


    def create_custom_profile(self, format_info: Dict[str, Any]) -> Optional[FormatProfile]:
        try:
            physical_format = self._create_physical_format(format_info)
            target_fs_type = format_info.get("filesystem_type", "FAT12")
            filesystem_config_obj: Optional[Any] = None

            if target_fs_type == "FAT12":
                total_sectors = physical_format.total_sectors
                bytes_per_sector = physical_format.bytes_per_sector
                sectors_per_cluster = format_info.get("sectors_per_cluster", 1)
                if sectors_per_cluster == 0: sectors_per_cluster = 1 # Avoid division by zero
                reserved_sectors = format_info.get("reserved_sectors", 1)
                num_fats = format_info.get("num_fats", 2)
                root_entries = format_info.get("root_entries", 224 if total_sectors > 1440 else 112) # Common defaults
                
                # Ensure bytes_per_sector is not zero for calculations
                if bytes_per_sector == 0:
                    self.logger.error("Bytes per sector cannot be zero for custom FAT12 profile.")
                    return None
                root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
                
                available_for_fats_and_data = total_sectors - (reserved_sectors + root_dir_sectors)
                if available_for_fats_and_data < 0 :
                    self.logger.error("Not enough space for reserved and root directory sectors in custom FAT12 profile.")
                    return None

                # Iterative calculation for sectors_per_fat (SPF)
                # Start with a low estimate for SPF and increase until consistent
                sectors_per_fat = 1 # Minimal SPF
                final_num_clusters = 0
                
                for _attempt in range(available_for_fats_and_data // (num_fats if num_fats > 0 else 1) +1): # Max possible SPF + safety margin
                    if num_fats == 0 : # Avoid issues if num_fats is 0
                        data_sectors = available_for_fats_and_data
                    else:
                        data_sectors = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)
                    
                    if data_sectors < sectors_per_cluster: # Not enough space for even one cluster
                        break 
                    
                    num_clusters_current_try = data_sectors // sectors_per_cluster
                    if num_clusters_current_try <= 0:
                        break

                    # For FAT12, each entry is 1.5 bytes (12 bits)
                    # Bytes needed for FAT = ceil(num_clusters * 1.5)
                    # Add 2 for media descriptor and EOC marker if storing raw FAT bytes
                    # More accurately: 2 entries for FAT ID (0 and 1) are typically reserved.
                    # Max cluster number for FAT12 is 4084 (0xFF4).
                    # FAT entries are 0 to N+1 (N = number of data clusters).
                    # So, (num_clusters_current_try + 2) entries needed.
                    fat_bytes_needed = ((num_clusters_current_try + 2) * 3 + 1) // 2 # ceil((N+2)*1.5)
                    
                    spf_needed_for_this_many_clusters = (fat_bytes_needed + bytes_per_sector -1) // bytes_per_sector
                    
                    if spf_needed_for_this_many_clusters <= sectors_per_fat:
                        # Consistent or sectors_per_fat is generous enough
                        final_num_clusters = num_clusters_current_try
                        break # Found a consistent SPF
                    else:
                        # sectors_per_fat is too small, need to increase it
                        sectors_per_fat = spf_needed_for_this_many_clusters
                        # Loop again with the new sectors_per_fat
                else: # Loop completed without break (no consistent SPF found)
                    self.logger.error(f"Could not determine a consistent sectors_per_fat for FAT12. Data area too small or params conflicting.")
                    return None
                
                # Recalculate final_num_clusters with the chosen sectors_per_fat (should be same as last successful try)
                if num_fats > 0:
                    data_s = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)
                else:
                    data_s = available_for_fats_and_data

                if data_s < sectors_per_cluster: # Check again after final SPF
                    self.logger.error(f"Final data sectors ({data_s}) less than sectors_per_cluster ({sectors_per_cluster}). Cannot create profile.")
                    return None
                final_num_clusters = data_s // sectors_per_cluster


                if final_num_clusters <= 0:
                     self.logger.error(f"Final calculated non-positive number of clusters ({final_num_clusters}). Cannot create profile.")
                     return None
                if final_num_clusters > FAT12_MAX_CLUSTERS: # FAT12_MAX_CLUSTERS is 4084
                     self.logger.warning(f"Calculated cluster count ({final_num_clusters}) for custom FAT12 profile exceeds typical limit of {FAT12_MAX_CLUSTERS}. This may lead to issues.")

                filesystem_config_obj = FATVolumeInfo(
                    bytes_per_sector=bytes_per_sector,
                    sectors_per_cluster=sectors_per_cluster,
                    reserved_sectors=reserved_sectors,
                    num_fats=num_fats,
                    root_entries=root_entries,
                    total_sectors=total_sectors,
                    media_descriptor=format_info.get("media_descriptor", 0xF0), # Common for 1.44M
                    sectors_per_fat=sectors_per_fat,
                    sectors_per_track=physical_format.track_formats[0].sectors_per_track,
                    num_heads=physical_format.heads,
                    hidden_sectors=format_info.get("hidden_sectors", 0),
                    drive_number=format_info.get("drive_number", 0),
                    volume_serial=format_info.get("volume_serial", 0), # Can be auto-generated
                )
            elif target_fs_type == "CPM":
                # For CP/M, expect DPB parameters directly or derive sensible defaults
                # This requires CPMDiskParameterBlock to be defined and imported
                if 'CPMDiskParameterBlock' in globals():
                    filesystem_config_obj = CPMDiskParameterBlock(
                        spt=format_info.get("spt", physical_format.track_formats[0].sectors_per_track * (physical_format.bytes_per_sector // 128)), # Logical 128-byte sectors
                        bsh=format_info.get("bsh", 3), # Default 1K blocks (2^3 * 128)
                        blm=format_info.get("blm", (2**format_info.get("bsh", 3)) - 1),
                        exm=format_info.get("exm", 0), # Default 16KB extents
                        dsm=format_info.get("dsm", (physical_format.total_sectors * (physical_format.bytes_per_sector // 128)) // (2**format_info.get("bsh",3)) -10), # Rough estimate
                        drm=format_info.get("drm", 63), # Default 64 directory entries
                        al0=format_info.get("al0", 0xC0), # Default for 64 entries, 1K blocks
                        al1=format_info.get("al1", 0x00),
                        cks=format_info.get("cks", 0), # No checksum typically
                        off=format_info.get("off", 2)  # Default 2 reserved tracks
                    )
                else:
                    self.logger.warning("CPMDiskParameterBlock not available for custom CPM profile.")
                    filesystem_config_obj = None

            else:
                self.logger.warning(f"Custom profile creation for filesystem type '{target_fs_type}' is not fully implemented. Filesystem config will be None.")
                filesystem_config_obj = None
            
            profile_name = format_info.get("profile_name", "custom")
            profile_description = format_info.get("description", 
                f"Custom {physical_format.cylinders}x{physical_format.heads}x{physical_format.track_formats[0].sectors_per_track}x{physical_format.bytes_per_sector} ({target_fs_type})"
            )

            profile = FormatProfile(
                name=profile_name,
                description=profile_description,
                physical_format=physical_format,
                filesystem_type=target_fs_type,
                filesystem_config=filesystem_config_obj
            )
            self.logger.debug(f"Created custom format profile: {profile.description}")
            return profile
        except Exception as e:
            self.logger.error(f"Error creating custom profile: {e}", exc_info=True)
            return None
