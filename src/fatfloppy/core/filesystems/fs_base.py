# src/fatfloppy/core/filesystems/fs_base.py
"""
Defines the abstract base classes for filesystem implementations.

This module provides the core interface that all concrete filesystem handlers
(like FAT12, CP/M, etc.) must implement. It ensures a consistent API for
interacting with different types of filesystems on disk images.

Classes:
    FileInfo: A dataclass for holding metadata about a single file or directory.
    Filesystem: An abstract base class defining the required methods for a
                filesystem implementation.
"""
import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any, Callable, ClassVar

from ..disk import Disk
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger
from ..physical_format import PhysicalFormat


@dataclass
class FileInfo:
    """
    A data structure to hold metadata about a file or directory.
    """
    name: str
    size: int
    is_dir: bool
    datetime: datetime.datetime
    attributes: str
    starting_cluster: int = 0  # Represents the starting allocation unit (cluster, block, etc.)
    extra_data: Optional[Any] = None  # For filesystem-specific additional data


class Filesystem(ABC):
    """
    Abstract base class for all filesystem implementations.

    This class defines a standard interface for interacting with various
    filesystems. Subclasses must implement all abstract methods to provide
    concrete functionality for a specific filesystem type.
    """
    # Plugin metadata (must be set by subclasses)
    filesystem_type: ClassVar[str] = ""  # e.g., "FAT12", "CPM"
    filesystem_aliases: ClassVar[List[str]] = []  # e.g., ["FAT", "MSDOS"]
    validity_threshold: ClassVar[int] = 30

    # Optional: Minimum version required
    min_fatfloppy_version: ClassVar[Optional[str]] = None

    def __init__(self, disk: Disk):
        """
        Initializes the Filesystem base class.

        Args:
            disk: The Disk object that this filesystem will operate on.
        """
        if not self.filesystem_type:
            raise ValueError(f"{self.__class__.__name__} must define filesystem_type")
        self.logger = get_logger(self.__class__.__name__)
        self.disk = disk

    @abstractmethod
    def create_directory(self, path: str) -> None:
        """
        Creates a new directory at the specified path.

        Args:
            path: The full path of the new directory.
        """
        raise NotImplementedError

    @abstractmethod
    def delete(self, path: str) -> None:
        """
        Deletes a file or an empty directory.

        Args:
            path: The full path of the file or directory to delete.
        """
        raise NotImplementedError

    @abstractmethod
    def delete_recursive(self, path: str) -> bool:
        """
        Deletes a file or a directory and all its contents recursively.

        Args:
            path: The full path of the item to delete.

        Returns:
            True if deletion was successful, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None:
        """
        Formats the disk with this filesystem type according to a given profile.

        Args:
            profile: The FormatProfile containing geometry and filesystem parameters.
            volume_label: An optional label for the volume.
        """
        raise NotImplementedError

    @abstractmethod
    def get_allocated_units(self) -> List[int]:
        """
        Gets a list of all allocated unit numbers (e.g., clusters, blocks).

        Returns:
            A sorted list of integers representing the allocated units.
        """
        raise NotImplementedError

    @abstractmethod
    def get_disk_map_layout(self) -> Dict[str, Any]:
        """
        Returns layout information for visualizing the disk map.

        Expected return dictionary keys:
        - 'legend': List[Tuple[str, str]] (e.g., [("Boot Sector", "#FF0000")])
        - 'get_sector_type': Callable[[int], str] (takes LBA, returns type string)
        - 'allocation_unit_size_sectors': int (sectors per cluster/block)
        - 'first_data_sector': int (LBA of the first data sector)
        - 'type_color_map': (Optional) Dict[str, str] (maps type string to color)

        Returns:
            A dictionary containing the layout information.
        """
        raise NotImplementedError

    @abstractmethod
    def get_display_info(self) -> Dict[str, str]:
        """
        Returns a dictionary of filesystem-specific parameters for display.

        Returns:
            A dictionary where keys are parameter names and values are their
            string representations.
        """
        raise NotImplementedError

    @abstractmethod
    def get_free_space(self) -> Tuple[int, int]:
        """
        Returns the free and total available space in the data area.

        Returns:
            A tuple containing (free_bytes, total_bytes).
        """
        raise NotImplementedError

    @abstractmethod
    def get_specific_config(self) -> Optional[Any]:
        """
        Returns the filesystem-specific configuration object.

        For example, a FAT BPB (FATVolumeInfo) or a CP/M DPB.

        Returns:
            The configuration object, or None if not available.
        """
        raise NotImplementedError

    @abstractmethod
    def get_validity_score(self) -> int:
        """
        Checks the filesystem structure and returns a confidence score.

        The score indicates the likelihood that the disk is formatted with this
        filesystem type.

        Returns:
            An integer score from 0 (not this filesystem) to 100 (perfect match).
        """
        raise NotImplementedError

    @abstractmethod
    def list_directory(self, path: str) -> List[FileInfo]:
        """
        Lists the contents of a specified directory.

        Args:
            path: The path of the directory to list.

        Returns:
            A list of FileInfo objects representing the directory's contents.
        """
        raise NotImplementedError

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """
        Reads the full contents of a specified file.

        Args:
            path: The path of the file to read.

        Returns:
            A bytes object containing the file's data.
        """
        raise NotImplementedError

    @abstractmethod
    def write_file(self, path: str, data: bytes) -> None:
        """
        Writes data to a file, creating or overwriting it.

        Args:
            path: The path of the file to write to.
            data: The binary data to be written.
        """
        raise NotImplementedError

    def check(self) -> bool:
        """
        Performs a basic consistency check on the filesystem.

        This is an optional implementation. Subclasses can override it to provide
        more specific checks (e.g., checking for cross-linked files).

        Returns:
            True if the filesystem appears consistent, False otherwise.
        """
        self.logger.warning("Filesystem check not implemented for this type.")
        return True

    def get_volume_label(self) -> Optional[str]:
        """
        Returns the volume label for this filesystem, if supported.

        This is an optional method that filesystems can override to provide
        volume label information. The base implementation returns None.

        Returns:
            The volume label as a string, or None if not supported or not available.
        """
        return None

    @staticmethod
    def create_config_from_params(format_info: Dict[str, Any],
                                   physical_format: PhysicalFormat) -> Optional[Any]:
        """
        Creates a filesystem-specific configuration object from parameters.

        Subclasses should override this to provide their config creation logic.

        Args:
            format_info: Dictionary containing filesystem parameters.
            physical_format: The physical format of the disk.

        Returns:
            A filesystem-specific config object, or None if not supported.
        """
        return None
