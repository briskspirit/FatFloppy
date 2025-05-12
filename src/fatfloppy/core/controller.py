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
from .filesystems.fat12fs import Filesystem, FATFilesystem, FATVolumeInfo, FAT12_MAX_CLUSTERS
from .format_definitions import FLOPPY_FORMATS
from .filesystem_factory import create_filesystem

logger = get_logger()


class DiskController:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        self.known_formats = FLOPPY_FORMATS
        self.driver: Optional[DiskIODriver] = None
        self.explicit_format_set = False
        self.physical_format = None
        self.boot_sector = None

    def _create_physical_driver(self, source: str, drive_letter: str, drive_size: str) -> GreaseweazleDriver:
        device_name = source if source else None
        driver = GreaseweazleDriver(device_name=device_name, drive=drive_letter, drive_size=drive_size)
        try:
            driver.initialize()
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
        else:
            physical_format = self._create_physical_format(format_info, base_profile)

        self.driver.set_physical_format(physical_format)
        self.disk.set_geometry(physical_format)

        if isinstance(self.driver, GreaseweazleDriver):
            self.driver._create_and_set_custom_diskdef()
            self.logger.debug("Applied custom diskdef for Greaseweazle driver")

    def _adjust_geometry_after_filesystem(self) -> None:
        if self.filesystem and isinstance(self.filesystem, FATFilesystem):
            bs = self.filesystem.boot_sector
            if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                actual_sectors = self.driver.physical_format.track_formats[0].sectors_per_track
                if actual_sectors != self.disk.physical_format.track_formats[0].sectors_per_track:
                    updated_track_format = TrackFormat(
                        track_start=0,
                        track_end=self.disk.physical_format.cylinders - 1,
                        head_start=0,
                        head_end=self.disk.physical_format.heads - 1,
                        sectors_per_track=actual_sectors,
                        encoding=self.disk.physical_format.track_formats[0].encoding,
                        rate=self.disk.physical_format.track_formats[0].rate,
                        gap3_bytes=self.disk.physical_format.track_formats[0].gap3_bytes,
                        interleave=self.disk.physical_format.track_formats[0].interleave
                    )
                    updated_geometry = PhysicalFormat(
                        cylinders=self.disk.physical_format.cylinders,
                        heads=self.disk.physical_format.heads,
                        rpm=self.disk.physical_format.rpm,
                        heads_inverted=self.disk.physical_format.heads_inverted,
                        bytes_per_sector=self.disk.physical_format.bytes_per_sector,
                        track_formats=[updated_track_format]
                    )
                    self.set_geometry(updated_geometry)
                    self.logger.debug("Adjusted geometry based on driver physical format")
            elif (not self.explicit_format_set and bs and
                  hasattr(bs, 'sectors_per_track') and bs.sectors_per_track > 0 and
                  hasattr(bs, 'num_heads') and bs.num_heads > 0):
                sectors_per_track = bs.sectors_per_track
                heads = bs.num_heads
                if (self.disk.physical_format.track_formats[0].sectors_per_track != sectors_per_track or
                        self.disk.physical_format.heads != heads):
                    updated_track_format = TrackFormat(
                        track_start=0,
                        track_end=self.disk.physical_format.cylinders - 1,
                        head_start=0,
                        head_end=heads - 1,
                        sectors_per_track=sectors_per_track,
                        encoding=self.disk.physical_format.track_formats[0].encoding,
                        rate=self.disk.physical_format.track_formats[0].rate,
                        gap3_bytes=self.disk.physical_format.track_formats[0].gap3_bytes,
                        interleave=self.disk.physical_format.track_formats[0].interleave
                    )
                    updated_geometry = PhysicalFormat(
                        cylinders=self.disk.physical_format.cylinders,
                        heads=heads,
                        rpm=self.disk.physical_format.rpm,
                        heads_inverted=self.disk.physical_format.heads_inverted,
                        bytes_per_sector=self.disk.physical_format.bytes_per_sector,
                        track_formats=[updated_track_format]
                    )
                    self.set_geometry(updated_geometry)
                    self.logger.debug("Adjusted geometry based on boot sector")

    def _handle_format(self, format_info: dict, drive_size: str) -> bool:
        if isinstance(self.driver, IMDImageDriver):
            if not self.driver.physical_format:
                self.logger.error("IMD driver loaded but failed to derive physical format.")
                return False
            self.disk.set_geometry(self.driver.physical_format)
            self.filesystem = create_filesystem(self.disk)
            if format_info:
                self.logger.warning("Applying user format_info to an IMD disk, this may override IMD metadata.")
                try:
                    self._apply_user_format(format_info)
                    self.filesystem = create_filesystem(self.disk)
                except Exception as e:
                    self.logger.error(f"Failed to apply user format override to IMD: {e}")
                    return False
            return True
        elif format_info:
            try:
                self._apply_user_format(format_info)
                self.filesystem = create_filesystem(self.disk)
                if isinstance(self.filesystem, FATFilesystem):
                    self._adjust_geometry_after_filesystem()
                self.logger.debug(f"Applied user format. Geometry: {self.disk.physical_format}")
                return True
            except Exception as e:
                self.logger.error(f"Failed applying user format: {e}")
                return False
        else:
            if isinstance(self.driver, GreaseweazleDriver):
                success = self._detect_physical_disk_format(drive_size)
            elif isinstance(self.driver, IMGImageDriver):
                success = self._detect_image_file_format(self.driver.file_path)
            else:
                success = False
            if success:
                self.filesystem = create_filesystem(self.disk)
                if isinstance(self.filesystem, FATFilesystem):
                    self._adjust_geometry_after_filesystem()
                self.logger.debug(f"Auto-detected format. Geometry: {self.disk.physical_format}")
            return success

    def open_disk(self, source: str, disk_type: str = "IMG", drive_letter: str = "A", drive_size: str = "3.5", format_info: dict = None) -> bool:
        if self.disk:
            self.close_disk()
        self.explicit_format_set = bool(format_info)

        try:
            self.driver = self._create_driver(disk_type, source, drive_letter, drive_size)
            if isinstance(self.driver, (IMGImageDriver, IMDImageDriver)) and not self.driver.file_path and not self.driver.image_data:
                if disk_type == "IMG" and not os.path.exists(source):
                    self.logger.info(f"Raw image file {source} doesn't exist. Proceeding for potential format operation.")
                else:
                    self.logger.error(f"Driver initialization failed for {disk_type} at {source}")
                    self.driver = None
                    return False
            self.disk = Disk(self.driver)
            success = self._handle_format(format_info, drive_size)
            if not success or not self.disk or not self.disk.physical_format:
                self.logger.error(f"Failed to establish valid format/geometry for {source}")
                self.close_disk()
                return False

            self.filesystem = create_filesystem(self.disk)
            if isinstance(self.filesystem, FATFilesystem):
                self._adjust_geometry_after_filesystem()

            self.physical_format = self.disk.physical_format
            self.boot_sector = self.filesystem.boot_sector if self.filesystem and hasattr(self.filesystem, 'boot_sector') else None
            self.logger.info(f"Disk '{source}' opened successfully. Type: {disk_type}. Geometry: {self.physical_format}")
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
        self.boot_sector = None
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
        format_name, _ = self.detect_format()
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile:
                return profile.physical_format
        return None

    def set_geometry(self, geometry: PhysicalFormat) -> None:
        if not self.disk:
            self.logger.error("No disk opened to set geometry")
            raise ValueError("No disk opened")
        self.disk.set_geometry(geometry)
        self.logger.debug(f"Geometry set: {geometry}")

    def detect_format(self) -> Optional[Tuple[str, Any]]:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format")
            return None, None

        matched_profile_name: Optional[str] = None
        boot_info_obj: Optional[Any] = None

        try:
            if isinstance(self.driver, IMDImageDriver):
                boot_sector_bytes = self.driver.read_boot_sector_data()
                if not boot_sector_bytes:
                    self.logger.warning("IMD driver could not read boot sector data (0,0,1).")
                    return None, None
                try:
                    boot_info_obj = FATVolumeInfo.from_bytes(boot_sector_bytes)
                except ValueError as e:
                    self.logger.warning(f"Could not parse boot sector data from IMD as FAT BPB: {e}")
                    boot_info_obj = None

                if boot_info_obj:
                    for format_name, profile in self.known_formats.items():
                        if (profile.filesystem_type == "FAT12" and
                                profile.physical_format and self.driver.physical_format and
                                profile.filesystem_config and isinstance(profile.filesystem_config, FATVolumeInfo) and
                                profile.filesystem_config.sectors_per_track == boot_info_obj.sectors_per_track and
                                profile.filesystem_config.num_heads == boot_info_obj.num_heads and
                                profile.filesystem_config.total_sectors == boot_info_obj.total_sectors and
                                profile.filesystem_config.bytes_per_sector == boot_info_obj.bytes_per_sector and
                                profile.filesystem_config.sectors_per_fat == boot_info_obj.sectors_per_fat and
                                profile.filesystem_config.root_entries == boot_info_obj.root_entries and
                                profile.physical_format.cylinders == self.driver.physical_format.cylinders):
                            matched_profile_name = format_name
                            if self.disk.physical_format != profile.physical_format:
                                self.logger.info(f"Updating geometry to match detected profile '{format_name}' for IMD")
                                self.set_geometry(profile.physical_format)
                            break
                    if matched_profile_name:
                        self.logger.info(f"IMD matched FAT12 profile: {matched_profile_name}")
                    else:
                        self.logger.info("Parsed BPB from IMD, but no exact known FAT12 format profile match found.")
                return matched_profile_name, boot_info_obj
            else: # For other driver types (IMG, Physical)
                if not self.disk.physical_format:
                    self.logger.warning("No physical format set for detection. Applying temporary default.")
                    temp_profile = self.get_format_by_name("ibm_3.5_1.44m")
                    if temp_profile:
                        self.disk.set_geometry(temp_profile.physical_format) # Minimal set, won't persist if no match
                    else: # Fallback if "ibm_3.5_1.44m" isn't in known_formats
                        temp_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1, gap3_bytes=84)
                        temp_geom = PhysicalFormat(80, 2, 300, False, 512, [temp_tf])
                        self.disk.set_geometry(temp_geom)
                    if not self.disk.physical_format: # Still no format
                        self.logger.error("Failed to set even a temporary default geometry.")
                        return None, None

                if self.filesystem and isinstance(self.filesystem, FATFilesystem):
                    boot_info_obj = self.filesystem.boot_sector
                else:
                    try:
                        raw_boot_sector_bytes = self.disk.read_sector(0, 0, 1)
                        if raw_boot_sector_bytes and len(raw_boot_sector_bytes) >= 62: # Basic check for BPB presence
                            boot_info_obj = FATVolumeInfo.from_bytes(raw_boot_sector_bytes)
                        else:
                            self.logger.warning("Failed to read sufficient boot sector data for detection.")
                            boot_info_obj = None
                    except Exception as read_e:
                        self.logger.error(f"Failed to read boot sector (0,0,1) for detection: {read_e}")
                        boot_info_obj = None

                if boot_info_obj and isinstance(boot_info_obj, FATVolumeInfo):
                    for format_name, profile in self.known_formats.items():
                        if (profile.filesystem_type == "FAT12" and
                                profile.physical_format and profile.filesystem_config and
                                isinstance(profile.filesystem_config, FATVolumeInfo) and
                                self.disk.physical_format and
                                profile.physical_format.cylinders == self.disk.physical_format.cylinders and
                                profile.physical_format.heads == boot_info_obj.num_heads and
                                profile.physical_format.bytes_per_sector == boot_info_obj.bytes_per_sector and
                                profile.physical_format.get_sectors_per_track(0, 0) == boot_info_obj.sectors_per_track and
                                profile.filesystem_config.total_sectors == boot_info_obj.total_sectors and
                                profile.filesystem_config.sectors_per_fat == boot_info_obj.sectors_per_fat and
                                profile.filesystem_config.root_entries == boot_info_obj.root_entries):
                            matched_profile_name = format_name
                            if self.disk.physical_format != profile.physical_format:
                                self.logger.info(f"Aligning geometry to matched FAT12 profile '{format_name}'")
                                self.set_geometry(profile.physical_format)
                            break
                    if matched_profile_name:
                        self.logger.info(f"Detected FAT12 profile: {matched_profile_name}")
                    else:
                        self.logger.info("Parsed BPB data, but no exact known FAT12 format profile match found.")
                return matched_profile_name, boot_info_obj
        except Exception as e:
            self.logger.exception(f"Error during format detection: {e}")
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
        self.disk.set_geometry(profile.physical_format)
        if hasattr(self.driver, "set_physical_format"):
            physical_format_copy = copy.deepcopy(profile.physical_format)
            if profile.filesystem_config:
                physical_format_copy._associated_boot_sector = profile.filesystem_config
            try:
                self.driver.set_physical_format(physical_format_copy)
            finally:
                if hasattr(physical_format_copy, '_associated_boot_sector'):
                    delattr(physical_format_copy, '_associated_boot_sector')
            if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
                self.logger.debug("Applying Greaseweazle custom diskdef after set_format.")
                self.driver._create_and_set_custom_diskdef()
        else:
            self.logger.warning(f"Driver type {type(self.driver).__name__} does not support set_physical_format.")
        self.physical_format = self.disk.physical_format

    def get_allocated_units(self) -> List[int]:
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_units"):
            return []
        try:
            clusters = self.filesystem.get_allocated_units()
            self.logger.debug(f"Retrieved {len(clusters)} allocated clusters")
            return clusters
        except Exception as e:
            self.logger.error(f"Error getting allocated clusters: {e}")
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
            self.logger.error("Formatting is not supported directly via the IMDImageDriver. Create a raw image first, format it, then convert to IMD if needed.")
            return False
        if not self.disk or not self.driver:
            self.logger.error("Cannot format disk: Disk or driver not initialized.")
            return False

        profile = self.known_formats.get(format_name)
        if not profile:
            found_profile = False
            if self.disk.physical_format:
                for name, prof in self.known_formats.items():
                    if prof.physical_format == self.disk.physical_format:
                        profile = prof
                        format_name = name
                        found_profile = True
                        self.logger.info(f"Using format profile '{format_name}' matching current geometry.")
                        break
            if not found_profile:
                if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                    if hasattr(self.driver.physical_format, '_associated_boot_sector'):
                        self.logger.info("Using custom format profile potentially set via set_format.")
                        profile = FormatProfile(
                            name="custom_runtime",
                            description="Custom (Runtime)",
                            physical_format=self.driver.physical_format,
                            filesystem_type="FAT12", # Assuming FAT12 for now
                            filesystem_config=self.driver.physical_format._associated_boot_sector
                        )
                        format_name = "custom_runtime"
                    else:
                        self.logger.error(f"Cannot format with unknown/custom profile '{format_name}' without associated boot sector info attached to driver's format.")
                        return False
                else:
                    self.logger.error(f"Cannot format with unknown profile '{format_name}' and no geometry set or driver format available.")
                    return False

        if not profile or not profile.physical_format or not profile.filesystem_config:
            self.logger.error(f"Format profile '{format_name}' is invalid or missing required information.")
            return False

        if profile.filesystem_type != "FAT12":
            self.logger.error(f"Formatting for filesystem type '{profile.filesystem_type}' is not supported.")
            return False

        self.logger.info(f"Starting format process with profile: {profile.name}")
        if volume_label:
            profile.filesystem_config.volume_label = volume_label.ljust(11)[:11]
        elif not profile.filesystem_config.volume_label or not profile.filesystem_config.volume_label.strip():
            profile.filesystem_config.volume_label = "NO NAME".ljust(11)

        try:
            if self.disk.physical_format != profile.physical_format:
                self.logger.warning(f"Disk geometry differs from profile '{profile.name}' before format. Setting format now.")
                self.set_format(profile)
            elif hasattr(self.driver, "physical_format") and self.driver.physical_format != profile.physical_format:
                self.logger.warning(f"Driver physical format differs from profile '{profile.name}'. Setting format now.")
                self.set_format(profile)

            filesystem = FATFilesystem(self.disk)
            filesystem.format_fs(profile)
            self.filesystem = filesystem
            self.physical_format = self.disk.physical_format
            self.boot_sector = self.filesystem.boot_sector
            self.flush()
            self.logger.info(f"Disk formatting complete for profile '{profile.name}'. Volume: '{profile.filesystem_config.volume_label.strip()}'")
            return True
        except Exception as e:
            self.logger.exception(f"Error formatting disk with profile '{profile.name}': {e}")
            self.filesystem = None
            self.boot_sector = None
            return False

    def create_and_format_image(self, file_path: str, profile: FormatProfile, volume_label: str = "NO NAME", disk_type: str = "IMG") -> bool:
        if self.disk:
            self.close_disk()
        if not profile or not profile.physical_format or not profile.filesystem_config:
            self.logger.error("Invalid or incomplete profile provided for image creation.")
            return False

        if profile.filesystem_type != "FAT12":
            self.logger.error(f"Formatting with filesystem type '{profile.filesystem_type}' is not supported for image creation.")
            return False

        if volume_label:
            profile.filesystem_config.volume_label = volume_label.ljust(11)[:11]
        elif not profile.filesystem_config.volume_label or not profile.filesystem_config.volume_label.strip():
            profile.filesystem_config.volume_label = "NO NAME".ljust(11)

        try:
            if disk_type == "IMG":
                total_bytes = profile.physical_format.total_bytes
                self.logger.info(f"Creating raw image file '{file_path}' with size {total_bytes} bytes.")
                with open(file_path, 'wb') as f:
                    f.truncate(total_bytes)
                self.driver = self._create_img_driver(file_path)
                if not self.driver or len(self.driver.image_data) != total_bytes:
                    raise IOError(f"Failed to create or correctly size raw image file '{file_path}'")
                self.disk = Disk(self.driver)
                self.logger.info(f"Setting format for new raw image using profile: {profile.name}")
                self.set_format(profile)
                self.logger.info(f"Formatting new raw image with volume label: '{profile.filesystem_config.volume_label.strip()}'")
                filesystem = FATFilesystem(self.disk)
                filesystem.format_fs(profile)
                self.filesystem = filesystem
                self.physical_format = self.disk.physical_format
                self.boot_sector = self.filesystem.boot_sector

                self.flush()
                self.logger.info(f"Successfully created and formatted raw image '{file_path}'")
                return True
            elif disk_type == "IMD":
                self.driver = self._create_imd_driver(file_path)
                self.disk = Disk(self.driver)
                self.logger.info(f"Setting format for new IMD image using profile: {profile.name}")
                self.set_format(profile)
                self.logger.info(f"Formatting new IMD image with volume label: '{profile.filesystem_config.volume_label.strip()}'")
                filesystem = FATFilesystem(self.disk)
                filesystem.format_fs(profile)
                self.filesystem = filesystem
                self.physical_format = self.disk.physical_format
                self.boot_sector = self.filesystem.boot_sector
                self.flush()
                self.logger.info(f"Successfully created and formatted IMD image '{file_path}'")
                return True
            else:
                self.logger.error(f"Unsupported disk type: {disk_type}")
                return False
        except Exception as e:
            self.logger.exception(f"Error during create_and_format_image: {e}")
            self.close_disk()
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    self.logger.warning(f"Could not remove partially created image file: {file_path}")
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
            cylinders = 80
            sectors_per_track = 18
            bytes_per_sector = 512
            rate = 500
            encoding = "MFM"
            rpm = 300
        elif drive_size == "5.25":
            cylinders = 40
            sectors_per_track = 9
            bytes_per_sector = 512
            rate = 250
            encoding = "MFM"
            rpm = 300
        elif drive_size == "8":
            cylinders = 77
            sectors_per_track = 26
            bytes_per_sector = 128
            rate = 250
            encoding = "FM"
            rpm = 360
        else:
            raise ValueError(f"Unsupported drive size: {drive_size}")
        track_format = TrackFormat(
            track_start=0,
            track_end=cylinders - 1,
            head_start=0,
            head_end=1,
            sectors_per_track=sectors_per_track,
            encoding=encoding,
            rate=rate,
            gap3_bytes=84,
            interleave=1
        )
        geometry = PhysicalFormat(
            cylinders=cylinders,
            heads=2,
            rpm=rpm,
            heads_inverted=False,
            bytes_per_sector=bytes_per_sector,
            track_formats=[track_format]
        )
        self.logger.debug(f"Created default geometry for drive size {drive_size}")
        return geometry, geometry

    def _check_second_head(self, temp_profile: FormatProfile) -> bool:
        if self.filesystem and hasattr(self.filesystem, 'boot_sector'):
            bs = self.filesystem.boot_sector
            if hasattr(bs, 'num_heads') and bs.num_heads > 0:
                return bs.num_heads > 1
        self.set_format(temp_profile)
        if hasattr(self.driver, '_read_track') and isinstance(self.driver, GreaseweazleDriver):
            try:
                success = self.driver._read_track(0, 1)
                return bool(success and self.driver.track_data.get((0, 1), {}))
            except Exception:
                self.logger.warning("Error reading track; assuming single-sided")
                return False
        try:
            self.disk.read_sector(0, 1, 1)
            return True
        except Exception:
            return False

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
        for profile in filtered_formats:
            try:
                self.set_format(profile)
                self.filesystem = create_filesystem(self.disk)
                if self.filesystem:
                    if isinstance(self.filesystem, FATFilesystem):
                        self._adjust_geometry_after_filesystem()
                    self.logger.debug(f"Adjusted geometry after filesystem: {self.disk.physical_format}")
                    return profile
            except Exception:
                continue
        return None

    def _detect_physical_disk_format(self, drive_size: str = "3.5") -> bool:
        temp_geometry, temp_physical = self._get_default_geometry_and_physical(drive_size)
        temp_profile = FormatProfile(
            name="temp_detect",
            description="Temporary for detection",
            filesystem_type="FAT12", # Assuming FAT12 for temp
            physical_format=temp_geometry
        )
        self.set_format(temp_profile)
        if hasattr(self.driver, 'initialize'):
            self.driver.initialize()
        try:
            self.filesystem = create_filesystem(self.disk)
            if self.filesystem and isinstance(self.filesystem, FATFilesystem):
                bs = self.filesystem.boot_sector
                if hasattr(bs, 'num_heads') and bs.num_heads > 0:
                    heads = bs.num_heads
                    updated_track_format = TrackFormat(
                        track_start=0,
                        track_end=temp_geometry.cylinders - 1,
                        head_start=0,
                        head_end=heads - 1,
                        sectors_per_track=temp_geometry.track_formats[0].sectors_per_track,
                        encoding=temp_geometry.track_formats[0].encoding,
                        rate=temp_geometry.track_formats[0].rate,
                        gap3_bytes=temp_geometry.track_formats[0].gap3_bytes,
                        interleave=temp_geometry.track_formats[0].interleave
                    )
                    updated_geometry = PhysicalFormat(
                        cylinders=temp_geometry.cylinders,
                        heads=heads,
                        rpm=temp_geometry.rpm,
                        heads_inverted=temp_geometry.heads_inverted,
                        bytes_per_sector=temp_geometry.bytes_per_sector,
                        track_formats=[updated_track_format]
                    )
                    self.set_geometry(updated_geometry)
                    return True
        except Exception:
            self.logger.debug("Initial filesystem detection failed; proceeding with head detection")
        has_second_head = self._check_second_head(temp_profile)
        filtered_formats = self._filter_known_formats(drive_size, has_second_head)
        matching_profile = self._find_matching_format(filtered_formats)
        if matching_profile:
            self.set_format(matching_profile)
            return True
        default_heads = 2 if has_second_head else 1
        fallback_track_format = TrackFormat(
            track_start=0,
            track_end=temp_geometry.cylinders - 1,
            head_start=0,
            head_end=default_heads - 1,
            sectors_per_track=temp_geometry.track_formats[0].sectors_per_track,
            encoding=temp_geometry.track_formats[0].encoding,
            rate=temp_geometry.track_formats[0].rate,
            gap3_bytes=temp_geometry.track_formats[0].gap3_bytes,
            interleave=temp_geometry.track_formats[0].interleave
        )
        fallback_geometry = PhysicalFormat(
            cylinders=temp_geometry.cylinders,
            heads=default_heads,
            rpm=temp_geometry.rpm,
            heads_inverted=temp_geometry.heads_inverted,
            bytes_per_sector=temp_geometry.bytes_per_sector,
            track_formats=[fallback_track_format]
        )
        fallback_profile = FormatProfile(
            name="fallback_detected",
            description="Fallback based on detection",
            filesystem_type="FAT12", # Defaulting to FAT12
            physical_format=fallback_geometry
        )
        self.set_format(fallback_profile)
        return True

    def _detect_image_file_format(self, file_path: str) -> bool:
        format_name, boot_data = self.detect_format()
        if not boot_data or not isinstance(boot_data, FATVolumeInfo): # Ensure it's FATVolumeInfo for now
            self.logger.error("Could not detect a valid FAT boot sector for image file.")
            return False

        self.logger.info(f"BPB: bytes_per_sector={boot_data.bytes_per_sector}, sectors_per_track={boot_data.sectors_per_track}, num_heads={boot_data.num_heads}, total_sectors={boot_data.total_sectors}")
        file_size = os.path.getsize(file_path)
        if boot_data.bytes_per_sector == 0: # Prevent division by zero
            self.logger.error("Bytes per sector is zero in BPB, cannot calculate geometry.")
            return False
        total_sectors_from_file = file_size // boot_data.bytes_per_sector

        # Use total_sectors_from_file if BPB total_sectors is zero or clearly wrong.
        # This is a common issue with some images.
        if boot_data.total_sectors == 0 or abs(boot_data.total_sectors - total_sectors_from_file) > (boot_data.num_heads * boot_data.sectors_per_track * 2): # Heuristic for significant mismatch
            self.logger.warning(f"BPB total_sectors ({boot_data.total_sectors}) differs significantly from file size derived sectors ({total_sectors_from_file}). Using file size.")
            effective_total_sectors = total_sectors_from_file
        else:
            effective_total_sectors = boot_data.total_sectors


        if boot_data.num_heads == 0 or boot_data.sectors_per_track == 0:
             self.logger.error("Number of heads or sectors per track is zero in BPB, cannot calculate cylinders.")
             return False

        cylinders = effective_total_sectors // (boot_data.num_heads * boot_data.sectors_per_track)
        format_info = {
            "cylinders": cylinders,
            "heads": boot_data.num_heads,
            "sectors_per_track": boot_data.sectors_per_track,
            "bytes_per_sector": boot_data.bytes_per_sector,
            "encoding": "MFM",
            "rate": 500,
            "rpm": 360,
            "gap3_bytes": 84,
            "interleave": 1
        }
        physical_format = self._create_physical_format(format_info)
        self.disk.set_geometry(physical_format)
        self.driver.set_physical_format(physical_format)
        self.logger.info(f"Geometry set: bytes_per_sector={physical_format.bytes_per_sector}, sectors_per_track={physical_format.track_formats[0].sectors_per_track}, heads={physical_format.heads}, cylinders={physical_format.cylinders}")
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile:
                self.set_format(profile)
        self.filesystem = create_filesystem(self.disk)
        if isinstance(self.filesystem, FATFilesystem):
            self._adjust_geometry_after_filesystem()
        logger.debug(f"Adjusted geometry after filesystem: {self.disk.physical_format}")
        return True

    def create_custom_profile(self, format_info: Dict[str, any]) -> Optional[FormatProfile]:
        try:
            physical_format = self._create_physical_format(format_info)
            total_sectors = physical_format.total_sectors
            bytes_per_sector = physical_format.bytes_per_sector
            sectors_per_cluster = format_info.get("sectors_per_cluster", 1)
            reserved_sectors = format_info.get("reserved_sectors", 1)
            num_fats = format_info.get("num_fats", 2)
            root_entries = format_info.get("root_entries", 224 if total_sectors > 1440 else 112)
            root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector
            data_sectors = total_sectors - (reserved_sectors + (num_fats * root_dir_sectors) + root_dir_sectors) # Error in original, num_fats should be multiplied by sectors_per_fat

            # Corrected calculation for sectors_per_fat and data_sectors
            # Temp calculation for sectors_per_fat to estimate num_clusters first
            # This is a bit of a circular dependency for FAT12, so we estimate.
            # A more robust way would be to calculate num_clusters first.

            # Calculate num_clusters first
            temp_data_area_sectors = total_sectors - (reserved_sectors + root_dir_sectors) # Exclude FATs for now
            num_clusters_rough = temp_data_area_sectors // sectors_per_cluster

            # Estimate sectors_per_fat based on this rough cluster count for FAT12
            # For FAT12, each entry is 1.5 bytes (12 bits).
            # Total FAT size in bytes = (num_clusters * 1.5)
            # Add a few bytes for media descriptor and end-of-chain markers
            bytes_per_fat_estimate = (num_clusters_rough * 3 + 1) // 2 + 3 # Add 3 for safety (media desc, 2 EOC markers)
            sectors_per_fat = (bytes_per_fat_estimate + bytes_per_sector - 1) // bytes_per_sector

            # Recalculate data sectors and num_clusters with this sectors_per_fat
            data_sectors = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)
            num_clusters = data_sectors // sectors_per_cluster

            if num_clusters > FAT12_MAX_CLUSTERS:
                self.logger.error(f"Cluster count ({num_clusters}) exceeds FAT12 limit. Recalculating sectors_per_cluster.")
                # Attempt to adjust sectors_per_cluster to fit FAT12_MAX_CLUSTERS
                # This is a simple heuristic, might not always be optimal
                if data_sectors > 0:
                    sectors_per_cluster = (data_sectors + FAT12_MAX_CLUSTERS -1) // FAT12_MAX_CLUSTERS
                    if sectors_per_cluster == 0: sectors_per_cluster = 1 # Ensure at least 1
                    num_clusters = data_sectors // sectors_per_cluster
                    if num_clusters > FAT12_MAX_CLUSTERS:
                         self.logger.error(f"Still too many clusters ({num_clusters}) after adjustment. Cannot create valid FAT12 profile.")
                         return None
                    self.logger.info(f"Adjusted sectors_per_cluster to {sectors_per_cluster} to fit FAT12 limits.")
                else:
                    self.logger.error("No data sectors available, cannot create FAT12 profile.")
                    return None


            # Final calculation for sectors_per_fat with the new num_clusters
            bytes_per_fat = (num_clusters * 3 + 1) // 2 + 3 # Add 3 for media_descriptor and 0xFF, 0xFF
            sectors_per_fat = (bytes_per_fat + bytes_per_sector - 1) // bytes_per_sector

            # Final check on data_sectors and num_clusters
            data_sectors = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)
            num_clusters = data_sectors // sectors_per_cluster

            if data_sectors <=0 or num_clusters <= 0:
                self.logger.error(f"Calculated data sectors ({data_sectors}) or num_clusters ({num_clusters}) is non-positive. Cannot create profile.")
                return None


            filesystem_config = FATVolumeInfo(
                bytes_per_sector=bytes_per_sector,
                sectors_per_cluster=sectors_per_cluster,
                reserved_sectors=reserved_sectors,
                num_fats=num_fats,
                root_entries=root_entries,
                total_sectors=total_sectors,
                media_descriptor=format_info.get("media_descriptor", 0xF0),
                sectors_per_fat=sectors_per_fat,
                sectors_per_track=physical_format.track_formats[0].sectors_per_track,
                num_heads=physical_format.heads,
                hidden_sectors=format_info.get("hidden_sectors", 0),
                drive_number=format_info.get("drive_number", 0),
                volume_serial=format_info.get("volume_serial", 0),
            )
            profile = FormatProfile(
                name="custom",
                description=f"Custom {physical_format.cylinders}x{physical_format.heads}x{physical_format.track_formats[0].sectors_per_track}x{physical_format.bytes_per_sector}",
                physical_format=physical_format,
                filesystem_type="FAT12",
                filesystem_config=filesystem_config
            )
            self.logger.debug(f"Created custom format profile: {profile.description} with {num_clusters} clusters.")
            return profile
        except Exception as e:
            self.logger.error(f"Error creating custom profile: {e}", exc_info=True)
            return None
