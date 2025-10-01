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

            # Validate that the driver can open this source
            is_valid, error = self.driver.validate_for_opening(
                source,
                drive_letter=drive_letter,
                drive_size=drive_size
            )

            if not is_valid:
                self.logger.error(f"Driver validation failed: {error}")
                self.driver = None
                return False

            # Validate driver state after creation
            is_valid, error = self.driver.validate_state_for_opening()
            if not is_valid:
                self.logger.error(f"Driver state validation failed: {error}")
                self.driver = None
                return False

            # Initialize driver if needed (e.g., Greaseweazle)
            if self.driver.requires_initialization:
                if hasattr(self.driver, 'initialize'):
                    try:
                        self.driver.initialize()
                    except Exception as e:
                        self.logger.error(f"Driver initialization failed: {e}")
                        self.driver = None
                        return False

            # Create disk object
            self.disk = Disk(self.driver)

            # Handle format detection/application
            success = self._handle_format(format_info, drive_size)

            if not success or not self.disk or not self.disk.physical_format:
                # Check if driver allows deferred format
                requirements = self.driver.get_format_requirements()

                if requirements['needs_format_for_io']:
                    self.logger.error(f"Failed to establish valid format/geometry for {source}")
                    self.close_disk()
                    return False
                else:
                    self.logger.warning(f"No format set for {source}, but driver allows deferred format")

            # Create filesystem
            self.filesystem = create_filesystem(self.disk)

            # Let filesystem adjust geometry if needed
            if self.filesystem and hasattr(self.filesystem, '_check_and_adjust_geometry'):
                self.logger.debug(f"Pre-adjustment disk geometry: {self.disk.physical_format}")
                self.filesystem._check_and_adjust_geometry(self.driver, self.explicit_format_set)
                self.logger.debug(f"Post-adjustment disk geometry: {self.disk.physical_format}")

            # Update controller state
            self.physical_format = self.disk.physical_format
            self.active_filesystem_config = self.filesystem.get_specific_config() if self.filesystem else None

            self.logger.info(f"Disk '{source}' opened successfully. Type: {disk_type}. "
                            f"Final Geometry: {self.physical_format}")
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

    def format_disk_media(self, format_name: str, volume_label: str = "NO NAME",
                         file_path: Optional[str] = None,
                         disk_type: str = "IMG") -> bool:
        """
        Formats disk media using a specified format profile.

        This method handles both formatting an already-open disk and creating
        a new disk image file. The operation is determined by whether a disk
        is currently open.

        Args:
            format_name: The name of the format profile to use.
            volume_label: The volume label to assign.
            file_path: Path for new image (required if no disk is open).
            disk_type: Type of image to create if creating new ("IMG", "IMD", "H17").

        Returns:
            True if formatting was successful, False otherwise.
        """
        creating_new_image = (self.disk is None)

        if creating_new_image:
            if not file_path:
                self.logger.error("file_path required when creating new image")
                return False
            return self._format_new_image(file_path, format_name, volume_label, disk_type)
        else:
            return self._format_existing_disk(format_name, volume_label)

    def _format_existing_disk(self, format_name: str, volume_label: str) -> bool:
        """
        Formats an already-open disk.

        Args:
            format_name: The name of the format profile to use.
            volume_label: The volume label to assign.

        Returns:
            True if successful, False otherwise.
        """
        if not self.driver.supports_in_place_formatting:
            self.logger.error(
                f"{self.driver.__class__.__name__} does not support "
                "in-place formatting. Close disk and use format_disk_media with file_path."
            )
            return False

        if not self.disk or not self.driver:
            self.logger.error("No disk opened to format")
            return False

        profile = self._resolve_format_profile(format_name)
        if not profile:
            return False

        return self._execute_format(profile, volume_label)

    def _format_new_image(self, file_path: str, format_name: str,
                         volume_label: str, disk_type: str) -> bool:
        """
        Creates and formats a new disk image.

        Args:
            file_path: Path where the new image will be created.
            format_name: The name of the format profile to use.
            volume_label: The volume label to assign.
            disk_type: Type of image to create ("IMG", "IMD", "H17").

        Returns:
            True if successful, False otherwise.
        """
        if self.disk:
            self.close_disk()

        profile = self._resolve_format_profile(format_name)
        if not profile:
            return False

        try:
            # Create the driver
            self.driver = DriverFactory.create(disk_type, source=file_path)

            if not self.driver.supports_new_image_creation:
                self.logger.error(f"{disk_type} driver does not support creating new images")
                return False

            # Let driver initialize its image structure
            self.driver.initialize_new_image(profile.physical_format, profile)

            # Create disk object
            self.disk = Disk(self.driver)

            # Set format
            self.set_format(profile)

            # Execute the format
            success = self._execute_format(profile, volume_label)

            if success:
                self.logger.info(
                    f"Successfully created and formatted {disk_type} image '{file_path}'"
                )

            return success

        except Exception as e:
            self.logger.exception(f"Error creating image {file_path}: {e}")
            self.close_disk()

            # Clean up partially created file
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as rm_e:
                    self.logger.warning(f"Could not remove partial file '{file_path}': {rm_e}")

            return False

    def _resolve_format_profile(self, format_name: str) -> Optional[FormatProfile]:
        """
        Resolves a format name to a FormatProfile.

        Args:
            format_name: Name of the format profile.

        Returns:
            The FormatProfile, or None if not found/invalid.
        """
        profile = self.known_formats.get(format_name)

        if not profile:
            # Try to find one matching current geometry
            if self.disk and self.disk.physical_format:
                for name, prof in self.known_formats.items():
                    if prof.physical_format == self.disk.physical_format:
                        profile = prof
                        self.logger.info(f"Using format profile '{name}' matching current geometry")
                        break

            # Try to construct from driver's current format
            if (not profile and self.driver and
                hasattr(self.driver, 'physical_format') and self.driver.physical_format):

                fs_cfg = getattr(self.driver.physical_format, '_associated_filesystem_config', None)
                if fs_cfg and hasattr(fs_cfg, 'filesystem_type'):
                    profile = FormatProfile(
                        name="custom_runtime",
                        description="Custom (Runtime)",
                        physical_format=self.driver.physical_format,
                        filesystem_type=fs_cfg.filesystem_type,
                        filesystem_config=fs_cfg
                    )

            if not profile:
                self.logger.error(f"Cannot resolve format profile '{format_name}'")
                return None

        # Validate profile
        if not profile.physical_format or not profile.filesystem_config:
            self.logger.error(f"Format profile '{format_name}' is incomplete")
            return None

        return profile

    def _execute_format(self, profile: FormatProfile, volume_label: str) -> bool:
        """
        Executes the actual formatting operation.

        Args:
            profile: The FormatProfile to use.
            volume_label: The volume label to assign.

        Returns:
            True if successful, False otherwise.
        """
        # Get filesystem class
        fs_class = get_filesystem_class_by_type(profile.filesystem_type)
        if not fs_class:
            self.logger.error(
                f"No filesystem handler for type '{profile.filesystem_type}'"
            )
            return False

        self.logger.info(f"Formatting with profile: {profile.name}")

        try:
            # Ensure geometry matches profile
            if self.disk.physical_format != profile.physical_format:
                self.set_format(profile)
            elif (hasattr(self.driver, "physical_format") and
                  self.driver.physical_format != profile.physical_format):
                self.set_format(profile)

            # Attach filesystem config
            if (not hasattr(self.disk.physical_format, '_associated_filesystem_config') and
                profile.filesystem_config):
                setattr(self.disk.physical_format, '_associated_filesystem_config',
                       profile.filesystem_config)

            # Create filesystem handler and format
            filesystem_handler = fs_class(self.disk)
            filesystem_handler.format_fs(profile, volume_label=volume_label)

            # Update controller state
            self.filesystem = filesystem_handler
            self.physical_format = self.disk.physical_format
            self.active_filesystem_config = (
                self.filesystem.get_specific_config() if self.filesystem else None
            )

            # Flush changes
            self.flush()

            # Get final volume label
            final_vol_label = self.filesystem.get_volume_label() or volume_label

            self.logger.info(
                f"Format complete for profile '{profile.name}'. Volume: '{final_vol_label}'"
            )
            return True

        except Exception as e:
            self.logger.exception(f"Error during format with profile '{profile.name}': {e}")
            self.filesystem = None
            self.active_filesystem_config = None
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

            # Get the filesystem class to create its config
            fs_class = get_filesystem_class_by_type(target_fs_type)
            if not fs_class:
                self.logger.error(f"Unknown filesystem type: {target_fs_type}")
                return None

            # Let the filesystem create its own config
            filesystem_config_obj = fs_class.create_config_from_params(
                format_info, physical_format
            )

            if not filesystem_config_obj:
                self.logger.warning(
                    f"Could not create filesystem config for '{target_fs_type}'"
                )

            profile_name = format_info.get("profile_name", "custom")
            profile_description = format_info.get(
                "description",
                f"Custom {physical_format.cylinders}x{physical_format.heads}x"
                f"{physical_format.track_formats[0].sectors_per_track}x"
                f"{physical_format.bytes_per_sector} ({target_fs_type})"
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

        Raises:
            ValueError: If format application fails.
        """
        self.explicit_format_set = True

        from .format_application import create_format_applier

        try:
            # Create appropriate applier for this driver
            applier = create_format_applier(self.driver, self.disk)

            # Apply the format
            success, error = applier.apply_format(format_info)

            if not success:
                raise ValueError(f"Format application failed: {error}")

            if error:  # Success but with warnings
                self.logger.warning(f"Format applied with warnings: {error}")

            # Update controller state
            self.physical_format = self.disk.physical_format

            self.logger.info("User format applied successfully")

        except Exception as e:
            self.logger.error(f"Error applying user format: {e}")
            raise ValueError(f"Failed to apply format: {e}") from e

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

        # Check what the driver needs
        requirements = self.driver.get_format_requirements()

        # Metadata-based drivers (IMD, H17) derive format from file
        if self.driver.driver_category == "metadata_based":
            if not self.driver.physical_format:
                self.logger.error(f"{self.driver.__class__.__name__} has no physical format after loading")
                return False

            self.disk.set_geometry(self.driver.physical_format)
            self.physical_format = self.driver.physical_format

            # User format can override if explicitly provided
            if format_info:
                self.logger.warning(f"Applying user format to {self.driver.__class__.__name__} "
                                "will override embedded metadata")
                try:
                    self._apply_user_format(format_info)
                except Exception as e:
                    self.logger.error(f"Failed to apply user format override: {e}")
                    return False

            return True

        # User-specified format takes precedence
        if format_info:
            try:
                self._apply_user_format(format_info)
                self.logger.debug(f"Applied user format. Geometry: {self.disk.physical_format}")
                return True
            except Exception as e:
                self.logger.error(f"Failed applying user format: {e}")
                return False

        # Auto-detection for drivers that support it
        if requirements['can_derive_format']:
            self.logger.debug(f"Attempting auto-detection for {self.driver.__class__.__name__}")

            try:
                format_name, fs_config = self.detect_format()

                # Check if detection succeeded
                if format_name:
                    self.logger.info(f"Auto-detection successful: format='{format_name}'")
                    return True

                if fs_config:
                    self.logger.info(f"Auto-detection found filesystem config: {type(fs_config).__name__}")
                    return True

                # Check if geometry was set even without explicit format name
                if self.disk.physical_format:
                    self.logger.info("Auto-detection set geometry without format name")
                    return True

                # Detection completely failed
                self.logger.warning(f"Auto-detection returned no results for {self.driver.__class__.__name__}")

            except Exception as e:
                self.logger.error(f"Auto-detection raised exception: {e}", exc_info=True)

        # For raw drivers, we must have some geometry at this point
        if self.driver.driver_category == "raw":
            if not self.disk.physical_format:
                self.logger.error(
                    f"Raw image driver requires format information. "
                    f"Auto-detection failed and no explicit format provided. "
                    f"Image size: {len(self.driver.image_data) if hasattr(self.driver, 'image_data') else 'unknown'} bytes"
                )
                return False
            else:
                # We have geometry, even though detection didn't return a name
                self.logger.info(f"Raw driver has geometry set: {self.disk.physical_format}")
                return True

        # Physical drives can operate without explicit format (will scan tracks)
        if self.driver.driver_category == "physical":
            self.logger.info("Physical drive opened without explicit format, "
                            "will use track scanning for geometry detection")
            return True

        # Unknown situation
        self.logger.error(f"Could not establish format for {self.driver.driver_category} driver")
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
        # First check filesystem config for head count
        if self.filesystem and (specific_config := self.filesystem.get_specific_config()):
            if hasattr(specific_config, 'num_heads') and isinstance(specific_config.num_heads, int):
                if specific_config.num_heads > 0:
                    return specific_config.num_heads > 1

        # Physical test
        current_physical_format_before_check = copy.deepcopy(self.disk.physical_format)
        self.set_format(temp_profile)
        has_second_head_result = False

        try:
            # Try to read from head 1
            # Use capability check instead of isinstance
            if self.driver.driver_category == "physical" and hasattr(self.driver, '_read_track'):
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
