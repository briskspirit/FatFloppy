import datetime
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat
from ..utils.logging_config import get_logger


@dataclass
class FileInfo:
    """
    A data structure to hold metadata about a file or directory.

    This dataclass encapsulates all relevant metadata for filesystem entries,
    providing a consistent interface across different filesystem implementations.

    Attributes:
        name: The filename or directory name.
        size: The size of the file in bytes (0 for directories).
        is_dir: True if this entry represents a directory, False for files.
        datetime: The timestamp associated with the file or directory.
        attributes: String representation of file attributes.
        starting_cluster: The starting allocation unit (cluster, block, etc.).
        extra_data: Filesystem-specific additional data.
    """

    name: str
    size: int
    is_dir: bool
    datetime: datetime.datetime
    attributes: str
    starting_cluster: int = 0
    extra_data: Optional[Any] = None


class Filesystem(ABC):
    """
    Abstract base class for all filesystem implementations.

    This class defines a standard interface for interacting with various
    filesystems. Subclasses must implement all abstract methods to provide
    concrete functionality for a specific filesystem type.
    """

    filesystem_type: ClassVar[str] = ""
    filesystem_aliases: ClassVar[list[str]] = []
    validity_threshold: ClassVar[int] = 30
    config_class: ClassVar[Optional[type]] = None
    min_fatfloppy_version: ClassVar[Optional[str]] = None

    def __init__(self, disk: Disk, config: Optional[Any] = None):
        """
        Initializes the Filesystem base class.

        Args:
            disk: The Disk object that this filesystem will operate on.
            config: Optional filesystem-specific configuration object. The
                controller constructs filesystems as ``fs_class(disk, config=...)``,
                so the base contract accepts it; subclasses may interpret it.

        Raises:
            ValueError: If the subclass does not define filesystem_type.
        """
        if not self.filesystem_type:
            raise ValueError(f"{self.__class__.__name__} must define filesystem_type")
        self.logger = get_logger(self.__class__.__name__)
        self.disk = disk
        self.config = config

    @classmethod
    def get_format_definitions(cls) -> dict[str, "FormatProfile"]:
        """
        Returns all format definitions provided by this filesystem plugin.

        Subclasses should override this to provide their format definitions.

        Returns:
            Dictionary mapping format names to FormatProfile objects.
        """
        return {}

    @staticmethod
    @abstractmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        """
        Compares two filesystem-specific configuration objects for equality.

        This is used by the format detector to match profiles.

        Args:
            config1: The first configuration object.
            config2: The second configuration object.

        Returns:
            True if the configurations are considered a match, False otherwise.
        """
        raise NotImplementedError

    @staticmethod
    def create_config_from_params(
        _format_info: dict[str, Any], _physical_format: PhysicalFormat
    ) -> Optional[Any]:
        """
        Creates a filesystem-specific configuration object from parameters.

        Subclasses should override this to provide their config creation logic.

        Args:
            _format_info: Dictionary containing filesystem parameters.
            _physical_format: The physical format of the disk.

        Returns:
            A filesystem-specific config object, or None if not supported.
        """
        return None

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
    def format_fs(
        self, profile: FormatProfile, volume_label: Optional[str] = None
    ) -> None:
        """
        Formats the disk with this filesystem type according to a given profile.

        Args:
            profile: The FormatProfile containing geometry and filesystem parameters.
            volume_label: An optional label for the volume.
        """
        raise NotImplementedError

    @abstractmethod
    def get_allocated_units(self) -> list[int]:
        """
        Gets a list of all allocated unit numbers (e.g., clusters, blocks).

        Returns:
            A sorted list of integers representing the allocated units.
        """
        raise NotImplementedError

    @abstractmethod
    def get_disk_map_layout(self) -> dict[str, Any]:
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
    def get_display_info(self) -> dict[str, str]:
        """
        Returns a dictionary of filesystem-specific parameters for display.

        Returns:
            A dictionary where keys are parameter names and values are their
            string representations.
        """
        raise NotImplementedError

    @abstractmethod
    def get_file_allocation_units(self, path: str) -> list[int]:
        """
        Gets the allocation units (clusters, blocks, groups) used by a specific file.

        Args:
            path: The full path to the file.

        Returns:
            A list of allocation unit numbers used by the file, in order.
            Returns an empty list if the file doesn't exist or has no allocated units.
        """
        raise NotImplementedError

    @abstractmethod
    def get_free_space(self) -> tuple[int, int]:
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
    def list_directory(self, path: str) -> list[FileInfo]:
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

    # -- name policy (overridable; defaults match classic 8.3 filesystems) -------

    def suggest_import_name(
        self, host_name: str, existing_names, is_dir: bool = False
    ) -> str:
        """
        Derives a valid, unique on-disk name from a host filename.

        Never raises for any host string. Uniqueness against ``existing_names``
        is case-insensitive (all shipped filesystems store uppercase-canonical
        names). The default implements classic 8.3 rules; filesystems with
        different conventions override this.

        Args:
            host_name: The filename from the host filesystem.
            existing_names: Iterable of names already present in the target
                directory (compared case-insensitively).
            is_dir: True when importing a directory entry (no extension).

        Returns:
            A valid, unique on-disk name string.

        Raises:
            ValueError: If a unique name cannot be generated after 999 attempts.
        """
        existing = {n.upper() for n in existing_names}
        name = host_name.upper()
        name = re.sub(r'[\\/:*?"<>|\s+]', "_", name)
        if "." in name.strip(".") and not is_dir:
            base, _, ext = name.rpartition(".")
            base = base.replace(".", "_").strip()[:8]
            ext = ext.strip(".")[:3]
        else:
            base, ext = name.replace(".", "_")[:8], ""
        if not base.strip("_"):
            base = "_FILE"
        candidate = f"{base}.{ext}" if ext else base
        if candidate.upper() not in existing:
            return candidate
        stem = base[:6]
        for counter in range(1, 1000):
            new_base = f"{stem}~{counter:02d}"[:8]
            candidate = f"{new_base}.{ext}" if ext else new_base
            if candidate.upper() not in existing:
                return candidate
        raise ValueError(f"Cannot generate unique name for {host_name!r}")

    def suggest_host_name(self, name: str) -> str:
        """
        Makes an on-disk name safe as a host filename.

        Path separators, Windows-illegal characters and control bytes become
        '_'; leading/trailing spaces and dots are stripped; an all-separator
        result is replaced with ``_unnamed_``.

        Args:
            name: The raw on-disk filename.

        Returns:
            A host-safe filename string, never empty.
        """
        safe = "".join("_" if (c in '/\\:*?"<>|' or ord(c) < 0x20) else c for c in name)
        safe = safe.strip(" .")
        return safe if safe.strip("_ ") else "_unnamed_"

    def name_hint(self) -> str:
        """Short description of this filesystem's naming rules, for dialogs."""
        return "8.3 format"
