# src/fatfloppy/core/controller.py
import os
from typing import List, Optional, Tuple

from .utils.logging_config import get_logger
from .disk import Disk, DiskGeometry
from .drivers import DiskIODriver, GreaseweazleDriver, RawImageDriver, PhysicalFormat
from .formats import FormatManager, FormatProfile
from .filesystem import Filesystem, FATFilesystem

logger = get_logger()

class DiskController:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        self.format_manager = FormatManager()
        self.driver: Optional[DiskIODriver] = None
        self.explicit_format_set = False
        self.logger.debug("DiskController initialized")

    def open_disk(self, source: str, disk_type: str = "image", drive_letter: str = "A", drive_size: str = "3.5", format_info: dict = None) -> bool:
        if self.disk:
            self.close_disk()

        self.explicit_format_set = False

        try:
            # Create appropriate driver
            if disk_type == "physical":
                device_name = source if source else None
                self.logger.debug(f"Creating GreaseweazleDriver with device: {device_name}, drive: {drive_letter}, size: {drive_size}\"")
                self.driver = GreaseweazleDriver(device_name=device_name, drive=drive_letter, drive_size=drive_size)

                # Apply format parameters if provided
                if format_info:
                    self.explicit_format_set = True
                    self.logger.info(f"Using user-specified format parameters")
                    physical_format = PhysicalFormat(
                        encoding=format_info.get("encoding", "MFM"),
                        rate=format_info.get("rate", 500),
                        rpm=format_info.get("rpm", 300),
                        gap3=format_info.get("gap3", 84),
                        cskew=format_info.get("cskew", 0),
                        interleave=format_info.get("interleave", 1),
                        sectors_per_track=format_info.get("sectors_per_track", 18),
                        heads=format_info.get("heads", 2),
                        sector_size=format_info.get("sector_size", 512)
                    )
                    self.driver.set_physical_format(physical_format)

                    # Set geometry based on format parameters
                    geometry = DiskGeometry(
                        cylinders=format_info.get("cylinders", 80),
                        heads=format_info.get("heads", 2),
                        sectors_per_track=format_info.get("sectors_per_track", 18),
                        sector_size=format_info.get("sector_size", 512)
                    )

                    self.disk = Disk(self.driver)
                    self.disk.set_geometry(geometry)
                    self.logger.info(f"Set geometry from user-specified format")

                    # Create custom disk definition for Greaseweazle when format is explicitly specified
                    if hasattr(self.driver, '_create_and_set_custom_diskdef'):
                        self.logger.debug("Creating custom disk definition for Greaseweazle")
                        self.driver._create_and_set_custom_diskdef()

                    # Try to detect the filesystem with the specified format
                    if self.detect_filesystem():
                        self.logger.info("Filesystem detected with user-specified format")
                        return True

                    # If filesystem detection failed, we still return true
                    # since the user explicitly set the format
                    return True

                # Create disk object
                self.disk = Disk(self.driver)
                self.logger.debug("Disk object created")

                # Different detection strategies based on disk type
                if disk_type == "physical":
                    # If format info was provided, apply geometry directly
                    if format_info:
                        geometry = DiskGeometry(
                            cylinders=format_info.get("cylinders", 80),
                            heads=format_info.get("heads", 2),
                            sectors_per_track=format_info.get("sectors_per_track", 18),
                            sector_size=format_info.get("sector_size", 512)
                        )
                        self.disk.set_geometry(geometry)
                        self.logger.info(f"Set geometry from user-specified format")

                        # Create custom disk definition for Greaseweazle when format is explicitly specified
                        if hasattr(self.driver, '_create_and_set_custom_diskdef'):
                            self.driver._create_and_set_custom_diskdef()
                            self.logger.info("Created custom disk definition for Greaseweazle")

                        # Try to detect the filesystem with the specified format
                        if self.detect_filesystem():
                            self.logger.info("Filesystem detected with user-specified format")
                            return True

                        # If filesystem detection failed, we still return true
                        # since the user explicitly set the format
                        return True

                    # Otherwise use the regular detection method
                    result = self._detect_physical_disk_format(drive_size)
                    self.logger.info(f"Physical disk format detection {'succeeded' if result else 'failed'}")
                    return result
                else:
                    result = self._detect_image_file_format(source)
                    self.logger.info(f"Image file format detection {'succeeded' if result else 'failed'}")
                    return result

            elif disk_type == "image":
                if not os.path.exists(source):
                    self.logger.error(f"Image file not found: {source}")
                    return False
                self.logger.debug(f"Creating RawImageDriver for: {source}")
                self.driver = RawImageDriver(file_path=source)

                self.disk = Disk(self.driver)
                self.logger.debug("Disk object created")

                result = self._detect_image_file_format(source)
                self.logger.info(f"Image file format detection {'succeeded' if result else 'failed'}")
                return result
            else:
                self.logger.error(f"Unsupported disk type: {disk_type}")
                raise ValueError(f"Unsupported disk type: {disk_type}")

        except Exception as e:
            self.logger.exception(f"Error opening disk: {e}")
            self.close_disk()
            return False

    def close_disk(self) -> None:
        if self.disk and self.driver:
            try:
                self.logger.debug("Flushing driver before closing disk")
                self.driver.flush()
            except Exception as e:
                self.logger.error(f"Error flushing driver: {e}")

        self.logger.debug("Closing disk")
        self.disk = None
        self.filesystem = None
        self.driver = None

    def detect_geometry(self) -> Optional[DiskGeometry]:
        if not self.disk:
            self.logger.error("No disk opened to detect geometry")
            return None

        format_name = self.detect_format()
        if format_name:
            profile = self.format_manager.get_format_by_name(format_name)
            if profile:
                self.logger.debug(f"Detected geometry from format {format_name}")
                return profile.geometry

        self.logger.debug("Could not detect geometry from format")
        return None

    def set_geometry(self, geometry: DiskGeometry) -> None:
        if not self.disk:
            self.logger.error("No disk opened to set geometry")
            raise ValueError("No disk opened")

        self.logger.debug(f"Setting geometry to {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}, {geometry.sector_size} bytes/sector")
        self.disk.set_geometry(geometry)

    def detect_format(self) -> Optional[str]:
        if not self.disk:
            self.logger.error("No disk opened to detect format")
            return None

        format_name = self.format_manager.detect_format(self.disk)
        if format_name:
            self.logger.debug(f"Detected format: {format_name}")
        else:
            self.logger.debug("No format detected")
        return format_name

    def set_format(self, profile: FormatProfile) -> None:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to set format")
            raise ValueError("No disk opened")

        self.logger.debug(f"Setting format: {profile.name} ({profile.description})")

        # Set disk geometry
        self.disk.set_geometry(profile.geometry)

        # Set physical format in driver
        self.driver.set_physical_format(profile.physical_format)

    def detect_filesystem(self) -> Optional[str]:
        if not self.disk:
            self.logger.error("No disk opened to detect filesystem")
            return None

        # Try to mount a FAT filesystem
        try:
            # Validate geometry is set
            if not self.disk.geometry:
                self.logger.warning("Cannot detect filesystem: disk geometry not set")
                return None

            # Try to read the boot sector
            try:
                self.logger.debug("Reading boot sector to detect filesystem")
                boot_sector = self.disk.read_sector(0, 0, 1)
                if not boot_sector or all(b == 0 for b in boot_sector):
                    self.logger.warning("Boot sector is empty - disk may be unformatted")
                    return None
            except Exception as e:
                self.logger.error(f"Error reading boot sector: {e}")
                return None

            # Try to mount filesystem
            self.logger.debug("Attempting to mount FAT filesystem")
            self.filesystem = FATFilesystem(self.disk)
            if self.filesystem.is_valid():
                # Always check if the driver has a physically detected sector count
                if hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                    actual_sectors = self.driver.physical_format.sectors_per_track
                    # If the driver detected a different sector count than geometry, use the detected count
                    if actual_sectors != self.disk.geometry.sectors_per_track:
                        self.logger.info(f"Updating geometry with physically detected sector count: {actual_sectors}")
                        updated_geometry = DiskGeometry(
                            cylinders=self.disk.geometry.cylinders,
                            heads=self.disk.geometry.heads,
                            sectors_per_track=actual_sectors,
                            sector_size=self.disk.geometry.sector_size
                        )
                        self.set_geometry(updated_geometry)

                # Only update from BPB if an explicit format wasn't provided during open_disk
                elif not self.explicit_format_set and hasattr(self.filesystem, 'boot_sector'):
                    bs = self.filesystem.boot_sector

                    # Only update geometry from BPB if the detected values are valid and reasonable
                    if (hasattr(bs, 'sectors_per_track') and bs.sectors_per_track > 0 and
                        hasattr(bs, 'num_heads') and bs.num_heads > 0):

                        # Update geometry from the BPB
                        sectors_per_track = bs.sectors_per_track
                        heads = bs.num_heads

                        # If these differ from our current geometry, update it
                        if (self.disk.geometry.sectors_per_track != sectors_per_track or
                            self.disk.geometry.heads != heads):
                            self.logger.info(f"Updating geometry from BPB: {sectors_per_track} sectors, {heads} heads")

                            updated_geometry = DiskGeometry(
                                cylinders=self.disk.geometry.cylinders,
                                heads=heads,
                                sectors_per_track=sectors_per_track,
                                sector_size=self.disk.geometry.sector_size
                            )
                            self.set_geometry(updated_geometry)

                self.logger.info("Valid FAT12 filesystem detected")
                return "FAT12"
        except Exception as e:
            self.logger.exception(f"Error detecting filesystem: {e}")
            self.filesystem = None

        self.logger.warning("No valid filesystem detected")
        return None

    def get_allocated_clusters(self) -> List[int]:
        """Returns list of allocated cluster numbers if available"""
        if not self.filesystem or not hasattr(self.filesystem, "get_allocated_clusters"):
            self.logger.debug("Cannot get allocated clusters: no valid filesystem")
            return []

        try:
            clusters = self.filesystem.get_allocated_clusters()
            self.logger.debug(f"Found {len(clusters)} allocated clusters")
            return clusters
        except Exception as e:
            self.logger.error(f"Error getting allocated clusters: {e}")
            return []

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        """Returns (free_bytes, total_bytes) if available"""
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            self.logger.debug("Cannot get free space: no valid filesystem")
            return None

        try:
            space_info = self.filesystem.get_free_space()
            free_kb = space_info[0] // 1024
            total_kb = space_info[1] // 1024
            self.logger.debug(f"Free space: {free_kb}KB / {total_kb}KB")
            return space_info
        except Exception as e:
            self.logger.error(f"Error getting free space: {e}")
            return None

    def list_directory(self, path: str = "/") -> List[dict]:
        if not self.filesystem:
            self.logger.warning(f"Cannot list directory {path}: no valid filesystem")
            return []

        try:
            self.logger.debug(f"Listing directory: {path}")
            items = self.filesystem.list_directory(path)
            self.logger.debug(f"Found {len(items)} items in directory {path}")
            return [
                {
                    "name": item.name,
                    "size": item.size,
                    "is_dir": item.is_dir,
                    "datetime": item.datetime,
                    "attributes": item.attributes
                }
                for item in items
            ]
        except Exception as e:
            self.logger.error(f"Error listing directory {path}: {e}")
            return []

    def read_file(self, path: str) -> Optional[bytes]:
        if not self.filesystem:
            self.logger.warning(f"Cannot read file {path}: no valid filesystem")
            return None

        try:
            self.logger.debug(f"Reading file: {path}")
            data = self.filesystem.read_file(path)
            self.logger.debug(f"Read {len(data)} bytes from {path}")
            return data
        except ValueError as e:
            # More specific handling for expected errors like "File not found"
            # This provides clearer logging without returning None for known issues
            if "File not found" in str(e):
                self.logger.warning(f"File not found: {path}")
            else:
                self.logger.warning(f"Value error reading file {path}: {e}")
            return None
        except Exception as e:
            # General error handling for unexpected errors
            self.logger.error(f"Error reading file {path}: {e}")
            return None

    def write_file(self, path: str, data: bytes) -> bool:
        if not self.filesystem:
            self.logger.warning(f"Cannot write file {path}: no valid filesystem")
            return False

        try:
            self.logger.debug(f"Writing {len(data)} bytes to file: {path}")
            self.filesystem.write_file(path, data)
            self.logger.info(f"Successfully wrote file: {path}")
            return True
        except Exception as e:
            self.logger.error(f"Error writing file {path}: {e}")
            return False

    def create_directory(self, path: str) -> bool:
        if not self.filesystem:
            self.logger.warning(f"Cannot create directory {path}: no valid filesystem")
            return False

        try:
            self.logger.debug(f"Creating directory: {path}")
            self.filesystem.create_directory(path)
            self.logger.info(f"Successfully created directory: {path}")
            return True
        except Exception as e:
            self.logger.error(f"Error creating directory {path}: {e}")
            return False

    def delete_item(self, path: str) -> bool:
        if not self.filesystem:
            self.logger.warning(f"Cannot delete item {path}: no valid filesystem")
            return False

        try:
            self.logger.debug(f"Deleting item: {path}")
            self.filesystem.delete(path)
            self.logger.info(f"Successfully deleted item: {path}")
            return True
        except Exception as e:
            self.logger.error(f"Error deleting item {path}: {e}")
            return False

    def list_formats(self) -> List[Tuple[str, str]]:
        formats = self.format_manager.list_known_formats()
        self.logger.debug(f"Listed {len(formats)} available formats")
        return formats

    def _detect_physical_disk_format(self, drive_size: str = "3.5") -> bool:
        """Detect format for physical floppy disks, considering drive size"""
        self.logger.debug(f"Detecting physical disk format for {drive_size}\" drive")

        # Default cylinder count based on drive size
        default_cylinders = 80
        if drive_size == "5.25":
            default_cylinders = 40
        elif drive_size == "8":
            default_cylinders = 77

        # Initialize with temporary geometry to read the boot sector
        temp_geometry = DiskGeometry(
            cylinders=default_cylinders,
            heads=2,  # Start with double-sided to read boot sector
            sectors_per_track=18 if drive_size == "3.5" else 9,
            sector_size=512
        )
        self.set_geometry(temp_geometry)

        # Set temporary physical format based on common parameters for the drive size
        self.driver.set_physical_format(PhysicalFormat(
            encoding="MFM",
            rate=500 if drive_size == "3.5" else 250,
            rpm=300 if drive_size != "8" else 360,
            gap3=84 if drive_size != "8" else 26,
            sectors_per_track=temp_geometry.sectors_per_track,
            heads=temp_geometry.heads,
            sector_size=temp_geometry.sector_size
        ))

        # Ensure driver is initialized
        if hasattr(self.driver, 'initialize'):
            self.driver.initialize()

        # Try to detect filesystem to get head count from BPB
        has_second_head = True  # Default to true
        fs_type = None  # Initialize fs_type to prevent UnboundLocalError
        try:
            fs_type = self.detect_filesystem()
            if fs_type and hasattr(self.filesystem, 'boot_sector'):
                bs = self.filesystem.boot_sector
                if hasattr(bs, 'num_heads') and bs.num_heads > 0:
                    self.logger.info(f"BPB reports {bs.num_heads} heads")
                    has_second_head = bs.num_heads > 1
                    if not has_second_head:
                        self.logger.info("Filesystem indicates single-sided disk")
                    return True  # If we have a filesystem, we can return success
        except Exception as e:
            self.logger.warning(f"Error detecting filesystem: {e}")
            # Continue with format detection

        # Only if we couldn't determine from filesystem, physically test head 1
        if not fs_type:
            self.logger.debug("No filesystem detected, testing if disk has a second head (head 1)")
            try:
                # Use the driver's read capability to test head 1
                if hasattr(self.driver, '_read_track'):
                    self.logger.debug("Testing head 1 at cylinder 0")
                    success = self.driver._read_track(0, 1)
                    if success:
                        self.logger.info("Successfully read from head 1 - disk is double-sided")
                        has_second_head = True
                    else:
                        self.logger.info("Could not read from head 1 - disk appears to be single-sided")
                        has_second_head = False
                else:
                    # Fallback to direct sector read
                    try:
                        self.logger.debug("Trying direct sector read from head 1")
                        self.disk.read_sector(0, 1, 1)
                        self.logger.info("Successfully read from head 1 - disk is double-sided")
                        has_second_head = True
                    except Exception:
                        self.logger.info("Could not read from head 1 - disk appears to be single-sided")
                        has_second_head = False
            except Exception as e:
                self.logger.warning(f"Error testing for second head: {e}")
                # If we can't test, assume double-sided as safer default
                self.logger.info("Assuming double-sided disk due to test failure")
                has_second_head = True

        # Get formats from format_definitions.py
        filtered_formats = []
        try:
            from .format_definitions import FLOPPY_FORMATS
            for name, profile in FLOPPY_FORMATS.items():
                # Filter formats based on drive size
                if ((drive_size == "3.5" and "3.5\"" in profile.description) or
                    (drive_size == "5.25" and "5.25\"" in profile.description) or
                    (drive_size == "8" and "8\"" in profile.description)):

                    # Filter formats based on detected head count
                    if not has_second_head and profile.geometry.heads > 1:
                        self.logger.debug(f"Skipping format {name} because it requires 2 heads but disk appears to be single-sided")
                        continue

                    # Extract all parameters directly from the profile
                    geometry = DiskGeometry(
                        cylinders=profile.geometry.cylinders,
                        heads=profile.geometry.heads,
                        sectors_per_track=profile.geometry.sectors_per_track,
                        sector_size=profile.geometry.sector_size
                    )

                    # Preserve all physical format parameters
                    fmt_params = {
                        "rate": profile.physical_format.rate,
                        "encoding": profile.physical_format.encoding,
                        "rpm": profile.physical_format.rpm,
                        "gap3": profile.physical_format.gap3,
                        "cskew": profile.physical_format.cskew,
                        "interleave": profile.physical_format.interleave
                    }

                    filtered_formats.append((geometry, fmt_params, name))
                    self.logger.debug(f"Added format {name}: {profile.description} - "
                                    f"{geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}, "
                                    f"{fmt_params['encoding']}, {fmt_params['rate']}kbps")
        except Exception as e:
            self.logger.error(f"Error loading formats from format_definitions: {e}")
            return False

        # Try each geometry until we find a valid filesystem
        for geometry, fmt_params, format_name in filtered_formats:
            self.set_geometry(geometry)
            self.logger.debug(f"Trying geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}, "
                            f"{fmt_params['encoding']} at {fmt_params['rate']}kbps, {fmt_params['rpm']}rpm, gap3={fmt_params['gap3']}")

            # Set the physical format - make sure to use all parameters from the format
            self.driver.set_physical_format(PhysicalFormat(
                encoding=fmt_params['encoding'],
                rate=fmt_params['rate'],
                rpm=fmt_params['rpm'],
                gap3=fmt_params['gap3'],
                cskew=fmt_params['cskew'],
                interleave=fmt_params['interleave'],
                sectors_per_track=geometry.sectors_per_track,
                heads=geometry.heads,
                sector_size=geometry.sector_size
            ))

            # Try to detect filesystem with this geometry
            try:
                fs_type = self.detect_filesystem()
                if fs_type:
                    self.logger.info(f"Found valid filesystem {fs_type} with geometry {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")

                    # Calculate correct cylinder count based on BPB if available
                    if hasattr(self.filesystem, 'boot_sector') and self.filesystem.boot_sector:
                        bs = self.filesystem.boot_sector
                        if bs.total_sectors > 0 and bs.sectors_per_track > 0 and bs.num_heads > 0:
                            # Calculate cylinders from total sectors
                            calculated_cylinders = bs.total_sectors // (bs.sectors_per_track * bs.num_heads)

                            if calculated_cylinders > 0 and calculated_cylinders != geometry.cylinders:
                                self.logger.info(f"Updating cylinder count from {geometry.cylinders} to {calculated_cylinders} based on BPB data")

                                # Update geometry with calculated cylinder count
                                updated_geometry = DiskGeometry(
                                    cylinders=calculated_cylinders,
                                    heads=bs.num_heads,
                                    sectors_per_track=bs.sectors_per_track,
                                    sector_size=geometry.sector_size
                                )
                                self.set_geometry(updated_geometry)

                    return True
            except Exception as e:
                self.logger.debug(f"Failed with geometry {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}: {e}")
                continue

        # If we get here, just set a default geometry for displaying something
        default_heads = 1 if not has_second_head else 2
        default_geometry = DiskGeometry(
            cylinders=default_cylinders,
            heads=default_heads,
            sectors_per_track=18 if drive_size == "3.5" else (9 if drive_size == "5.25" else 26),
            sector_size=512 if drive_size != "8" else 128
        )
        self.set_geometry(default_geometry)

        # Set default physical format parameters based on drive size
        default_encoding = "MFM" if drive_size != "8" else "FM"
        default_rate = 500 if drive_size == "3.5" else 250
        default_rpm = 300 if drive_size != "8" else 360
        default_gap3 = 84 if drive_size != "8" else 26

        self.driver.set_physical_format(PhysicalFormat(
            encoding=default_encoding,
            rate=default_rate,
            rpm=default_rpm,
            gap3=default_gap3,
            sectors_per_track=default_geometry.sectors_per_track,
            heads=default_heads,
            sector_size=default_geometry.sector_size
        ))
        self.logger.warning("No filesystem detected, using default geometry for display")
        return True

    def _detect_image_file_format(self, file_path: str) -> bool:
        """Detect format for disk image files"""
        self.logger.debug(f"Detecting format for image file: {file_path}")
        # First try to detect format
        format_name = self.detect_format()
        if format_name:
            profile = self.format_manager.get_format_by_name(format_name)
            if profile:
                self.set_format(profile)
                if self.detect_filesystem():
                    self.logger.info(f"Detected format {format_name} with valid filesystem")
                    return True

        # Try default geometries for image files
        default_geometries = [
            DiskGeometry(80, 2, 18, 512),  # 1.44MB
            DiskGeometry(80, 2, 9, 512),   # 720KB
            DiskGeometry(40, 2, 9, 512)    # 360KB
        ]

        for geometry in default_geometries:
            self.logger.debug(f"Trying default geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
            self.set_geometry(geometry)
            if self.detect_filesystem():
                self.logger.info(f"Found valid filesystem with geometry {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                return True

        # No format detected, but we can still work with the image
        # Just set a default geometry
        if not self.disk.geometry:
            self.set_geometry(default_geometries[0])
            self.logger.warning("No filesystem detected, using default 1.44MB geometry")

        return True
