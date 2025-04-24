# src/fatfloppy/core/controller.py
import os
from typing import List, Optional, Tuple

from .utils.logging_config import get_logger
from .disk import Disk
from .drivers import DiskIODriver, GreaseweazleDriver, RawImageDriver
from .physical_format import PhysicalFormat
from .formats import FATVolumeInfo, FormatProfile
from .filesystem import Filesystem, FATFilesystem
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
        self.geometry = None
        self.boot_sector = None

    def _create_physical_driver(self, source: str, drive_letter: str, drive_size: str) -> GreaseweazleDriver:
        device_name = source if source else None
        driver = GreaseweazleDriver(device_name=device_name, drive=drive_letter, drive_size=drive_size)
        try:
            driver.initialize()
        except Exception as e:
            self.logger.error(f"Error initializing Greaseweazle driver: {e}")
            raise
        return driver

    def _create_image_driver(self, source: str) -> RawImageDriver:
        if not os.path.exists(source):
            raise FileNotFoundError(f"Image file not found: {source}")
        return RawImageDriver(file_path=source)

    def _create_driver(self, disk_type: str, source: str, drive_letter: str, drive_size: str) -> DiskIODriver:
        if disk_type == "physical":
            return self._create_physical_driver(source, drive_letter, drive_size)
        elif disk_type == "image":
            return self._create_image_driver(source)
        else:
            raise ValueError(f"Unsupported disk type: {disk_type}")

    def _apply_user_format(self, format_info: dict) -> None:
        self.explicit_format_set = True
        format_name = format_info.get("format_name")
        base_profile = self.get_format_by_name(format_name) if format_name else None
        default_phys = PhysicalFormat(encoding="MFM", rate=500, rpm=300, gap3=84, cskew=0, interleave=1, cylinders=80, heads=2, sectors_per_track=18, bytes_per_sector=512)
        if base_profile and base_profile.physical_format:
            default_phys = base_profile.physical_format
        physical_format = PhysicalFormat(
            encoding=format_info.get("encoding", default_phys.encoding),
            rate=format_info.get("rate", default_phys.rate),
            rpm=format_info.get("rpm", default_phys.rpm),
            gap3=format_info.get("gap3", default_phys.gap3),
            cskew=format_info.get("cskew", default_phys.cskew),
            interleave=format_info.get("interleave", default_phys.interleave),
            cylinders=format_info.get("cylinders", default_phys.cylinders),
            heads=format_info.get("heads", default_phys.heads),
            sectors_per_track=format_info.get("sectors_per_track", default_phys.sectors_per_track),
            bytes_per_sector=format_info.get("bytes_per_sector", default_phys.bytes_per_sector)
        )
        self.driver.set_physical_format(physical_format)
        self.disk.set_geometry(physical_format)
        if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
            self.driver._create_and_set_custom_diskdef(physical_format.cylinders)

    def _adjust_geometry_after_filesystem(self) -> None:
        if self.filesystem and isinstance(self.filesystem, FATFilesystem):
            bs = self.filesystem.boot_sector
            if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                actual_sectors = self.driver.physical_format.sectors_per_track
                if actual_sectors != self.disk.geometry.sectors_per_track:
                    updated_geometry = PhysicalFormat(
                        encoding=self.disk.geometry.encoding,
                        rate=self.disk.geometry.rate,
                        rpm=self.disk.geometry.rpm,
                        gap3=self.disk.geometry.gap3,
                        cskew=self.disk.geometry.cskew,
                        interleave=self.disk.geometry.interleave,
                        cylinders=self.disk.geometry.cylinders,
                        heads=self.disk.geometry.heads,
                        sectors_per_track=actual_sectors,
                        bytes_per_sector=self.disk.geometry.bytes_per_sector
                    )
                    self.set_geometry(updated_geometry)
            elif (not self.explicit_format_set and bs and
                  hasattr(bs, 'sectors_per_track') and bs.sectors_per_track > 0 and
                  hasattr(bs, 'num_heads') and bs.num_heads > 0):
                sectors_per_track = bs.sectors_per_track
                heads = bs.num_heads
                if (self.disk.geometry.sectors_per_track != sectors_per_track or
                    self.disk.geometry.heads != heads):
                    updated_geometry = PhysicalFormat(
                        encoding=self.disk.geometry.encoding,
                        rate=self.disk.geometry.rate,
                        rpm=self.disk.geometry.rpm,
                        gap3=self.disk.geometry.gap3,
                        cskew=self.disk.geometry.cskew,
                        interleave=self.disk.geometry.interleave,
                        cylinders=self.disk.geometry.cylinders,
                        heads=heads,
                        sectors_per_track=sectors_per_track,
                        bytes_per_sector=self.disk.geometry.bytes_per_sector
                    )
                    self.set_geometry(updated_geometry)

    def _handle_format(self, format_info: dict, drive_size: str) -> bool:
        if format_info:
            self._apply_user_format(format_info)
            self.filesystem = create_filesystem(self.disk)
            self._adjust_geometry_after_filesystem()
            logger.debug(f"Adjusted geometry after filesystem: {self.disk.geometry}")
            return True
        else:
            if isinstance(self.driver, GreaseweazleDriver):
                return self._detect_physical_disk_format(drive_size)
            elif isinstance(self.driver, RawImageDriver):
                return self._detect_image_file_format(self.driver.file_path)
            return False

    def open_disk(self, source: str, disk_type: str = "image", drive_letter: str = "A", drive_size: str = "3.5", format_info: dict = None) -> bool:
        if self.disk:
            self.close_disk()
        self.explicit_format_set = False

        try:
            self.driver = self._create_driver(disk_type, source, drive_letter, drive_size)
            self.disk = Disk(self.driver)
            success = self._handle_format(format_info, drive_size)
            if not success:
                return False
            self.geometry = self.disk.geometry if self.disk else None
            self.boot_sector = self.filesystem.boot_sector if self.filesystem else None
            return True
        except Exception as e:
            self.logger.exception(f"Error opening disk: {e}")
            self.close_disk()
            return False

    def close_disk(self) -> None:
        if self.disk and self.driver:
            try:
                self.driver.flush()
            except Exception as e:
                self.logger.error(f"Error flushing driver: {e}")
        self.disk = None
        self.filesystem = None
        self.driver = None

    def detect_geometry(self) -> Optional[PhysicalFormat]:
        if not self.disk:
            self.logger.error("No disk opened to detect geometry")
            return None
        format_name = self.detect_format()
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

    def detect_format(self) -> Optional[Tuple[str, FATVolumeInfo]]:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format")
            return None, None
        try:
            # Set a temporary geometry if none exists
            if not self.disk.geometry:
                temp_geom = PhysicalFormat(
                    encoding="MFM", rate=250, rpm=300, cylinders=80, heads=2,
                    sectors_per_track=18, bytes_per_sector=512  # Default to 512
                )
                self.disk.set_geometry(temp_geom)
                if hasattr(self.driver, "set_physical_format") and not getattr(self.driver, "physical_format", None):
                    self.driver.set_physical_format(temp_geom)
            self.logger.debug(f"Before adjustment, geometry bytes_per_sector={self.disk.geometry.bytes_per_sector}")

            # Read the boot sector (512 bytes)
            boot_sector_bytes = self.disk.read_boot_sector()
            if not boot_sector_bytes:
                self.logger.warning("Failed to read sufficient boot sector data.")
                return None, None

            try:
                # Parse the BPB
                boot_data = FATVolumeInfo.from_bytes(boot_sector_bytes)

                # Adjust geometry based on BPB values
                actual_sector_size = boot_data.bytes_per_sector
                if actual_sector_size != self.disk.geometry.bytes_per_sector:
                    self.logger.info(f"Adjusting sector size from {self.disk.geometry.bytes_per_sector} to {actual_sector_size}")
                    self.disk.geometry.bytes_per_sector = actual_sector_size
                    if hasattr(self.driver, "set_physical_format"):
                        self.driver.physical_format.bytes_per_sector = actual_sector_size

                # Match against known formats, including bytes_per_sector
                for format_name, profile in self.known_formats.items():
                    if (profile.boot_sector and
                        profile.boot_sector.sectors_per_track == boot_data.sectors_per_track and
                        profile.boot_sector.num_heads == boot_data.num_heads and
                        profile.boot_sector.total_sectors == boot_data.total_sectors and
                        profile.boot_sector.bytes_per_sector == boot_data.bytes_per_sector):
                        return format_name, boot_data
                # Return parsed BPB if no known format matches
                return None, boot_data
            except ValueError as e:
                self.logger.warning(f"Could not parse boot sector data: {e}")
                return None, None
        except Exception as e:
            self.logger.error(f"Error during format detection: {e}", exc_info=True)
            return None, None

    def set_format(self, profile: FormatProfile) -> None:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to set format")
            raise ValueError("No disk opened")
        self.disk.set_geometry(profile.physical_format)
        import copy
        physical_format_copy = copy.deepcopy(profile.physical_format)
        self.driver.set_physical_format(physical_format_copy)
        if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
            self.driver._create_and_set_custom_diskdef(profile.physical_format.cylinders)

    def get_allocated_clusters(self) -> List[int]:
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_clusters"):
            return []
        try:
            clusters = self.filesystem.get_allocated_clusters()
            return clusters
        except Exception as e:
            self.logger.error(f"Error getting allocated clusters: {e}")
            return []

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            return None
        try:
            space_info = self.filesystem.get_free_space()
            return space_info
        except Exception as e:
            self.logger.error(f"Error getting free space: {e}")
            return None

    def list_directory(self, path: str = "/") -> List[dict]:
        if not self.filesystem:
            return []
        try:
            items = self.filesystem.list_directory(path)
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
            return data
        except ValueError as e:
            return None
        except Exception as e:
            self.logger.error(f"Error reading file {path}: {e}")
            return None

    def write_file(self, path: str, data: bytes) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.write_file(path, data)
            return True
        except Exception as e:
            self.logger.error(f"Error writing file {path}: {e}")
            return False

    def create_directory(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.create_directory(path)
            return True
        except Exception as e:
            self.logger.error(f"Error creating directory {path}: {e}")
            return False

    def delete_item(self, path: str) -> bool:
        if not self.filesystem:
            return False
        try:
            self.filesystem.delete(path)
            return True
        except Exception as e:
            self.logger.error(f"Error deleting item {path}: {e}")
            return False

    def format_disk(self, format_name: str, volume_label: str = "NO NAME") -> bool:
        if not self.disk or not self.driver:
             self.logger.error("Cannot format disk: Disk or driver not initialized.")
             return False
        if format_name not in self.known_formats:
             profile = None
             if hasattr(self.disk, 'geometry') and self.disk.geometry:
                  for name, prof in self.known_formats.items():
                       if prof.physical_format == self.disk.geometry:
                            profile = prof
                            format_name = name # Use the found name
                            break
                  if not profile: # Still not found, must be custom
                       if hasattr(self.driver, 'physical_format') and hasattr(self.driver.physical_format, '_associated_boot_sector'):
                            # A potential (hacky) way to pass the boot sector info through set_format
                            profile = FormatProfile(name="custom", description="Custom",
                                                    physical_format=self.driver.physical_format,
                                                    boot_sector=self.driver.physical_format._associated_boot_sector)
                       else:
                            self.logger.error(f"Cannot format with unknown profile '{format_name}' without associated boot sector info.")
                            return False
             else:
                  self.logger.error(f"Cannot format with unknown profile '{format_name}' and no geometry set.")
                  return False
        else:
             profile = self.known_formats[format_name]

        if not profile or not profile.boot_sector:
            self.logger.error(f"Format profile '{format_name}' is invalid or missing boot sector information.")
            return False

        self.logger.debug(f"Formatting with profile: {profile.name}, BPS: {profile.boot_sector.bytes_per_sector}")
        if volume_label:
            profile.boot_sector.volume_label = volume_label.ljust(11)[:11]
        else:
            profile.boot_sector.volume_label = "NO NAME".ljust(11)

        try:
            if self.disk.geometry != profile.physical_format:
                 self.logger.warning(f"Disk geometry doesn't match profile '{profile.name}' during format. Re-setting.")
                 self.disk.set_geometry(profile.physical_format)
                 if hasattr(self.driver, "set_physical_format"):
                     self.driver.set_physical_format(profile.physical_format)

            filesystem = FATFilesystem(self.disk)
            filesystem.format_fs(profile)

            self.filesystem = filesystem
            self.geometry = self.disk.geometry # Geometry should match profile now
            self.boot_sector = self.filesystem.boot_sector # Get the actual BS written

            self.logger.info(f"Disk formatting complete for profile '{profile.name}'.")
            return True
        except Exception as e:
            self.logger.exception(f"Error formatting disk with profile '{profile.name}': {e}")
            # Reset filesystem state on error
            self.filesystem = None
            self.boot_sector = None
            return False

    def create_and_format_image(self, file_path: str, profile: FormatProfile, volume_label: str = "NO NAME") -> bool:
        if self.disk:
            self.close_disk() # Close any existing disk first

        if not profile or not profile.physical_format or not profile.boot_sector:
            self.logger.error("Invalid or incomplete profile provided for image creation.")
            return False
        # Ensure volume label is correctly formatted early
        if volume_label:
             profile.boot_sector.volume_label = volume_label.ljust(11)[:11]

        try:
            total_bytes = profile.physical_format.total_bytes
            self.logger.info(f"Creating image file '{file_path}' with size {total_bytes} bytes.")
            with open(file_path, 'wb') as f:
                f.write(b'\0' * total_bytes)

            self.driver = self._create_image_driver(file_path)
            self.disk = Disk(self.driver)

            self.logger.info(f"Setting format for new image using profile: {profile.name}")
            self.set_format(profile) # Use the full profile

            self.logger.info(f"Formatting new image with volume label: '{profile.boot_sector.volume_label}'")
            success = self.format_disk(profile.name, profile.boot_sector.volume_label) # Pass name and corrected label

            if not success:
                self.logger.error(f"Formatting step failed for new image '{file_path}' with profile '{profile.name}'")
                self.close_disk() # Reset controller state
                return False

            self.logger.info(f"Successfully created and formatted image '{file_path}'")
            return True

        except Exception as e:
            self.logger.exception(f"Error during create_and_format_image: {e}")
            self.close_disk() # Ensure controller is reset on error
            return False

    def list_formats(self) -> List[Tuple[str, str]]:
        formats = [(name, profile.description) for name, profile in self.known_formats.items()]
        return formats

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        return self.known_formats.get(name)

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

        geometry = PhysicalFormat(
            encoding=encoding,
            rate=rate,
            rpm=rpm,
            cylinders=cylinders,
            heads=2,
            sectors_per_track=sectors_per_track,
            bytes_per_sector=bytes_per_sector
        )
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
        return filtered

    def _find_matching_format(self, filtered_formats: List[FormatProfile]) -> Optional[FormatProfile]:
        for profile in filtered_formats:
            try:
                self.set_format(profile)
                self.filesystem = create_filesystem(self.disk)
                if self.filesystem:
                    self._adjust_geometry_after_filesystem()
                    logger.debug(f"Adjusted geometry after filesystem: {self.disk.geometry}")
                    return profile
            except Exception:
                continue
        return None

    def _detect_physical_disk_format(self, drive_size: str = "3.5") -> bool:
        temp_geometry, temp_physical = self._get_default_geometry_and_physical(drive_size)
        temp_profile = FormatProfile(
            name="temp_detect",
            description="Temporary for detection",
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
                    updated_geometry = PhysicalFormat(
                        encoding=temp_geometry.encoding,
                        rate=temp_geometry.rate,
                        rpm=temp_geometry.rpm,
                        cylinders=temp_geometry.cylinders,
                        heads=heads,
                        sectors_per_track=temp_geometry.sectors_per_track,
                        bytes_per_sector=temp_geometry.bytes_per_sector
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
        fallback_geometry = PhysicalFormat(
            encoding=temp_geometry.encoding,
            rate=temp_geometry.rate,
            rpm=temp_geometry.rpm,
            cylinders=temp_geometry.cylinders,
            heads=default_heads,
            sectors_per_track=temp_geometry.sectors_per_track,
            bytes_per_sector=temp_geometry.bytes_per_sector
        )
        fallback_profile = FormatProfile(
            name="fallback_detected",
            description="Fallback based on detection",
            physical_format=fallback_geometry
        )
        self.set_format(fallback_profile)
        return True

    def _detect_image_file_format(self, file_path: str) -> bool:
        # Detect format and get BPB data
        format_name, boot_data = self.detect_format()
        self.logger.info(f"BPB: bytes_per_sector={boot_data.bytes_per_sector}, sectors_per_track={boot_data.sectors_per_track}, num_heads={boot_data.num_heads}, total_sectors={boot_data.total_sectors}")

        # Calculate cylinders based on file size and BPB values
        file_size = os.path.getsize(file_path)
        total_sectors = file_size // boot_data.bytes_per_sector
        cylinders = total_sectors // (boot_data.num_heads * boot_data.sectors_per_track)

        # Set geometry using BPB values
        physical_format = PhysicalFormat(
            encoding="MFM",  # Adjust as needed
            rate=500,        # Adjust as needed
            rpm=360,         # Adjust as needed
            cylinders=cylinders,
            heads=boot_data.num_heads,
            sectors_per_track=boot_data.sectors_per_track,
            bytes_per_sector=boot_data.bytes_per_sector,  # Use BPB value (256 in this case)
            gap3=84,         # Default or adjust as needed
            cskew=0,         # Default or adjust as needed
            interleave=1     # Default or adjust as needed
        )

        # Apply the geometry
        self.disk.set_geometry(physical_format)
        self.driver.set_physical_format(physical_format)
        self.logger.info(f"Geometry set: bytes_per_sector={physical_format.bytes_per_sector}, sectors_per_track={physical_format.sectors_per_track}, heads={physical_format.heads}, cylinders={physical_format.cylinders}")

        # Apply format-specific settings if detected
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile:
                self.set_format(profile)

        # Initialize filesystem and adjust geometry if needed
        self.filesystem = create_filesystem(self.disk)
        self._adjust_geometry_after_filesystem()
        logger.debug(f"Adjusted geometry after filesystem: {self.disk.geometry}")
        return True
