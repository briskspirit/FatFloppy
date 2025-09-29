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
from .drivers import (
    DiskIODriver, GreaseweazleDriver, IMGImageDriver, IMDImageDriver, H17ImageDriver
)
from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .filesystems.fs_base import Filesystem
from .filesystems.fat12fs import FATFilesystem, FATVolumeInfo, FAT12_MAX_CLUSTERS
from .filesystems.cpm_fs import CPMFilesystem, CPMDiskParameterBlock
from .filesystems.hdos_fs import HDOSFilesystem, HDOSLabelRecord
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
            disk_type: The type of disk to open ("IMG", "IMD", or "physical").
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
                    generic_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1)  # Default 1.44M like
                    generic_pf = PhysicalFormat(80, 2, 300, False, 512, [generic_tf])
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

        This method orchestrates several detection strategies:
        1. Uses IMD metadata if the file is a self-describing .imd image.
        2. Uses H17 metadata if the file is a .h17disk image.  # ADD THIS LINE
        3. Attempts a direct parse of the boot sector for a FAT BPB.
        4. Iterates through all known format profiles as a fallback.

        Returns:
            A tuple containing:
            - The name of the matched format profile (or None).
            - The specific filesystem configuration (e.g., FATVolumeInfo).
        """
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format (DiskController.detect_format)")
            return None, None

        initial_format = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        # Strategy 1: IMD-specific detection
        if isinstance(self.driver, IMDImageDriver):
            return self._detect_format_for_imd()

        # Strategy 1.5: H17-specific detection  # ADD THIS BLOCK
        if isinstance(self.driver, H17ImageDriver):
            return self._detect_format_for_h17()

        # Strategy 2: Direct parse of boot sector (for FAT)
        result = self._detect_format_by_direct_parse()
        if result:
            return result

        # Restore state after failed direct parse before trying next strategy
        if initial_format:
            if not self.disk.physical_format or self.disk.physical_format != initial_format:
                self.set_geometry(initial_format)
        elif self.disk.physical_format is not None:
            self.disk.physical_format = None
            self.physical_format = None

        # Strategy 3: Iteration over all known format profiles
        result = self._detect_format_by_profile_iteration(initial_format)
        if result:
            return result

        # All strategies failed
        self.logger.warning("detect_format: No format detected after all attempts.")
        if initial_format:
            if not self.disk.physical_format or self.disk.physical_format != initial_format:
                self.set_geometry(initial_format)
        elif self.disk.physical_format is not None:
            self.disk.physical_format = None
            self.physical_format = None

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
            disk_type: The type of image to create ("IMG", "IMD", or "H17").  # UPDATED

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
                self.driver = self._create_img_driver(file_path)
                if not self.driver or len(self.driver.image_data) != total_bytes:
                    raise IOError(f"Failed to create or correctly size raw image file '{file_path}'")

            elif disk_type == "IMD":
                self.driver = self._create_imd_driver(file_path)
                if hasattr(self.driver, 'format_imd') and isinstance(self.driver, IMDImageDriver):
                    fill_byte = 0xE5
                    self.driver.format_imd(profile, fill_byte=fill_byte)

            # ADD THIS BLOCK
            elif disk_type == "H17":
                self.driver = self._create_h17_driver(file_path)
                if hasattr(self.driver, 'format_h17') and isinstance(self.driver, H17ImageDriver):
                    # Determine volume scheme from filesystem type
                    scheme = 'cpm' if profile.filesystem_type == 'CPM' else 'hdos'
                    hdos_volume = 1  # Default HDOS volume

                    # Try to extract volume from filesystem config
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

    def _create_driver(self, disk_type: str, source: str, drive_letter: str, drive_size: str) -> DiskIODriver:
        """
        Internal factory method to create the appropriate disk driver.

        Args:
            disk_type: The type of disk ("physical", "IMG", "IMD", "H17").  # UPDATED comment
            source: The device name or file path.
            drive_letter: The drive letter for physical drives.
            drive_size: The drive size for physical drives.

        Returns:
            An initialized DiskIODriver instance.

        Raises:
            ValueError: If the disk_type is unsupported.
        """
        if disk_type == "physical":
            return self._create_physical_driver(source, drive_letter, drive_size)
        elif disk_type == "IMG":
            return self._create_img_driver(source)
        elif disk_type == "IMD":
            return self._create_imd_driver(source)
        elif disk_type == "H17":
            return self._create_h17_driver(source)
        else:
            raise ValueError(f"Unsupported disk type: {disk_type}")

    def _create_physical_driver(self, source: str, drive_letter: str, drive_size: str) -> GreaseweazleDriver:
        """
        Creates and initializes a GreaseweazleDriver for a physical floppy drive.

        Args:
            source: The device name of the Greaseweazle.
            drive_letter: The drive letter to use (e.g., "A").
            drive_size: The physical size of the drive (e.g., "3.5").

        Returns:
            An initialized GreaseweazleDriver instance.
        """
        device_name = source if source else None
        driver = GreaseweazleDriver(device_name=device_name, drive=drive_letter, drive_size=drive_size)
        try:
            driver.initialize()  # RPM measurement happens here
            self.logger.debug(f"Initialized Greaseweazle driver for {source}")
        except Exception as e:
            self.logger.error(f"Error initializing Greaseweazle driver: {e}")
            raise
        return driver

    def _create_img_driver(self, source: str) -> IMGImageDriver:
        """
        Creates an IMGImageDriver for a raw disk image file.

        Args:
            source: The file path to the IMG image.

        Returns:
            An IMGImageDriver instance.
        """
        driver = IMGImageDriver(file_path=source)
        self.logger.debug(f"Created IMG driver for {source}")
        return driver

    def _create_imd_driver(self, source: str) -> IMDImageDriver:
        """
        Creates an IMDImageDriver for an ImageDisk file.

        Args:
            source: The file path to the IMD image.

        Returns:
            An IMDImageDriver instance.
        """
        driver = IMDImageDriver(file_path=source)
        self.logger.debug(f"Created IMD driver for {source}")
        return driver

    def _create_h17_driver(self, source: str) -> H17ImageDriver:
        """
        Creates an H17ImageDriver for a Heathkit hard-sectored disk image.

        Args:
            source: The file path to the H17 image.

        Returns:
            An H17ImageDriver instance.
        """
        driver = H17ImageDriver(file_path=source)
        self.logger.debug(f"Created H17 driver for {source}")
        return driver

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

        It decides whether to apply a user-specified format or trigger auto-detection.

        Args:
            format_info: The user-provided format, or None.
            drive_size: The physical drive size, used for detection hints.

        Returns:
            True if a valid format was successfully applied or detected, False otherwise.
        """
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
        elif isinstance(self.driver, H17ImageDriver):
            if not self.driver.physical_format:
                self.logger.error("H17 driver loaded but failed to derive physical format.")
                return False
            self.disk.set_geometry(self.driver.physical_format)
            self.physical_format = self.driver.physical_format
            if format_info:
                self.logger.warning("Applying user format_info to an H17 disk, this may override H17 metadata.")
                try:
                    self._apply_user_format(format_info)
                except Exception as e:
                    self.logger.error(f"Failed to apply user format override to H17: {e}")
                    return False
            return True
        elif format_info:  # User provided explicit format
            try:
                self._apply_user_format(format_info)
                self.logger.debug(f"Applied user format. Geometry: {self.disk.physical_format}")
                return True
            except Exception as e:
                self.logger.error(f"Failed applying user format: {e}")
                return False
        else:  # Auto-detection needed
            if isinstance(self.driver, GreaseweazleDriver):
                success = self._detect_physical_disk_format(drive_size)
            elif isinstance(self.driver, IMGImageDriver):
                success = self._detect_image_file_format(self.driver.file_path)
            else:
                success = False  # Should not happen if IMD handled above

            if success:
                self.logger.info(f"Auto-detection successful. Final disk geometry: {self.disk.physical_format}")
            else:
                self.logger.warning("Auto-detection of format failed.")
            return success

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

    def _detect_physical_disk_format(self, drive_size: str = "3.5") -> bool:
        """
        Performs the auto-detection sequence for a physical disk.

        It checks for a second head, scans tracks to infer geometry, and iterates
        through known formats to find a valid filesystem.

        Args:
            drive_size: The physical size of the drive ("3.5", "5.25", etc.).

        Returns:
            True if a suitable format was found, False otherwise.
        """
        true_initial_controller_pf = copy.deepcopy(self.physical_format) if self.physical_format else None
        temp_geometry, _ = self._get_default_geometry_and_physical(drive_size)
        temp_profile = FormatProfile("temp_detect", "Temporary for detection", temp_geometry, "Unknown", None)

        self.set_format(temp_profile)

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

        self.filesystem = create_filesystem(self.disk)

        current_disk_pf_for_head_check = self.disk.physical_format if self.disk.physical_format else temp_profile.physical_format
        fs_config_for_head_check = self.filesystem.get_specific_config() if self.filesystem else None
        fs_type_for_head_check = fs_config_for_head_check.__class__.__name__ if fs_config_for_head_check else "Unknown"
        temp_profile_for_head_check = FormatProfile("head_check_temp", "", current_disk_pf_for_head_check,
                                                    fs_type_for_head_check, fs_config_for_head_check)

        has_second_head = self._check_second_head(temp_profile_for_head_check)

        filtered_formats = self._filter_known_formats(drive_size, has_second_head)
        matching_profile = self._find_matching_format(filtered_formats)

        if matching_profile:
            self.logger.info(f"Physical disk format detected and set to: {matching_profile.name} with geometry {self.disk.physical_format}")
            return True

        self.logger.warning("No known format profile matched physical disk after all checks. Using a fallback geometry.")
        base_geom_for_fallback = true_initial_controller_pf if true_initial_controller_pf else self.physical_format
        if not base_geom_for_fallback:
            base_geom_for_fallback = temp_geometry

        self.set_geometry(base_geom_for_fallback)

        self.filesystem = create_filesystem(self.disk)

        final_default_heads = 2 if has_second_head else 1
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
            if bs and bs.is_valid():
                final_default_heads = bs.num_heads if bs.num_heads > 0 else final_default_heads
                final_fallback_spt = bs.sectors_per_track if bs.sectors_per_track > 0 else final_fallback_spt
                final_fallback_bps = bs.bytes_per_sector if bs.bytes_per_sector > 0 else final_fallback_bps
                if bs.num_heads > 0 and bs.sectors_per_track > 0 and bs.total_sectors > 0:
                    final_fallback_cyls = bs.total_sectors // (bs.num_heads * bs.sectors_per_track)
                fs_config_from_fallback_base = bs

        final_fallback_tf = TrackFormat(
            0, final_fallback_cyls - 1, 0, final_default_heads - 1,
            final_fallback_spt, final_fallback_encoding, final_fallback_rate,
            final_fallback_interleave, gap3_bytes=final_fallback_gap3
        )
        final_fallback_geom = PhysicalFormat(
            final_fallback_cyls, final_default_heads, final_fallback_rpm,
            final_fallback_inverted, final_fallback_bps, [final_fallback_tf]
        )
        fallback_fs_type = "FAT12" if isinstance(fs_config_from_fallback_base, FATVolumeInfo) else "Unknown"

        final_fallback_profile = FormatProfile(
            name="fallback_detected", description="Fallback based on physical detection",
            physical_format=final_fallback_geom,
            filesystem_type=fallback_fs_type,
            filesystem_config=fs_config_from_fallback_base
        )
        self.set_format(final_fallback_profile)
        self.logger.info(f"Physical disk format set to fallback: {final_fallback_profile.description} with geometry {self.disk.physical_format}")
        return True

    def _detect_image_file_format(self, file_path: str) -> bool:
        """
        Performs the auto-detection sequence for an image file.

        This primarily relies on the more general `detect_format` method.

        Args:
            file_path: The path to the image file (used for logging).

        Returns:
            True if a format was determined, False otherwise.
        """
        format_name, fs_config = self.detect_format()

        if format_name and fs_config:
            self.logger.info(f"Image file '{file_path}' matched profile: {format_name}. Current geometry: {self.disk.physical_format}. FS Config: {type(fs_config)}")
            return True
        elif fs_config:
            self.logger.info(f"Image file '{file_path}': Parsed filesystem data, using constructed/set geometry: {self.disk.physical_format}. FS Config: {type(fs_config)}")
            return True

        self.logger.warning(f"Could not determine a suitable format for image file: {file_path}.")
        if not self.disk.physical_format:
            self.logger.debug(f"Setting a generic default geometry for {file_path} as last resort for IMG after failed detection.")
            for default_prof_name in ["ibm_3.5_1.44m", "ibm_5.25_360k", "ibm_3.5_720k"]:
                profile_default = self.get_format_by_name(default_prof_name)
                if profile_default and profile_default.physical_format.total_bytes == os.path.getsize(file_path):
                    self.set_format(profile_default)
                    self.filesystem = create_filesystem(self.disk)
                    if self.filesystem:
                        self.logger.info(f"Last resort for IMG: Matched profile {default_prof_name} by size.")
                        return True

            tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1)
            pf = PhysicalFormat(80, 2, 300, False, 512, [tf])
            generic_profile = FormatProfile("generic_fallback", "Generic Fallback", pf, "Unknown", None)
            self.set_format(generic_profile)
            return True
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

    def _filter_known_formats(self, drive_size: str, has_second_head: bool) -> List[FormatProfile]:
        """
        Filters the list of known formats based on drive size and head count.

        Args:
            drive_size: The drive size string (e.g., "3.5").
            has_second_head: Boolean indicating if a second head is present.

        Returns:
            A list of potentially matching FormatProfile objects.
        """
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
        """
        Iterates through a filtered list of formats to find one that matches the disk.

        For each profile, it applies the format and attempts to validate the filesystem.

        Args:
            filtered_formats: A list of candidate FormatProfiles.

        Returns:
            The first matching FormatProfile, or None if no match is found.
        """
        original_disk_format_on_entry = copy.deepcopy(self.disk.physical_format) if self.disk.physical_format else None

        for profile in filtered_formats:
            self.logger.debug(f"_find_matching_format: Trying profile '{profile.name}'")
            self.set_format(profile)

            try:
                fs_class: Optional[Type[Filesystem]] = get_filesystem_class_by_type(profile.filesystem_type)
                if not fs_class:
                    continue

                fs = fs_class(self.disk)
                if fs.get_validity_score() < getattr(fs, 'VALIDITY_THRESHOLD', 30):
                    continue

                # Perform consistency check if applicable
                perform_bpb_check = True

                if isinstance(fs, FATFilesystem) and isinstance(profile.filesystem_config, FATVolumeInfo):
                    profile_bpb = profile.filesystem_config
                    current_bpb_from_fs_obj = fs.boot_sector

                    if not (current_bpb_from_fs_obj and
                            profile_bpb.sectors_per_track == current_bpb_from_fs_obj.sectors_per_track and
                            profile_bpb.num_heads == current_bpb_from_fs_obj.num_heads and
                            profile_bpb.total_sectors == current_bpb_from_fs_obj.total_sectors and
                            profile_bpb.bytes_per_sector == current_bpb_from_fs_obj.bytes_per_sector and
                            profile_bpb.sectors_per_fat == current_bpb_from_fs_obj.sectors_per_fat and
                            profile.physical_format.get_sectors_per_track(0, 0) == current_bpb_from_fs_obj.sectors_per_track and
                            profile.physical_format.heads == current_bpb_from_fs_obj.num_heads and
                            profile.physical_format.bytes_per_sector == current_bpb_from_fs_obj.bytes_per_sector):
                        self.logger.debug(f"Profile '{profile.name}' (FAT) BPB/Geom mismatch with current disk BPB. Skipping.")
                        perform_bpb_check = False
                elif isinstance(fs, CPMFilesystem) and isinstance(profile.filesystem_config, CPMDiskParameterBlock):
                    profile_dpb = profile.filesystem_config
                    current_dpb_from_fs_obj = fs.get_specific_config()
                    if not (current_dpb_from_fs_obj and
                            profile_dpb.spt == current_dpb_from_fs_obj.spt and
                            profile_dpb.bsh == current_dpb_from_fs_obj.bsh and
                            profile_dpb.dsm == current_dpb_from_fs_obj.dsm and
                            profile_dpb.off == current_dpb_from_fs_obj.off):
                        self.logger.debug(f"Profile '{profile.name}' (CP/M) DPB mismatch with current disk DPB. Skipping.")
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

    def _detect_format_for_imd(self) -> Tuple[Optional[str], Optional[Any]]:
        """Handles format detection specifically for an IMDImageDriver."""
        if not isinstance(self.driver, IMDImageDriver) or not self.driver.physical_format:
            self.logger.warning("IMD: No derived physical format available for detection.")
            return None, None

        self.set_geometry(self.driver.physical_format)
        parsed_fs_config: Optional[Any] = None

        if self.driver.read_boot_sector_data():
            try:
                temp_fs = create_filesystem(self.disk)
                if temp_fs:
                    parsed_fs_config = temp_fs.get_specific_config()
                    self.logger.info(f"IMD: Parsed system area using {temp_fs.__class__.__name__}.")
            except ValueError:
                self.logger.warning("IMD: Failed to parse system area for any known FS type.")

        if not parsed_fs_config:
            return None, None

        # Match against known profiles using more detailed criteria
        for pf_name, pf_profile in self.known_formats.items():
            is_fat_match = (pf_profile.filesystem_type == "FAT12" and
                            isinstance(pf_profile.filesystem_config, FATVolumeInfo) and
                            isinstance(parsed_fs_config, FATVolumeInfo))
            is_cpm_match = (pf_profile.filesystem_type == "CPM" and
                            isinstance(pf_profile.filesystem_config, CPMDiskParameterBlock) and
                            isinstance(parsed_fs_config, CPMDiskParameterBlock))
            # TODO: add HDOS match

            physical_match = (
                pf_profile.physical_format and
                pf_profile.physical_format.cylinders == self.driver.physical_format.cylinders and
                pf_profile.physical_format.heads == self.driver.physical_format.heads and
                pf_profile.physical_format.bytes_per_sector == self.driver.physical_format.bytes_per_sector and
                pf_profile.physical_format.get_sectors_per_track(0, 0) == self.driver.physical_format.get_sectors_per_track(0, 0)
            )
            if not physical_match:
                continue

            logical_match = False
            if is_fat_match and pf_profile.filesystem_config.total_sectors == parsed_fs_config.total_sectors:
                logical_match = True
            elif is_cpm_match and all([
                pf_profile.filesystem_config.spt == parsed_fs_config.spt,
                pf_profile.filesystem_config.bsh == parsed_fs_config.bsh,
                pf_profile.filesystem_config.dsm == parsed_fs_config.dsm,
                pf_profile.filesystem_config.off == parsed_fs_config.off,
            ]):
                logical_match = True

            if logical_match:
                self.logger.info(f"IMD: Matched known profile '{pf_name}'")
                return pf_name, parsed_fs_config

        self.logger.info("IMD: System area parsed, but no exact profile match. Using derived parameters.")
        return None, parsed_fs_config

    def _detect_format_for_h17(self) -> Tuple[Optional[str], Optional[Any]]:
        """Handles format detection specifically for an H17ImageDriver."""
        if not isinstance(self.driver, H17ImageDriver) or not self.driver.physical_format:
            self.logger.warning("H17: No derived physical format available for detection.")
            return None, None

        self.set_geometry(self.driver.physical_format)
        parsed_fs_config: Optional[Any] = None

        # Try to parse the filesystem
        try:
            temp_fs = create_filesystem(self.disk)
            if temp_fs:
                parsed_fs_config = temp_fs.get_specific_config()
                self.logger.info(f"H17: Parsed filesystem using {temp_fs.__class__.__name__}.")

                # Apply volumes if the filesystem supports it
                if hasattr(temp_fs, 'apply_volume_to_driver'):
                    try:
                        temp_fs.apply_volume_to_driver()
                        self.logger.info("H17: Applied filesystem-specific volume scheme")
                    except Exception as e:
                        self.logger.warning(f"H17: Could not apply volumes: {e}")
        except ValueError:
            self.logger.warning("H17: Failed to parse filesystem.")

        if not parsed_fs_config:
            return None, None

        # Try to match against known profiles
        for pf_name, pf_profile in self.known_formats.items():
            is_fat_match = (pf_profile.filesystem_type == "FAT12" and
                            isinstance(pf_profile.filesystem_config, FATVolumeInfo) and
                            isinstance(parsed_fs_config, FATVolumeInfo))
            is_cpm_match = (pf_profile.filesystem_type == "CPM" and
                            isinstance(pf_profile.filesystem_config, CPMDiskParameterBlock) and
                            isinstance(parsed_fs_config, CPMDiskParameterBlock))
            is_hdos_match = (pf_profile.filesystem_type == "HDOS" and
                            isinstance(pf_profile.filesystem_config, HDOSLabelRecord) and
                            isinstance(parsed_fs_config, HDOSLabelRecord))

            physical_match = (
                pf_profile.physical_format and
                pf_profile.physical_format.cylinders == self.driver.physical_format.cylinders and
                pf_profile.physical_format.heads == self.driver.physical_format.heads and
                pf_profile.physical_format.bytes_per_sector == self.driver.physical_format.bytes_per_sector and
                pf_profile.physical_format.get_sectors_per_track(0, 0) == self.driver.physical_format.get_sectors_per_track(0, 0)
            )
            if not physical_match:
                continue

            logical_match = False
            if is_fat_match and pf_profile.filesystem_config.total_sectors == parsed_fs_config.total_sectors:
                logical_match = True
            elif is_cpm_match and all([
                pf_profile.filesystem_config.spt == parsed_fs_config.spt,
                pf_profile.filesystem_config.bsh == parsed_fs_config.bsh,
                pf_profile.filesystem_config.dsm == parsed_fs_config.dsm,
                pf_profile.filesystem_config.off == parsed_fs_config.off,
            ]):
                logical_match = True
            elif is_hdos_match:
                # HDOS match is simpler - just physical format is enough
                logical_match = True

            if logical_match:
                self.logger.info(f"H17: Matched known profile '{pf_name}'")
                return pf_name, parsed_fs_config

        self.logger.info("H17: Filesystem parsed, but no exact profile match. Using derived parameters.")
        return None, parsed_fs_config

    def _detect_format_by_direct_parse(self) -> Optional[Tuple[Optional[str], Optional[Any]]]:
        """Handles format detection by attempting a direct parse of the boot sector."""
        if not self.disk:
            return None

        geom_for_parse = self.disk.physical_format
        if not geom_for_parse:
            self.logger.debug("Direct parse: No initial geometry, setting temporary default.")
            temp_tf = TrackFormat(0, 79, 0, 1, 18, "MFM", 500, 1, gap3_bytes=84)
            geom_for_parse = PhysicalFormat(80, 2, 300, False, 512, [temp_tf])
            self.set_geometry(geom_for_parse)

        try:
            fs = FATFilesystem(self.disk)
            if fs.get_validity_score() < FATFilesystem.VALIDITY_THRESHOLD:
                return None

            self.logger.info("Direct parse: Successfully validated as FAT filesystem.")
            parsed_bpb = fs.get_specific_config()
            if not isinstance(parsed_bpb, FATVolumeInfo):
                return None

            # Try to find a strong profile match
            for pf_name, pf_profile in self.known_formats.items():
                if (pf_profile.filesystem_type == "FAT12" and
                        isinstance(pf_profile.filesystem_config, FATVolumeInfo) and
                        pf_profile.physical_format and
                        pf_profile.filesystem_config.total_sectors == parsed_bpb.total_sectors and
                        pf_profile.filesystem_config.num_heads == parsed_bpb.num_heads and
                        pf_profile.physical_format.heads == parsed_bpb.num_heads):
                    self.logger.info(f"Direct parse strongly matched FAT profile: '{pf_name}'")
                    self.set_geometry(pf_profile.physical_format)
                    return pf_name, parsed_bpb

            # No strong match, but BPB is valid; refine the geometry from BPB
            self.logger.info("Direct parse valid, refining geometry from BPB.")
            if all(v > 0 for v in [parsed_bpb.bytes_per_sector, parsed_bpb.num_heads, parsed_bpb.sectors_per_track]):
                cyls = parsed_bpb.total_sectors // (parsed_bpb.num_heads * parsed_bpb.sectors_per_track)
                current_pf = self.disk.physical_format
                updated_tf = TrackFormat(
                    track_start=0, track_end=cyls - 1, head_start=0, head_end=parsed_bpb.num_heads - 1,
                    sectors_per_track=parsed_bpb.sectors_per_track,
                    encoding=current_pf.track_formats[0].encoding, rate=current_pf.track_formats[0].rate,
                    interleave=current_pf.track_formats[0].interleave
                )
                refined_pf = PhysicalFormat(
                    cylinders=cyls, heads=parsed_bpb.num_heads, rpm=current_pf.rpm,
                    heads_inverted=current_pf.heads_inverted,
                    bytes_per_sector=parsed_bpb.bytes_per_sector, track_formats=[updated_tf]
                )
                self.set_geometry(refined_pf)
                return None, parsed_bpb
        except Exception as e:
            self.logger.warning(f"Direct FAT parse attempt failed: {e}")

        return None

    def _detect_format_by_profile_iteration(
        self, initial_format: Optional[PhysicalFormat]
    ) -> Optional[Tuple[Optional[str], Optional[Any]]]:
        """Handles format detection by iterating through all known format profiles."""
        if not self.disk or not self.driver:
            return None

        self.logger.debug("Attempting full profile iteration for detection.")
        for profile_name, profile in self.known_formats.items():
            if not profile.physical_format or not profile.filesystem_type:
                continue

            if isinstance(self.driver, IMGImageDriver):
                image_size = len(self.driver.image_data)
                profile_size = profile.physical_format.total_bytes
                if image_size != profile_size:
                    continue

            self.logger.debug(f"Profile iteration: Trying '{profile_name}'")
            self.set_format(profile)

            try:
                fs_class = get_filesystem_class_by_type(profile.filesystem_type)
                if fs_class:
                    fs = fs_class(self.disk)
                    if fs.get_validity_score() >= getattr(fs, 'VALIDITY_THRESHOLD', 30):
                        self.logger.info(f"Profile iteration: Detected format '{profile_name}'")
                        return profile_name, fs.get_specific_config()
            except Exception as e:
                self.logger.debug(f"Profile '{profile_name}' did not match or caused error: {e}")

            # Restore state before trying the next profile
            if initial_format:
                if self.disk.physical_format != initial_format:
                    self.set_geometry(initial_format)
            elif self.disk.physical_format is not None:
                self.disk.physical_format = None
                self.physical_format = None

        return None
