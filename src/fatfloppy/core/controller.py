# src/fatfloppy/core/controller.py
"""
Core controller for managing floppy disk images and physical drives.

This module provides the `DiskController` class, which serves as the main
high-level interface for all disk-related operations within the fatfloppy
library. It orchestrates the interactions between different disk drivers
(e.g., Greaseweazle for physical disks, IMG/IMD for image files) and
various filesystem implementations (e.g., FAT12, CP/M).

The `DiskController` is responsible for:
- Opening and closing disk sources.
- Automatically detecting or manually setting disk geometry and format.
- Mounting the appropriate filesystem based on the detected format.
- Providing a unified API for file and directory operations, such as
  listing directories, reading/writing files, and deleting items.
- Creating new, formatted disk images from predefined or custom profiles.
- Flushing data from memory to the disk or image file.

By abstracting the low-level details of disk access and filesystem
structures, the controller offers a simple and consistent way for client
code to interact with a wide variety of floppy disk formats.
"""

import os
import copy
from logging import Logger
from typing import List, Optional, Tuple, Dict, Any, Type

from .utils.logging_config import get_logger
from .disk import Disk
from .driver_factory import DriverFactory
from .drivers import (
    DiskIODriver, GreaseweazleDriver, IMGImageDriver, IMDImageDriver, H17ImageDriver
)
from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .filesystems.fs_base import Filesystem
from .filesystems.fat12fs import FATVolumeInfo, FAT12_MAX_CLUSTERS
from .filesystems.cpm_fs import CPMDiskParameterBlock
from .format_definitions import FLOPPY_FORMATS
from .filesystem_factory import create_filesystem, get_filesystem_class_by_type

logger: Logger = get_logger()


