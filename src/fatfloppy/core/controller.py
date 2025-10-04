# src/fatfloppy/core/controller.py
"""
Core controller for managing floppy disk images and physical drives.
"""

import os
import copy
from logging import Logger
from typing import List, Optional, Tuple, Dict, Any

from .utils.logging_config import get_logger
from .disk import Disk
from .driver_factory import DriverFactory
from .drivers import DiskIODriver
from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .filesystems.fs_base import Filesystem
from .filesystem_factory import create_filesystem, get_filesystem_class_by_type

logger: Logger = get_logger()


class DiskController:
    """
    Manages disk operations, acting as a high-level interface for interacting
    with physical floppy disks and disk images.
    """

    def __init__(self) -> None:
        """Initializes the DiskController, setting up its initial state."""
        self.logger: Logger = get_logger(self.__class__.__name__)
        self.disk: Optional[Disk] = None
        self.filesystem: Optional[Filesystem] = None

        from .filesystem_registry import FilesystemRegistry
        self.logger.info("Loading formats from FilesystemRegistry...")
        self.known_formats: Dict[str, FormatProfile] = FilesystemRegistry.get_all_formats()
        self.logger.info(f"Loaded {len(self.known_formats)} formats from registry")
        self.logger.debug(f"Format names: {list(self.known_formats.keys())}")

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
        """
        if self.disk:
            self.close_disk()

        self.explicit_format_set = bool(format_info)

        try:
            self.driver = DriverFactory.create(
                disk_type,
                source=source,
                drive_letter=drive_letter,
                drive_size=drive_size
            )

            is_valid, error = self.driver.validate_for_opening(
                source,
                drive_letter=drive_letter,
                drive_size=drive_size
            )

            if not is_valid:
                self.logger.error(f"Driver validation failed: {error}")
                self.driver = None
                return False

            is_valid, error = self.driver.validate_state_for_opening()
            if not is_valid:
                self.logger.error(f"Driver state validation failed: {error}")
                self.driver = None
                return False

            if self.driver.requires_initialization:
                if hasattr(self.driver, 'initialize'):
                    try:
                        self.driver.initialize()
                    except Exception as e:
                        self.logger.error(f"Driver initialization failed: {e}")
                        self.driver = None
                        return False

            self.disk = Disk(self.driver)
            success = self._handle_format(format_info, drive_size)

            if not success or not self.disk or not self.disk.physical_format:
                requirements = self.driver.get_format_requirements()
                if requirements['needs_format_for_io']:
                    self.logger.error(f"Failed to establish valid format/geometry for {source}")
                    self.close_disk()
                    return False
                else:
                    self.logger.warning(f"No format set for {source}, but driver allows deferred format")

            # Create filesystem, using detected config if available
            self.filesystem = self._create_filesystem_with_config()

            self.physical_format = self.disk.physical_format
            try:
                self.active_filesystem_config = self.filesystem.get_specific_config() if self.filesystem else None
            except (ValueError, IOError) as e:
                # Disk may not be formatted yet - that's okay
                self.logger.debug(f"Could not get filesystem config (disk may be unformatted): {e}")
                self.active_filesystem_config = None

            self.logger.info(f"Disk '{source}' opened successfully. Type: {disk_type}. "
                            f"Final Geometry: {self.physical_format}")
            return True

        except (FileNotFoundError, ValueError, TypeError, Exception) as e:
            self.logger.exception(f"Error opening disk '{source}' (Type: {disk_type}): {e}")
            self.close_disk()
            return False

    def _create_filesystem_with_config(self) -> Optional[Filesystem]:
        """
        Creates a filesystem instance, using stored config if available.

        Returns:
            The created Filesystem instance, or None if creation fails
        """
        if not self.disk:
            return None

        # If we have a stored config from detection, use it
        if self.active_filesystem_config:
            # Determine filesystem type from the config
            from .filesystem_registry import FilesystemRegistry

            config_type = type(self.active_filesystem_config)

            # Find which filesystem handles this config type
            for fs_class in FilesystemRegistry.get_all():
                if (hasattr(fs_class, 'config_class') and
                    fs_class.config_class is not None and
                    fs_class.config_class == config_type):

                    self.logger.info(f"Creating {fs_class.__name__} with detected config")
                    try:
                        return fs_class(self.disk, config=self.active_filesystem_config)
                    except Exception as e:
                        self.logger.warning(f"Failed to create filesystem with config: {e}")
                        # Fall through to auto-detection
                        break

        # Fall back to auto-detection by scoring
        self.logger.debug("No stored config, using filesystem auto-detection")
        return create_filesystem(self.disk)

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

        # Invalidate detection cache
        self._detection_cached = False
        self._cached_format_name = None

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
        """
        if not self.disk:
            self.logger.error("No disk opened to detect geometry")
            return None
        if self.physical_format:
            return self.physical_format
        format_name, fs_config, physical_format = self.detect_format()
        if format_name:
            profile = self.get_format_by_name(format_name)
            if profile and profile.physical_format:
                return profile.physical_format
        return None

    def set_geometry(self, geometry: PhysicalFormat) -> None:
        """
        Explicitly sets the physical geometry for the current disk and driver.
        """
        if not self.disk or not self.driver:
            raise ValueError("No disk opened to set geometry")

        self.disk.set_geometry(geometry)
        self.physical_format = geometry

        if hasattr(self.driver, "set_physical_format"):
            self.driver.set_physical_format(copy.deepcopy(geometry))

        self.logger.debug(f"Geometry set on controller, disk, and driver: {geometry}")

    def detect_format(self) -> Tuple[Optional[str], Optional[Any], Optional[PhysicalFormat]]:
        """
        Attempts to auto-detect the disk's format by analyzing its structure.

        Returns cached results if detection has already been performed on the
        current disk to avoid invalidating the filesystem.

        Returns:
            Tuple of (format_name, filesystem_config, physical_format)
        """
        if not self.disk or not self.driver:
            self.logger.error("No disk opened to detect format")
            return None, None, None

        # Return cached results if we've already detected this disk
        if hasattr(self, '_detection_cached') and self._detection_cached:
            self.logger.debug("Returning cached format detection results")
            return (
                getattr(self, '_cached_format_name', None),
                self.active_filesystem_config,
                self.physical_format
            )

        from .format_detection import create_format_detector

        try:
            detector = create_format_detector(
                self.disk,
                self.driver,
                self.known_formats,
            )
            format_name, fs_config, physical_format = detector.detect()

            if physical_format and physical_format != self.physical_format:
                self.logger.info("Auto-detection yielded new physical format. Applying it.")
                self.set_geometry(physical_format)

            # Store the detected filesystem config for later use
            if fs_config:
                self.active_filesystem_config = fs_config

            # Cache the detection results
            self._cached_format_name = format_name
            self._detection_cached = True

            if format_name:
                self.logger.info(f"Detected format: {format_name}")
            elif fs_config:
                self.logger.info(f"Filesystem detected: {type(fs_config).__name__}")
            else:
                self.logger.warning("No format detected")

            return format_name, fs_config, physical_format

        except Exception as e:
            self.logger.exception(f"Format detection failed: {e}")
            return None, None, None

    def set_format(self, profile: FormatProfile) -> None:
        """
        Applies a specific format profile to the currently open disk.

        Args:
            profile: The FormatProfile to apply

        Raises:
            ValueError: If no disk is open or profile is invalid
        """
        if not self.disk or not self.driver:
            raise ValueError("No disk opened")
        if not profile or not profile.physical_format:
            raise ValueError("Invalid FormatProfile provided")

        self.logger.info(f"Setting format using profile: {profile.name}")
        physical_format_to_set = copy.deepcopy(profile.physical_format)

        # Store filesystem config separately
        if profile.filesystem_config:
            self.active_filesystem_config = profile.filesystem_config

        self.disk.set_geometry(physical_format_to_set)

        if hasattr(self.driver, "set_physical_format"):
            self.driver.set_physical_format(physical_format_to_set)
        else:
            self.logger.warning(f"Driver type {type(self.driver).__name__} does not support set_physical_format.")

        self.physical_format = self.disk.physical_format

    def list_formats(self) -> List[Tuple[str, str]]:
        """
        Returns a list of all known, predefined format profiles.
        """
        formats = [(name, profile.description) for name, profile in self.known_formats.items()]
        self.logger.debug(f"Listed {len(formats)} known formats")
        return formats

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        """
        Retrieves a format profile by its unique name.
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

    def get_file_allocation_units(self, file_path: str) -> Optional[List[int]]:
        """
        Get the allocation units (clusters/sectors/blocks) used by a specific file.

        Args:
            file_path: Full path to the file on the disk

        Returns:
            List of allocation unit numbers, or None if file not found or filesystem doesn't support it
        """
        if not self.filesystem:
            self.logger.warning("No filesystem available to get file allocation units.")
            return None

        try:
            units = self.filesystem.get_file_allocation_units(file_path)
            self.logger.debug(f"File '{file_path}' uses {len(units)} allocation units: {units}")
            return units
        except FileNotFoundError:
            self.logger.warning(f"File not found: {file_path}")
            return None
        except NotImplementedError:
            self.logger.warning(f"Filesystem {type(self.filesystem).__name__} doesn't support get_file_allocation_units")
            return None
        except Exception as e:
            self.logger.error(f"Error getting allocation units for {file_path}: {e}")
            return None

    def get_free_space(self) -> Optional[Tuple[int, int]]:
        """
        Calculates the free space on the disk.
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
            raise
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while writing file {path}: {e}")
            raise

    def create_directory(self, path: str) -> bool:
        """
        Creates a new directory on the disk.
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
            raise
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while creating directory {path}: {e}")
            raise

    def delete_item(self, path: str) -> bool:
        """
        Deletes a file or an empty directory from the disk.
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
            raise
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while deleting {path}: {e}")
            raise

    def delete_item_recursive(self, path: str) -> bool:
        """
        Recursively deletes a file or a directory and its contents.
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
            raise
        except Exception as e:
            self.logger.exception(f"An unexpected error occurred while recursively deleting {path}: {e}")
            raise

    # --- Public Methods: Disk Creation and Formatting ---

    def format_disk_media(self, format_name: str, volume_label: str = "NO NAME",
                         file_path: Optional[str] = None,
                         disk_type: str = "IMG") -> bool:
        """
        Formats disk media using a specified format profile.
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
        """
        if self.disk:
            self.close_disk()

        profile = self._resolve_format_profile(format_name)
        if not profile:
            return False

        try:
            self.driver = DriverFactory.create(disk_type, source=file_path)

            if not self.driver.supports_new_image_creation:
                self.logger.error(f"{disk_type} driver does not support creating new images")
                return False

            if hasattr(self.driver, 'initialize_new_image'):
                self.driver.initialize_new_image(profile.physical_format, profile)

            self.disk = Disk(self.driver)
            self.set_format(profile)
            success = self._execute_format(profile, volume_label)

            if success:
                self.logger.info(
                    f"Successfully created and formatted {disk_type} image '{file_path}'"
                )
            return success

        except Exception as e:
            self.logger.exception(f"Error creating image {file_path}: {e}")
            self.close_disk()

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
            format_name: Name of the format to resolve

        Returns:
            The resolved FormatProfile or None
        """
        profile = self.known_formats.get(format_name)

        if not profile:
            # Try to match current geometry to a known format
            if self.disk and self.disk.physical_format:
                for name, prof in self.known_formats.items():
                    if prof.physical_format == self.disk.physical_format:
                        profile = prof
                        self.logger.info(f"Using format profile '{name}' matching current geometry")
                        break

            # Last resort: try to build from driver's physical format
            if not profile and self.driver and hasattr(self.driver, 'physical_format') and self.driver.physical_format:
                # Build a minimal profile from driver's physical format
                # Note: No filesystem config since we don't have one
                profile = FormatProfile(
                    name="custom_runtime",
                    description="Custom (Runtime)",
                    physical_format=self.driver.physical_format,
                    filesystem_config=None  # Will be determined later
                )

            if not profile:
                self.logger.error(f"Cannot resolve format profile '{format_name}'")
                return None

        if not profile.physical_format:
            self.logger.error(f"Format profile '{format_name}' has no physical format")
            return None

        return profile

    def _execute_format(self, profile: FormatProfile, volume_label: str) -> bool:
        """
        Executes the actual formatting operation.

        Args:
            profile: The FormatProfile to use for formatting
            volume_label: Volume label to apply

        Returns:
            True if formatting succeeded, False otherwise
        """
        # Get filesystem class by looking up the profile's config type
        fs_type = profile.get_filesystem_type()
        if not fs_type:
            self.logger.error("Cannot determine filesystem type from profile")
            return False

        from .filesystem_factory import get_filesystem_class_by_type
        fs_class = get_filesystem_class_by_type(fs_type)
        if not fs_class:
            self.logger.error(f"No filesystem handler for type '{fs_type}'")
            return False

        self.logger.info(f"Formatting with profile: {profile.name} (filesystem: {fs_type})")

        try:
            # Set geometry if needed
            if self.disk.physical_format != profile.physical_format:
                self.set_format(profile)
            elif (hasattr(self.driver, "physical_format") and
                self.driver.physical_format != profile.physical_format):
                self.set_format(profile)

            # Create filesystem instance and format
            filesystem_handler = fs_class(self.disk)
            filesystem_handler.format_fs(profile, volume_label=volume_label)

            # Store results
            self.filesystem = filesystem_handler
            self.physical_format = self.disk.physical_format
            self.active_filesystem_config = profile.filesystem_config

            self.flush()
            final_vol_label = self.filesystem.get_volume_label() or volume_label

            self.logger.info(f"Format complete for profile '{profile.name}'. Volume: '{final_vol_label}'")
            return True

        except Exception as e:
            self.logger.exception(f"Error during format with profile '{profile.name}': {e}")
            self.filesystem = None
            self.active_filesystem_config = None
            return False

    def create_custom_profile(self, format_info: Dict[str, Any]) -> Optional[FormatProfile]:
        """
        Creates a new FormatProfile dynamically from a dictionary of parameters.
        """
        try:
            target_fs_type = format_info.get("filesystem_type")
            if not target_fs_type:
                raise ValueError("The 'filesystem_type' key is required for creating a custom profile.")

            physical_format = self._create_physical_format(format_info)
            fs_class = get_filesystem_class_by_type(target_fs_type)
            if not fs_class:
                self.logger.error(f"Unknown filesystem type: {target_fs_type}")
                return None

            filesystem_config_obj = fs_class.create_config_from_params(
                format_info, physical_format
            )
            if not filesystem_config_obj:
                self.logger.warning(f"Could not create filesystem config for '{target_fs_type}'")

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

        except (ValueError, KeyError) as e:
            self.logger.error(f"Error creating custom profile: {e}", exc_info=True)
            return None

    # --- Private Methods: Driver and Format Creation ---

    def _create_physical_format(self, format_info: Dict[str, any], base_profile: Optional[FormatProfile] = None) -> PhysicalFormat:
        """
        Constructs a PhysicalFormat object from a dictionary of parameters.
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

    def _apply_user_format(self, format_info: Dict[str, Any]) -> None:
        """
        Resolves format info and applies it to the current disk and driver.
        Now cleanly separates physical format from filesystem config.

        Args:
            format_info: Dictionary containing format parameters

        Raises:
            ValueError: If format cannot be resolved or applied
        """
        self.explicit_format_set = True

        physical_format = None
        filesystem_config = None

        # 1. Resolve from format_name if provided
        if "format_name" in format_info:
            profile = self.get_format_by_name(format_info["format_name"])
            if profile:
                physical_format = profile.physical_format
                filesystem_config = profile.filesystem_config
                self.logger.debug(f"Resolved format from profile: {format_info['format_name']}")

        # 2. Build custom geometry if params provided
        if not physical_format and any(k in format_info for k in ['cylinders', 'heads', 'sectors_per_track']):
            physical_format = self._create_physical_format(format_info)
            self.logger.debug("Created custom physical format from parameters")

            # Try to create filesystem config if type specified
            if 'filesystem_type' in format_info:
                from .filesystem_factory import get_filesystem_class_by_type
                fs_class = get_filesystem_class_by_type(format_info['filesystem_type'])
                if fs_class and hasattr(fs_class, 'create_config_from_params'):
                    filesystem_config = fs_class.create_config_from_params(format_info, physical_format)
                    self.logger.debug(f"Created {format_info['filesystem_type']} config from parameters")

        if not physical_format:
            raise ValueError("Could not resolve a valid physical format from the provided format_info.")

        # 3. Validate driver compatibility (simplified)
        if self.driver.driver_category == "raw" and not physical_format:
            raise ValueError("Raw image drivers require explicit format information.")

        # 4. Apply the format
        try:
            self.set_geometry(physical_format)

            # Store filesystem config separately (don't attach to physical_format)
            if filesystem_config:
                self.active_filesystem_config = filesystem_config
                self.logger.debug(f"Stored filesystem config: {type(filesystem_config).__name__}")

            self.logger.info("User-defined format applied successfully.")
        except Exception as e:
            self.logger.error(f"Error applying user format: {e}")
            raise ValueError(f"Failed to apply format: {e}") from e

    def _handle_format(self, format_info: Optional[Dict[str, Any]], drive_size: str) -> bool:
        """
        Coordinates the format handling process when a disk is opened.

        Args:
            format_info: Optional format information dictionary.
            drive_size: The drive size hint (e.g., "3.5", "5.25").

        Returns:
            True if format handling succeeded, False otherwise.
        """
        self._drive_size_hint = drive_size
        requirements = self.driver.get_format_requirements()

        # Handle metadata-based drivers (IMD, H17)
        if self.driver.driver_category == "metadata_based":
            return self._handle_metadata_based_format(format_info)

        # Handle user-provided format info
        if format_info:
            return self._handle_user_format(format_info)

        # Try auto-detection if driver supports it
        if requirements['can_derive_format']:
            if self._try_auto_detection():
                return True

        # Handle raw drivers (must have format)
        if self.driver.driver_category == "raw":
            return self._handle_raw_driver_format()

        # Physical drivers can defer format detection
        if self.driver.driver_category == "physical":
            self.logger.info("Physical drive opened without explicit format, "
                            "will use track scanning for geometry detection")
            return True

        self.logger.error(f"Could not establish format for {self.driver.driver_category} driver")
        return False

    def _handle_metadata_based_format(self, format_info: Optional[Dict[str, Any]]) -> bool:
        """Handles format for metadata-based drivers (IMD, H17)."""
        if not self.driver.physical_format:
            self.logger.error(f"{self.driver.__class__.__name__} has no physical format after loading")
            return False

        self.disk.set_geometry(self.driver.physical_format)
        self.physical_format = self.driver.physical_format

        if format_info:
            self.logger.warning(f"Applying user format to {self.driver.__class__.__name__} "
                                "will override embedded metadata")
            try:
                self._apply_user_format(format_info)
            except Exception as e:
                self.logger.error(f"Failed to apply user format override: {e}")
                return False

        return True

    def _handle_user_format(self, format_info: Dict[str, Any]) -> bool:
        """Handles user-provided format information."""
        try:
            self._apply_user_format(format_info)
            self.logger.debug(f"Applied user format. Geometry: {self.disk.physical_format}")
            return True
        except Exception as e:
            self.logger.error(f"Failed applying user format: {e}")
            return False

    def _try_auto_detection(self) -> bool:
        """
        Attempts auto-detection of disk format.

        Returns:
            True if auto-detection succeeded and set a valid format.
        """
        self.logger.debug(f"Attempting auto-detection for {self.driver.__class__.__name__}")
        try:
            format_name, fs_config, physical_format = self.detect_format()

            if format_name:
                self.logger.info(f"Auto-detection successful: format='{format_name}'")
                return True

            if fs_config:
                self.logger.info(f"Auto-detection found filesystem config: {type(fs_config).__name__}")
                return True

            if self.disk.physical_format:
                self.logger.info("Auto-detection set geometry without format name")
                return True

            self.logger.warning(f"Auto-detection returned no results for {self.driver.__class__.__name__}")
            return False

        except Exception as e:
            self.logger.error(f"Auto-detection raised exception: {e}", exc_info=True)
            return False

    def _handle_raw_driver_format(self) -> bool:
        """
        Handles format requirements for raw image drivers.

        Raw drivers require explicit format information since they have no metadata.
        """
        if not self.disk.physical_format:
            img_data_len = len(self.driver.image_data) if hasattr(self.driver, 'image_data') else 'unknown'
            self.logger.error(
                f"Raw image driver requires format information. "
                f"Auto-detection failed and no explicit format provided. "
                f"Image size: {img_data_len} bytes"
            )
            return False

        self.logger.info(f"Raw driver has geometry set: {self.disk.physical_format}")
        return True
