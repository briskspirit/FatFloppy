# src/fatfloppy/core/controller.py
import os
from typing import List, Optional, Tuple

from .utils.logging_config import get_logger
from .disk import Disk, DiskGeometry
from .drivers import DiskIODriver, GreaseweazleDriver, RawImageDriver
from .physical_format import PhysicalFormat
# Removed FormatManager import
from .formats import BootSectorData, FormatProfile # Keep these
from .filesystem import Filesystem, FATFilesystem
# Import formats directly
from .format_definitions import FLOPPY_FORMATS

logger = get_logger()

class DiskController:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        # Load formats directly here
        self.known_formats = FLOPPY_FORMATS
        self.driver: Optional[DiskIODriver] = None
        self.explicit_format_set = False
        self.logger.debug(f"DiskController initialized with {len(self.known_formats)} known formats")

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

                # --- MOVED INITIALIZATION HERE ---
                # Initialize the driver immediately after creation for physical disks
                try:
                    self.logger.debug("Initializing Greaseweazle driver immediately after creation.")
                    self.driver.initialize() # This will call util.Drive() and set initialized=True
                except Exception as e:
                    self.logger.error(f"Error during driver initialization: {e}")
                    self.close_disk()
                    return False
                # --- END MOVED INITIALIZATION ---

                # Apply format parameters if provided
                if format_info:
                    self.explicit_format_set = True
                    self.logger.info(f"Using user-specified format parameters")
                    # Check if format_name is provided to load a base profile
                    format_name = format_info.get("format_name")
                    base_profile = self.get_format_by_name(format_name) if format_name else None

                    # Use base profile values if available, otherwise use defaults/provided
                    default_phys = PhysicalFormat(
                        encoding="MFM", rate=500, rpm=300, gap3=84, cskew=0, interleave=1,
                        sectors_per_track=18, heads=2, sector_size=512
                    )
                    if base_profile and base_profile.physical_format: # Check if base profile has physical format
                        default_phys = base_profile.physical_format

                    physical_format = PhysicalFormat(
                        encoding=format_info.get("encoding", default_phys.encoding),
                        rate=format_info.get("rate", default_phys.rate),
                        rpm=format_info.get("rpm", default_phys.rpm),
                        gap3=format_info.get("gap3", default_phys.gap3),
                        cskew=format_info.get("cskew", default_phys.cskew),
                        interleave=format_info.get("interleave", default_phys.interleave),
                        sectors_per_track=format_info.get("sectors_per_track", default_phys.sectors_per_track),
                        heads=format_info.get("heads", default_phys.heads),
                        sector_size=format_info.get("sector_size", default_phys.sector_size)
                    )
                    self.driver.set_physical_format(physical_format) # Set format *after* initialization

                    # Set geometry based on format parameters or base profile
                    default_geom = DiskGeometry(
                        cylinders=80, heads=2, sectors_per_track=18, sector_size=512
                    )
                    if base_profile:
                         default_geom = base_profile.geometry

                    geometry = DiskGeometry(
                        cylinders=format_info.get("cylinders", default_geom.cylinders),
                        heads=format_info.get("heads", physical_format.heads), # Use heads from physical format
                        sectors_per_track=format_info.get("sectors_per_track", physical_format.sectors_per_track), # Use spt from physical format
                        sector_size=format_info.get("sector_size", physical_format.sector_size) # Use sector size from physical format
                    )

                    # Create disk object *after* driver is initialized and format potentially set
                    self.disk = Disk(self.driver)
                    self.disk.set_geometry(geometry) # Set geometry on the Disk object
                    self.logger.info(f"Set geometry from user-specified format: {geometry}")

                    # Create custom disk definition for Greaseweazle when format is explicitly specified
                    if hasattr(self.driver, '_create_and_set_custom_diskdef'):
                        self.logger.debug("Creating custom disk definition for Greaseweazle")
                        # Driver already has physical format, just need cylinders
                        self.driver._create_and_set_custom_diskdef(geometry.cylinders) # Pass cylinders

                    # Try to detect the filesystem with the specified format
                    if self.detect_filesystem():
                        self.logger.info("Filesystem detected with user-specified format")
                    else:
                        self.logger.warning("Could not detect filesystem with user-specified format, but proceeding as format was explicit.")

                    return True # Return true since user specified format explicitly

                # --- ELSE: format_info was NOT provided ---
                # Create disk object *after* driver is initialized
                self.disk = Disk(self.driver)
                self.logger.debug("Disk object created for auto-detection")

                # Use the regular detection method for physical disks
                # Driver is already initialized from above
                result = self._detect_physical_disk_format(drive_size)
                self.logger.info(f"Physical disk format detection {'succeeded' if result else 'failed'}")
                return result

            elif disk_type == "image":
                if not os.path.exists(source):
                    self.logger.error(f"Image file not found: {source}")
                    return False
                self.logger.debug(f"Creating RawImageDriver for: {source}")
                self.driver = RawImageDriver(file_path=source)

                # No driver initialization needed for RawImageDriver
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

        format_name = self.detect_format() # Uses integrated method
        if format_name:
            profile = self.get_format_by_name(format_name) # Uses integrated method
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
        """Detects disk format by reading and analysing the boot sector."""
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format")
            return None

        self.logger.debug("Attempting to detect disk format from boot sector")
        try:
            # Try direct read if driver supports it (faster for images)
            if hasattr(self.driver, 'read_bytes_direct'):
                self.logger.debug("Using driver's direct read for boot sector")
                boot_sector_bytes = self.driver.read_bytes_direct(0, 512)
            else:
                # Fall back to regular sector read (C=0, H=0, S=1)
                # Use a temporary minimal geometry if none is set yet
                temp_geom_set = False
                if not self.disk.geometry:
                     self.logger.debug("Temporarily setting minimal geometry for boot sector read")
                     # Use common defaults (512 sector size essential)
                     temp_geom = DiskGeometry(80, 2, 9, 512)
                     self.disk.set_geometry(temp_geom)
                     # Also set basic physical format if driver needs it
                     if hasattr(self.driver, "set_physical_format") and not getattr(self.driver, "physical_format", None):
                          self.logger.debug("Setting temporary physical format for boot sector read")
                          temp_phys = PhysicalFormat(encoding="MFM", rate=250, rpm=300, sectors_per_track=9, heads=2, sector_size=512)
                          self.driver.set_physical_format(temp_phys)
                     temp_geom_set = True

                self.logger.debug("Using disk's sector read for boot sector (0, 0, 1)")
                boot_sector_bytes = self.disk.read_sector(0, 0, 1)

                # Clean up temporary geometry if we set one
                # if temp_geom_set:
                #      self.logger.debug("Clearing temporary geometry")
                #      self.disk.geometry = None # Let subsequent steps set the real geometry

            if not boot_sector_bytes or len(boot_sector_bytes) < 512:
                 self.logger.warning(f"Failed to read valid boot sector (read {len(boot_sector_bytes or b'')} bytes). Cannot detect format.")
                 return None

            # Parse boot sector data
            try:
                boot_data = BootSectorData.from_bytes(boot_sector_bytes)
                self.logger.debug(f"Parsed boot sector: Heads={boot_data.num_heads}, SPT={boot_data.sectors_per_track}, Total={boot_data.total_sectors}, Media={boot_data.media_descriptor:02X}")
            except ValueError as e:
                self.logger.warning(f"Could not parse boot sector data: {e}")
                return None # Cannot detect format if boot sector is invalid

            # Compare parsed data against known format profiles
            for format_name, profile in self.known_formats.items():
                # Primarily match on BPB geometry fields and media descriptor
                if (profile.boot_sector and # Ensure profile has boot sector info
                    profile.boot_sector.sectors_per_track == boot_data.sectors_per_track and
                    profile.boot_sector.num_heads == boot_data.num_heads and
                    profile.boot_sector.total_sectors == boot_data.total_sectors and
                    profile.boot_sector.media_descriptor == boot_data.media_descriptor):
                    self.logger.info(f"Detected format based on boot sector: {format_name}")
                    return format_name
                # Secondary check: Match geometry directly if boot sector doesn't match
                elif (profile.geometry.sectors_per_track == boot_data.sectors_per_track and
                      profile.geometry.heads == boot_data.num_heads and
                      profile.geometry.total_sectors == boot_data.total_sectors):
                      self.logger.info(f"Detected format {format_name} based on geometry match in boot sector")
                      return format_name


            self.logger.debug("No matching format found based on boot sector data")
            return None

        except Exception as e:
            self.logger.error(f"Error during format detection: {e}", exc_info=True)
            return None

    def set_format(self, profile: FormatProfile) -> None:
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to set format")
            raise ValueError("No disk opened")

        self.logger.debug(f"Setting format: {profile.name} ({profile.description})")

        # Set disk geometry
        self.disk.set_geometry(profile.geometry)

        # Set physical format in driver
        # --- Create a DEEP COPY to avoid modifying the profile ---
        import copy
        physical_format_copy = copy.deepcopy(profile.physical_format)
        # --- END CHANGE ---
        self.driver.set_physical_format(physical_format_copy)

        # If it's a physical drive, potentially create the greaseweazle diskdef
        if isinstance(self.driver, GreaseweazleDriver):
             if hasattr(self.driver, '_create_and_set_custom_diskdef'):
                  self.logger.debug("Creating disk definition for Greaseweazle based on set format")
                  self.driver._create_and_set_custom_diskdef(profile.geometry.cylinders) # Pass cylinders

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
        formats = [(name, profile.description) for name, profile in self.known_formats.items()]
        self.logger.debug(f"Listed {len(formats)} available formats")
        return formats

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        """Retrieves a format profile by its name."""
        profile = self.known_formats.get(name)
        if profile:
            self.logger.debug(f"Found format profile for name: {name}")
        else:
            self.logger.debug(f"No format profile found for name: {name}")
        return profile

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
            sectors_per_track=18 if drive_size == "3.5" else (9 if drive_size == "5.25" else 26),
            sector_size=512 if drive_size != "8" else 128
        )
        # Use a common physical format temporarily
        temp_rate = 500 if drive_size == "3.5" else (250 if drive_size == "5.25" else 250)
        temp_encoding = "MFM" if drive_size != "8" else "FM"
        temp_rpm = 300 if drive_size != "8" else 360
        temp_physical = PhysicalFormat(
            encoding=temp_encoding, rate=temp_rate, rpm=temp_rpm,
            sectors_per_track=temp_geometry.sectors_per_track,
            heads=temp_geometry.heads, sector_size=temp_geometry.sector_size
        )
        # Create a dummy FormatProfile to use set_format
        temp_profile = FormatProfile(
             name="temp_detect", description="Temporary for detection",
             geometry=temp_geometry, physical_format=temp_physical
        )
        self.set_format(temp_profile) # This sets geometry and physical format in disk/driver

        # Ensure driver is initialized (might read RPM here)
        if hasattr(self.driver, 'initialize'):
            self.driver.initialize()

        # Try to detect filesystem to get head count from BPB
        has_second_head = True  # Default to true
        fs_type = None
        # Set geometry before detecting filesystem
        self.set_geometry(temp_geometry)
        try:
            fs_type = self.detect_filesystem() # This will now use the temp geometry/format
            if fs_type and self.filesystem and hasattr(self.filesystem, 'boot_sector'):
                bs = self.filesystem.boot_sector
                if hasattr(bs, 'num_heads') and bs.num_heads > 0:
                    self.logger.info(f"BPB reports {bs.num_heads} heads")
                    has_second_head = bs.num_heads > 1
                    if not has_second_head:
                        self.logger.info("Filesystem indicates single-sided disk based on BPB")
                    # If filesystem found, we might have updated geometry, return success
                    return True
        except Exception as e:
            self.logger.warning(f"Error detecting filesystem during initial check: {e}")
            # Continue with physical format detection

        # Only if we couldn't determine from filesystem, physically test head 1
        if fs_type is None: # Changed check to use fs_type directly
            self.logger.debug("No filesystem detected, physically testing if disk has a second head (head 1)")
            try:
                # Use the driver's read capability to test head 1
                if hasattr(self.driver, '_read_track') and isinstance(self.driver, GreaseweazleDriver):
                    self.logger.debug("Testing head 1 at cylinder 0 using Greaseweazle _read_track")
                    # Ensure temporary format is set before reading
                    self.set_format(temp_profile)
                    # Read track 0, head 1
                    success = self.driver._read_track(0, 1) # _read_track now returns bool
                    if success:
                        # Further check if sectors were actually found
                        track_data = self.driver.track_data.get((0, 1), {})
                        if track_data:
                             self.logger.info(f"Successfully read {len(track_data)} sector(s) from head 1 - disk is double-sided")
                             has_second_head = True
                        else:
                             self.logger.info("Read from head 1 succeeded but found no sectors - assuming single-sided")
                             has_second_head = False
                    else:
                        self.logger.info("Could not read from head 1 - disk appears to be single-sided")
                        has_second_head = False
                else:
                    # Fallback to direct sector read using Disk object
                    try:
                        self.logger.debug("Trying direct sector read from head 1 (C=0, H=1, S=1)")
                        # Ensure geometry allows reading head 1
                        if temp_geometry.heads < 2:
                             temp_geometry.heads = 2
                             self.set_geometry(temp_geometry)
                        self.disk.read_sector(0, 1, 1)
                        self.logger.info("Successfully read sector from head 1 - disk is double-sided")
                        has_second_head = True
                    except Exception:
                        self.logger.info("Could not read sector from head 1 - disk appears to be single-sided")
                        has_second_head = False
            except Exception as e:
                self.logger.warning(f"Error testing for second head: {e}")
                # If we can't test, assume double-sided as safer default
                self.logger.info("Assuming double-sided disk due to test failure")
                has_second_head = True

        # Filter known formats based on drive size and detected head count
        filtered_formats = []
        try:
            # Use self.known_formats loaded in __init__
            for name, profile in self.known_formats.items():
                # Filter by drive size compatibility
                size_match = False
                if drive_size == "3.5" and "3.5\"" in profile.description: size_match = True
                elif drive_size == "5.25" and "5.25\"" in profile.description: size_match = True
                elif drive_size == "8" and "8\"" in profile.description: size_match = True

                if not size_match:
                     # self.logger.debug(f"Skipping format {name} due to drive size mismatch ('{drive_size}' vs '{profile.description}')")
                     continue

                # Filter by detected head count
                if not has_second_head and profile.geometry.heads > 1:
                    self.logger.debug(f"Skipping format {name} because it requires 2 heads but disk appears single-sided")
                    continue
                # Conversely, if we detected 2 heads, skip explicitly single-headed formats?
                # Usually safer to try DS formats even if FS check failed on head 1 initially. Let's keep them.
                # if has_second_head and profile.geometry.heads == 1:
                #     self.logger.debug(f"Skipping format {name} because it requires 1 head but disk appears double-sided")
                #     continue

                # Add matching profile
                filtered_formats.append(profile)
                self.logger.debug(f"Candidate format: {name} ({profile.geometry.cylinders}x{profile.geometry.heads}x{profile.geometry.sectors_per_track}, {profile.physical_format.encoding} @ {profile.physical_format.rate}kbps)")

        except Exception as e:
            self.logger.error(f"Error filtering formats: {e}")
            return False

        # Try each filtered format profile until we find a valid filesystem
        for profile in filtered_formats:
            self.logger.debug(f"Trying format profile: {profile.name}")
            try:
                # Use set_format to apply geometry and physical format
                self.set_format(profile)

                # Try to detect filesystem with this format
                fs_type = self.detect_filesystem()
                if fs_type:
                    self.logger.info(f"Success! Found valid filesystem '{fs_type}' with format '{profile.name}'")
                    # Geometry might have been updated by detect_filesystem, no need to recalculate here
                    return True

            except Exception as e:
                self.logger.debug(f"Format '{profile.name}' failed: {e}", exc_info=False) # exc_info=False to avoid spamming logs
                continue # Try next format

        # If we get here, no format resulted in a detectable filesystem
        self.logger.warning("No standard format resulted in a detectable filesystem.")

        # Fallback: Use the initial temporary geometry/format, adjusted for detected heads
        default_heads = 1 if not has_second_head else 2
        final_geometry = DiskGeometry(
            cylinders=default_cylinders,
            heads=default_heads,
            sectors_per_track=temp_profile.geometry.sectors_per_track, # Use spt from initial guess
            sector_size=temp_profile.geometry.sector_size      # Use sector size from initial guess
        )
        final_physical = PhysicalFormat(
            encoding=temp_profile.physical_format.encoding,
            rate=temp_profile.physical_format.rate,
            rpm=temp_profile.physical_format.rpm,
            gap3=temp_profile.physical_format.gap3,
            cskew=temp_profile.physical_format.cskew,
            interleave=temp_profile.physical_format.interleave,
            sectors_per_track=final_geometry.sectors_per_track,
            heads=final_geometry.heads,
            sector_size=final_geometry.sector_size
        )
        final_profile = FormatProfile(
             name="fallback_detected", description="Fallback based on detection",
             geometry=final_geometry, physical_format=final_physical
        )
        self.logger.info(f"Setting fallback format: {final_geometry}, {final_physical.encoding} @ {final_physical.rate}kbps")
        self.set_format(final_profile)

        # Even if no FS detected, opening the disk succeeded at a low level
        return True

    def _detect_image_file_format(self, file_path: str) -> bool:
        """Detect format for disk image files"""
        self.logger.debug(f"Detecting format for image file: {file_path}")

        # First try to detect format using boot sector analysis
        format_name = self.detect_format() # Uses integrated method
        if format_name:
            profile = self.get_format_by_name(format_name) # Uses integrated method
            if profile:
                self.logger.info(f"Detected format '{format_name}' based on boot sector.")
                self.set_format(profile)
                # Check if filesystem is valid with this format
                if self.detect_filesystem():
                    self.logger.info(f"Filesystem is valid for detected format '{format_name}'.")
                    return True
                else:
                     self.logger.warning(f"Detected format '{format_name}' but filesystem check failed. Proceeding with format.")
                     return True # Return true as format was detected, even if FS fails

        # If boot sector detection failed, try matching file size to known formats
        self.logger.debug("Boot sector detection failed, trying file size matching.")
        try:
            file_size = os.path.getsize(file_path)
            self.logger.debug(f"Image file size: {file_size} bytes")
            matched_profiles = []
            for name, profile in self.known_formats.items():
                 # Allow a small tolerance? Maybe not needed for standard formats.
                 if profile.geometry.total_bytes == file_size:
                      matched_profiles.append(profile)
                      self.logger.debug(f"File size matches format: {name}")

            # Try matched profiles
            for profile in matched_profiles:
                 self.logger.debug(f"Trying size-matched format: {profile.name}")
                 self.set_format(profile)
                 if self.detect_filesystem():
                      self.logger.info(f"Success! Found valid filesystem using size-matched format '{profile.name}'.")
                      return True

            # If size matching failed, try common default geometries
            if not matched_profiles:
                 self.logger.debug("File size did not match standard formats, trying common defaults.")
                 default_geometries = [
                     DiskGeometry(80, 2, 18, 512),  # 1.44MB (most common)
                     DiskGeometry(80, 2, 9, 512),   # 720KB
                     DiskGeometry(40, 2, 9, 512),   # 360KB (5.25")
                     DiskGeometry(80, 2, 21, 512),  # 1.68MB DMF
                     DiskGeometry(80, 2, 36, 512),  # 2.88MB ED
                     DiskGeometry(40, 1, 9, 512),   # 180KB (5.25")
                 ]

                 for geometry in default_geometries:
                     # Create a temporary profile for set_format
                     # Use common physical params (rate doesn't matter for image)
                     temp_physical = PhysicalFormat(
                          encoding="MFM", rate=500, rpm=300,
                          sectors_per_track=geometry.sectors_per_track,
                          heads=geometry.heads, sector_size=geometry.sector_size
                     )
                     temp_profile = FormatProfile(
                          name="temp_default", description="Temporary default",
                          geometry=geometry, physical_format=temp_physical
                     )
                     self.logger.debug(f"Trying default geometry: {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                     self.set_format(temp_profile)
                     # Check if image size is at least sufficient for this geometry
                     if file_size >= geometry.total_bytes:
                          if self.detect_filesystem():
                               self.logger.info(f"Found valid filesystem with default geometry {geometry.cylinders}x{geometry.heads}x{geometry.sectors_per_track}")
                               return True
                     else:
                          self.logger.debug(f"Skipping default geometry {geometry} as image size {file_size} is too small.")


            # If still no format detected, set a basic default (1.44MB) for basic access
            if not self.disk.geometry:
                 self.logger.warning("No format or filesystem detected. Setting default 1.44MB geometry for basic access.")
                 default_profile = self.get_format_by_name("ibm_3.5_1.44m")
                 if default_profile:
                      self.set_format(default_profile)
                 else: # Should not happen if FLOPPY_FORMATS is populated
                      self.logger.error("Default format 'ibm_3.5_1.44m' not found!")
                      # Manually set a very basic geometry
                      fallback_geom = DiskGeometry(80, 2, 18, 512)
                      fallback_phys = PhysicalFormat(encoding="MFM", rate=500, rpm=300, sectors_per_track=18, heads=2, sector_size=512)
                      self.disk.set_geometry(fallback_geom)
                      if hasattr(self.driver, "set_physical_format"):
                           self.driver.set_physical_format(fallback_phys)

            return True # Succeeded in opening the image, even if format is uncertain

        except Exception as e:
            self.logger.error(f"Error detecting image format: {e}", exc_info=True)
            return False # Indicate failure if error occurs during detection