class DiskController:
    """
    Manages disk operations, acting as a high-level interface for interacting
    with physical floppy disks and disk images.

    This class handles driver initialization, format detection, filesystem
    interaction, and file operations. It abstracts the complexities of
    different disk drivers (Greaseweazle, IMG, IMD, H17) and filesystems (FAT12, CP/M, HDOS).
    """

    def __init__(self) -> None:
        """Initializes the DiskController, setting up its initial state."""
        self.logger: Logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None
        self.known_formats: Dict[str, FormatProfile] = FLOPPY_FORMATS
        self.driver: Optional[DiskIODriver] = None
        self.explicit_format_set: bool = False
        self.physical_format: Optional[PhysicalFormat] = None
        self.active_filesystem_config: Optional[Any] = None

    # --- Public Methods: Disk and Format Management ---

    def open_disk(
        self,
        source: str,
        disk_type: str = "IMG",
        drive_letter: str = "A",
        drive_size: str = "3.5",
        format_info: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Opens a disk source (physical drive or image file) and prepares it for use.

        This method initializes the appropriate driver, determines the disk format
        (either explicitly or through auto-detection), and mounts the filesystem.

        Args:
            source: The path to the disk image file or the device name for a physical drive.
            disk_type: The type of disk to open ("IMG", "IMD", "H17", or "physical").
            drive_letter: The drive letter to use (e.g., "A"), relevant for physical drives.
            drive_size: The physical size of the drive (e.g., "3.5", "5.25", "8").
            format_info: An optional dictionary specifying the disk format to apply.

        Returns:
            True if the disk was opened successfully, False otherwise.
        """
        if self.disk:
            self.close_disk()
        self.explicit_format_set = bool(format_info)

        try:
            # Use the factory to create the appropriate driver
            self.driver = DriverFactory.create(
                disk_type,
                source=source,
                drive_letter=drive_letter,
                drive_size=drive_size
            )

            # Validate driver state for non-physical types
            if disk_type == "IMG":
                if not self.driver.file_path and not hasattr(self.driver, 'image_data'):
                    if not (disk_type == "IMG" and not os.path.exists(source)):
                        self.logger.error(f"Driver for {disk_type} at {source} has no path or data.")
                        self.driver = None
                        return False
            elif disk_type == "IMD":
                if not self.driver.file_path:
                    self.logger.error(f"IMD Driver for {source} has no file path.")
                    self.driver = None
                    return False
            elif disk_type == "H17":
                if not self.driver.file_path:
                    self.logger.error(f"H17 Driver for {source} has no file path.")
                    self.driver = None
                    return False

            self.disk = Disk(self.driver)

            success = self._handle_format(format_info, drive_size)

            if not success or not self.disk or not self.disk.physical_format:
                # For IMG, if auto-detection failed but file exists, try a generic default.
                if disk_type == "IMG" and os.path.exists(source) and not self.disk.physical_format and not format_info:
                    self.logger.warning("Auto-detection failed for IMG, trying a generic default geometry to allow BPB parsing.")
                    generic_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1)
                    generic_pf = PhysicalFormat(80, 2, 300, False, 512, [generic_tf])
                    self.disk.set_geometry(generic_pf)
                    if hasattr(self.driver, "set_physical_format"):
                        self.driver.set_physical_format(generic_pf)
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
        """
        Closes the currently open disk, flushing any pending changes and resetting the controller state.
        """
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
        """
        Writes any buffered data to the disk image or physical disk.
        """
        if self.driver and hasattr(self.driver, "flush"):
            try:
                self.driver.flush()
                self.logger.debug("Driver flushed successfully")
            except Exception as e:
                self.logger.error(f"Error flushing driver: {e}")
        else:
            self.logger.warning("Flush called but no active driver or driver lacks flush method.")

    def detect_geometry(self) -> Optional[PhysicalFormat]:
        """
        Returns the currently detected or set physical format of the disk.

        Returns:
            A PhysicalFormat object representing the disk's geometry, or None if not available.
        """
        if not self.disk:
            self.logger.error("No disk opened to detect geometry")
            return None
        # Use the controller's current physical_format if available, as it's the most up-to-date
        if self.physical_format:
            return self.physical_format
        # Fallback if controller's physical_format is not set for some reason
        format_name, _ = self.detect_format()  # This might trigger detection again
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile and profile.physical_format:
                return profile.physical_format
        return None

    def set_geometry(self, geometry: PhysicalFormat) -> None:
        """
        Explicitly sets the physical geometry for the current disk and driver.

        Args:
            geometry: The PhysicalFormat object to apply.

        Raises:
            ValueError: If no disk is currently open.
        """
        if not self.disk:
            self.logger.error("No disk opened to set geometry")
            raise ValueError("No disk opened")
        self.disk.set_geometry(geometry)
        self.physical_format = geometry
        if hasattr(self.driver, "set_physical_format"):  # Also update driver
            self.driver.set_physical_format(copy.deepcopy(geometry))
        if isinstance(self.driver, GreaseweazleDriver):  # Re-create diskdef if GW
            self.driver._create_and_set_custom_diskdef()
        self.logger.debug(f"Geometry set on controller, disk, and driver: {geometry}")

    def detect_format(self) -> Tuple[Optional[str], Optional[Any]]:
        """
        Attempts to auto-detect the disk's format by analyzing its structure.

        Returns:
            A tuple containing:
            - The name of the matched format profile (or None).
            - The specific filesystem configuration (e.g., FATVolumeInfo).
        """
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format")
            return None, None

        from .format_detection import create_format_detector

        try:
            # Create appropriate detector based on driver type
            detector = create_format_detector(
                self.disk,
                self.driver,
                self.known_formats,
                drive_size=getattr(self, '_drive_size_hint', '3.5')  # For Greaseweazle
            )

            format_name, fs_config, physical_format = detector.detect()

            # Update controller state
            if physical_format:
                self.physical_format = physical_format

            if format_name:
                self.logger.info(f"Detected format: {format_name}")
            elif fs_config:
                self.logger.info(f"Filesystem detected: {type(fs_config).__name__}")
            else:
                self.logger.warning("No format detected")

            return format_name, fs_config

        except Exception as e:
            self.logger.exception(f"Format detection failed: {e}")
            return None, None

    def set_format(self, profile: FormatProfile) -> None:
        """
        Applies a specific format profile to the currently open disk.

        This configures the disk and driver with the physical geometry and filesystem
        parameters defined in the profile.

        Args:
            profile: The FormatProfile to apply.

        Raises:
            ValueError: If no disk is open or if the profile is invalid.
        """
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to set format")
            raise ValueError("No disk opened")
        if not profile or not profile.physical_format:
            raise ValueError("Invalid FormatProfile provided")

        if isinstance(self.driver, IMDImageDriver):
            self.logger.warning("Calling set_format with an IMD driver. This will overwrite the format derived from the file.")

        self.logger.info(f"Setting format using profile: {profile.name}")

        physical_format_to_set = copy.deepcopy(profile.physical_format)

        if profile.filesystem_config:
            setattr(physical_format_to_set, '_associated_filesystem_config', profile.filesystem_config)

        self.disk.set_geometry(physical_format_to_set)

        if hasattr(self.driver, "set_physical_format"):
            self.driver.set_physical_format(physical_format_to_set)
            if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_create_and_set_custom_diskdef'):
                self.logger.debug("Applying Greaseweazle custom diskdef after set_format.")
                self.driver._create_and_set_custom_diskdef()
        else:
            self.logger.warning(f"Driver type {type(self.driver).__name__} does not support set_physical_format.")

        self.physical_format = self.disk.physical_format

    def list_formats(self) -> List[Tuple[str, str]]:
        """
        Returns a list of all known, predefined format profiles.

        Returns:
            A list of tuples, where each tuple contains the format name and its description.
        """
        formats = [(name, profile.description) for name, profile in self.known_formats.items()]
        self.logger.debug(f"Listed {len(formats)} known formats")
        return formats

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        """
        Retrieves a format profile by its unique name.

        Args:
            name: The name of the format profile to retrieve.

        Returns:
            The corresponding FormatProfile object, or None if not found.
        """
        profile = self.known_formats.get(name)
        if profile:
            self.logger.debug(f"Retrieved format profile: {name}")
        else:
            self.logger.warning(f"Format profile not found: {name}")
        return profile

    # --- Public Methods: Filesystem Information ---

    def get_allocated_units(self) -> List[int]:
        """
        Gets a list of all allocated clusters or blocks on the filesystem.

        Returns:
            A list of integers representing the allocated units.
        """
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
        """
        Calculates the free space on the disk.

        Returns:
            A tuple containing (total_bytes, free_bytes), or None on error.
        """
        if not self.filesystem or not hasattr(self.filesystem, "get_free_space"):
            return None
        try:
            space_info = self.filesystem.get_free_space()
            self.logger.debug(f"Free space: {space_info}")
            return space_info
        except Exception as e:
            self.logger.error(f"Error getting free space: {e}")
            return None

    # --- Public Methods: File and Directory Operations ---

    def list_directory(self, path: str = "/") -> List[Dict[str, Any]]:
        """
        Lists the contents of a directory on the disk.

        Args:
            path: The path of the directory to list.

        Returns:
            A list of dictionaries, where each dictionary represents a file or subdirectory.
        """
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
        """
        Reads the contents of a file from the disk.

        Args:
            path: The path of the file to read.

        Returns:
            The file content as a bytes object, or None if the file cannot be read.
        """
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
        """
        Writes data to a file on the disk. Creates the file if it doesn't exist.

        Args:
            path: The path of the file to write.
            data: The content to write as a bytes object.

        Returns:
            True on success, False on failure.

        Raises:
            IOError, ValueError, NotImplementedError: On specific filesystem errors.
        """
        if not self.filesystem:
            self.logger.error("write_file called but no filesystem is active.")
            return False
        try:
            self.filesystem.write_file(path, data)
            self.logger.debug(f"Wrote file {path} with size {len(data)} bytes")
            return True
        except (IOError, ValueError, NotImplementedError) as e:
            self.logger.error(f"Error writing file {path}: {e}")
            raise  # Re-raise the exception so the caller knows what went wrong
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while writing file {path}: {e}")
            raise

    def create_directory(self, path: str) -> bool:
        """
        Creates a new directory on the disk.

        Args:
            path: The path of the directory to create.

        Returns:
            True on success, False on failure.

        Raises:
            IOError, ValueError, NotImplementedError: On specific filesystem errors.
        """
        if not self.filesystem:
            self.logger.error("create_directory called but no filesystem is active.")
            return False
        try:
            self.filesystem.create_directory(path)
            self.logger.debug(f"Created directory {path}")
            return True
        except (IOError, ValueError, NotImplementedError) as e:
            self.logger.error(f"Error creating directory {path}: {e}")
            raise  # Re-raise the exception
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while creating directory {path}: {e}")
            raise

    def delete_item(self, path: str) -> bool:
        """
        Deletes a file or an empty directory from the disk.

        Args:
            path: The path of the item to delete.

        Returns:
            True on success, False on failure.

        Raises:
            IOError, ValueError, NotImplementedError, FileNotFoundError: On specific filesystem errors.
        """
        if not self.filesystem:
            self.logger.error("delete_item called but no filesystem is active.")
            return False
        try:
            self.filesystem.delete(path)
            self.logger.debug(f"Deleted item {path}")
            return True
        except (IOError, ValueError, NotImplementedError, FileNotFoundError) as e:
            self.logger.error(f"Error deleting item {path}: {e}")
            raise  # Re-raise the exception
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while deleting {path}: {e}")
            raise

    def delete_item_recursive(self, path: str) -> bool:
        """
        Recursively deletes a file or a directory and its contents.

        Args:
            path: The path of the item to delete.

        Returns:
            True on success, False on failure.

        Raises:
            IOError, ValueError, NotImplementedError, FileNotFoundError: On specific filesystem errors.
        """
        if not self.filesystem:
            self.logger.error("delete_item_recursive called but no filesystem is active.")
            return False
        try:
            success = self.filesystem.delete_recursive(path)
            if success:
                self.logger.debug(f"Deleted item recursively {path}")
            return success
        except (IOError, ValueError, NotImplementedError, FileNotFoundError) as e:
            self.logger.error(f"Error deleting item recursively {path}: {e}")
            raise  # Re-raise the exception
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while recursively deleting {path}: {e}")
            raise

    # --- Public Methods: Disk Creation and Formatting ---

    def format_disk(self, format_name: str, volume_label: str = "NO NAME") -> bool:
        """
        Formats the currently open disk using a specified format profile.

        This process erases all data on the disk and initializes a new filesystem.

        Args:
            format_name: The name of the format profile to use.
            volume_label: The volume label to assign to the newly formatted disk.

        Returns:
            True if formatting was successful, False otherwise.
        """
        if isinstance(self.driver, IMDImageDriver):
            self.logger.error("Formatting is not supported directly via the IMDImageDriver.")
            return False
        if isinstance(self.driver, H17ImageDriver):
            self.logger.error("Formatting is not supported for already-open H17 images. Use create_and_format_image instead.")
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
            if not profile:
                if hasattr(self.driver, 'physical_format') and self.driver.physical_format and hasattr(self.driver.physical_format, '_associated_filesystem_config'):
                    fs_cfg = getattr(self.driver.physical_format, '_associated_filesystem_config')
                    fs_type = "FAT12" if isinstance(fs_cfg, FATVolumeInfo) else "Unknown"
                    if isinstance(fs_cfg, CPMDiskParameterBlock):
                        fs_type = "CPM"

                    profile = FormatProfile(name="custom_runtime", description="Custom (Runtime)",
                                            physical_format=self.driver.physical_format,
                                            filesystem_type=fs_type, filesystem_config=fs_cfg)
                    format_name = "custom_runtime"
                else:
                    self.logger.error(f"Cannot format with unknown profile '{format_name}' and no fallback.")
                    return False

        if not profile or not profile.physical_format or not profile.filesystem_config:
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

    def create_and_format_image(self, file_path: str, profile: FormatProfile,
                            volume_label: str = "NO NAME", disk_type: str = "IMG") -> bool:
        """
        Creates a new disk image file and formats it.

        Args:
            file_path: The path where the new image file will be created.
            profile: The FormatProfile defining the geometry and filesystem.
            volume_label: The volume label for the new filesystem.
            disk_type: The type of image to create ("IMG", "IMD", or "H17").

        Returns:
            True on successful creation and formatting, False otherwise.
        """
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

                # Use factory to create driver
                self.driver = DriverFactory.create("IMG", source=file_path)
                if not self.driver or len(self.driver.image_data) != total_bytes:
                    raise IOError(f"Failed to create or correctly size raw image file '{file_path}'")

            elif disk_type == "IMD":
                self.driver = DriverFactory.create("IMD", source=file_path)
                if hasattr(self.driver, 'format_imd') and isinstance(self.driver, IMDImageDriver):
                    fill_byte = 0xE5
                    self.driver.format_imd(profile, fill_byte=fill_byte)

            elif disk_type == "H17":
                self.driver = DriverFactory.create("H17", source=file_path)
                if hasattr(self.driver, 'format_h17') and isinstance(self.driver, H17ImageDriver):
                    scheme = 'cpm' if profile.filesystem_type == 'CPM' else 'hdos'
                    hdos_volume = 1

                    if profile.filesystem_type == 'HDOS' and hasattr(profile.filesystem_config, 'volume_number'):
                        hdos_volume = profile.filesystem_config.volume_number

                    self.driver.format_h17(
                        sides=profile.physical_format.heads,
                        tracks=profile.physical_format.cylinders,
                        scheme=scheme,
                        hdos_volume=hdos_volume,
                        label=self.driver.metadata.label or volume_label,
                        comment=f"Created by FatFloppy"
                    )
                    self.logger.info(f"H17 image formatted with {scheme.upper()} volume scheme")

            else:
                self.logger.error(f"Unsupported disk type: {disk_type} for image creation.")
                return False

            self.disk = Disk(self.driver)
            self.logger.info(f"Setting format for new {disk_type} image using profile: {profile.name}")
            self.set_format(profile)

            self.logger.info(f"Formatting new {disk_type} image with volume label: '{volume_label}'")
            filesystem_handler = fs_class(self.disk)
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

    def create_custom_profile(self, format_info: Dict[str, Any]) -> Optional[FormatProfile]:
        """
        Creates a new FormatProfile dynamically from a dictionary of parameters.

        This allows for working with non-standard or user-defined disk formats.

        Args:
            format_info: A dictionary containing physical and logical format parameters.

        Returns:
            A new FormatProfile object, or None if the parameters are invalid.
        """
        try:
            physical_format = self._create_physical_format(format_info)
            target_fs_type = format_info.get("filesystem_type", "FAT12")
            filesystem_config_obj: Optional[Any] = None

            if target_fs_type == "FAT12":
                total_sectors = physical_format.total_sectors
                bytes_per_sector = physical_format.bytes_per_sector
                sectors_per_cluster = format_info.get("sectors_per_cluster", 1)
                if sectors_per_cluster == 0:
                    sectors_per_cluster = 1
                reserved_sectors = format_info.get("reserved_sectors", 1)
                num_fats = format_info.get("num_fats", 2)
                root_entries = format_info.get("root_entries", 224 if total_sectors > 1440 else 112)

                if bytes_per_sector == 0:
                    self.logger.error("Bytes per sector cannot be zero for custom FAT12 profile.")
                    return None
                root_dir_sectors = (root_entries * 32 + bytes_per_sector - 1) // bytes_per_sector

                available_for_fats_and_data = total_sectors - (reserved_sectors + root_dir_sectors)
                if available_for_fats_and_data < 0:
                    self.logger.error("Not enough space for reserved and root directory sectors in custom FAT12 profile.")
                    return None

                sectors_per_fat = 1

                for _attempt in range(available_for_fats_and_data // (num_fats if num_fats > 0 else 1) + 1):
                    if num_fats == 0:
                        data_sectors = available_for_fats_and_data
                    else:
                        data_sectors = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)

                    if data_sectors < sectors_per_cluster:
                        break

                    num_clusters_current_try = data_sectors // sectors_per_cluster
                    if num_clusters_current_try <= 0:
                        break

                    fat_bytes_needed = ((num_clusters_current_try + 2) * 3 + 1) // 2

                    spf_needed_for_this_many_clusters = (fat_bytes_needed + bytes_per_sector - 1) // bytes_per_sector

                    if spf_needed_for_this_many_clusters <= sectors_per_fat:
                        break
                    else:
                        sectors_per_fat = spf_needed_for_this_many_clusters
                else:
                    self.logger.error("Could not determine a consistent sectors_per_fat for FAT12. Data area too small or params conflicting.")
                    return None

                if num_fats > 0:
                    data_s = total_sectors - (reserved_sectors + (num_fats * sectors_per_fat) + root_dir_sectors)
                else:
                    data_s = available_for_fats_and_data

                if data_s < sectors_per_cluster:
                    self.logger.error(f"Final data sectors ({data_s}) less than sectors_per_cluster ({sectors_per_cluster}). Cannot create profile.")
                    return None
                final_num_clusters = data_s // sectors_per_cluster

                if final_num_clusters <= 0:
                    self.logger.error(f"Final calculated non-positive number of clusters ({final_num_clusters}). Cannot create profile.")
                    return None
                if final_num_clusters > FAT12_MAX_CLUSTERS:
                    self.logger.warning(f"Calculated cluster count ({final_num_clusters}) for custom FAT12 profile exceeds typical limit of {FAT12_MAX_CLUSTERS}. This may lead to issues.")

                filesystem_config_obj = FATVolumeInfo(
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
            elif target_fs_type == "CPM":
                if 'CPMDiskParameterBlock' in globals():
                    filesystem_config_obj = CPMDiskParameterBlock(
                        spt=format_info.get("spt", physical_format.track_formats[0].sectors_per_track * (physical_format.bytes_per_sector // 128)),
                        bsh=format_info.get("bsh", 3),
                        blm=format_info.get("blm", (2**format_info.get("bsh", 3)) - 1),
                        exm=format_info.get("exm", 0),
                        dsm=format_info.get("dsm", (physical_format.total_sectors * (physical_format.bytes_per_sector // 128)) // (2**format_info.get("bsh", 3)) - 10),
                        drm=format_info.get("drm", 63),
                        al0=format_info.get("al0", 0xC0),
                        al1=format_info.get("al1", 0x00),
                        cks=format_info.get("cks", 0),
                        off=format_info.get("off", 2)
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

    # --- Private Methods: Driver and Format Creation ---

    def _create_physical_format(self, format_info: Dict[str, any], base_profile: Optional[FormatProfile] = None) -> PhysicalFormat:
        """
        Constructs a PhysicalFormat object from a dictionary of parameters.

        It uses a base profile for default values if one is provided.

        Args:
            format_info: Dictionary with geometry parameters.
            base_profile: An optional base FormatProfile to use for defaults.

        Returns:
            A new PhysicalFormat object.
        """
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

    # --- Private Methods: Format Handling and Detection ---

    def _apply_user_format(self, format_info: Dict[str, any]) -> None:
        """
        Applies a user-provided format to the current disk and driver.

        Args:
            format_info: A dictionary containing the format parameters.
        """
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

    def _handle_format(self, format_info: Optional[Dict[str, Any]], drive_size: str) -> bool:
        """
        Coordinates the format handling process when a disk is opened.

        Args:
            format_info: The user-provided format, or None.
            drive_size: The physical drive size, used for detection hints.

        Returns:
            True if a valid format was successfully applied or detected, False otherwise.
        """
        # Store drive size for detector
        self._drive_size_hint = drive_size

        # Metadata-based drivers (IMD, H17) get special handling
        from .drivers import IMDImageDriver, H17ImageDriver

        if isinstance(self.driver, (IMDImageDriver, H17ImageDriver)):
            if not self.driver.physical_format:
                self.logger.error(f"{type(self.driver).__name__} driver has no physical format")
                return False

            self.disk.set_geometry(self.driver.physical_format)
            self.physical_format = self.driver.physical_format

            if format_info:
                self.logger.warning(f"Applying user format_info to {type(self.driver).__name__} may override metadata")
                try:
                    self._apply_user_format(format_info)
                except Exception as e:
                    self.logger.error(f"Failed to apply user format: {e}")
                    return False

            return True

        # User-specified format
        if format_info:
            try:
                self._apply_user_format(format_info)
                self.logger.debug(f"Applied user format. Geometry: {self.disk.physical_format}")
                return True
            except Exception as e:
                self.logger.error(f"Failed applying user format: {e}")
                return False

        # Auto-detection needed
        format_name, fs_config = self.detect_format()

        # Check if we got anything useful
        if format_name or fs_config:
            return True

        # Last resort for IMG: try generic geometry
        from .drivers import IMGImageDriver
        if isinstance(self.driver, IMGImageDriver):
            self.logger.warning("IMG detection failed, trying generic default")
            generic_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1)
            generic_pf = PhysicalFormat(80, 2, 300, False, 512, [generic_tf])
            self.disk.set_geometry(generic_pf)
            if hasattr(self.driver, "set_physical_format"):
                self.driver.set_physical_format(generic_pf)
            return True

        return False

    def _auto_detect_for_image(self) -> bool:
        """
        Auto-detect format for an IMG driver based on image size and filesystem validation.
        (Currently not called by other methods in this file).

        Returns:
            True if a format was successfully detected, False otherwise.
        """
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
                if fs:
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

    def _get_default_geometry_and_physical(self, drive_size: str) -> Tuple[PhysicalFormat, PhysicalFormat]:
        """
        Generates a default, generic geometry based on drive size.

        Args:
            drive_size: The drive size ("3.5", "5.25", or "8").

        Returns:
            A tuple containing two identical PhysicalFormat objects.

        Raises:
            ValueError: If the drive_size is unsupported.
        """
        if drive_size == "3.5":
            cylinders = 80
            sectors_per_track = 18
            bytes_per_sector = 512
            rate = 500
            encoding = "MFM"
            rpm = 300
            heads = 2
        elif drive_size == "5.25":
            cylinders = 40
            sectors_per_track = 9
            bytes_per_sector = 512
            rate = 250
            encoding = "MFM"
            rpm = 300
            heads = 2
        elif drive_size == "8":
            cylinders = 77
            sectors_per_track = 26
            bytes_per_sector = 128
            rate = 250
            encoding = "FM"
            rpm = 360
            heads = 2
        else:
            raise ValueError(f"Unsupported drive size: {drive_size}")
        track_format = TrackFormat(0, cylinders - 1, 0, heads - 1, sectors_per_track, encoding, rate, 1, gap3_bytes=84)
        geometry = PhysicalFormat(cylinders, heads, rpm, False, bytes_per_sector, [track_format])
        self.logger.debug(f"Created default geometry for drive size {drive_size}")
        return geometry, geometry

    def _check_second_head(self, temp_profile: FormatProfile) -> bool:
        """
        Checks for the presence of a second read/write head.

        It attempts to read a sector from head 1.

        Args:
            temp_profile: A temporary profile to use for the check.

        Returns:
            True if a second head is detected, False otherwise.
        """
        if self.filesystem and (specific_config := self.filesystem.get_specific_config()):
            if hasattr(specific_config, 'num_heads') and isinstance(specific_config.num_heads, int) and specific_config.num_heads > 0:
                return specific_config.num_heads > 1

        current_physical_format_before_check = copy.deepcopy(self.disk.physical_format)
        self.set_format(temp_profile)
        has_second_head_result = False
        try:
            if isinstance(self.driver, GreaseweazleDriver) and hasattr(self.driver, '_read_track'):
                has_second_head_result = bool(self.driver._read_track(0, 1))
            else:
                self.disk.read_sector(0, 1, 1)
                has_second_head_result = True
        except Exception:
            has_second_head_result = False
        finally:
            if current_physical_format_before_check:
                self.set_format(FormatProfile("restore", "", current_physical_format_before_check, "Unknown", None))
            elif self.disk.physical_format != temp_profile.physical_format:
                self.set_format(temp_profile)
        return has_second_head_result
