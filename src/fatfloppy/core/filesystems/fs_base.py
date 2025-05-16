# src/fatfloppy/core/filesystems/fs_base.py
import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any

from ..disk import Disk
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger


@dataclass
class FileInfo:
    name: str
    size: int
    is_dir: bool
    datetime: datetime.datetime
    attributes: str
    starting_cluster: int = 0 # TODO: rename to starting_allocation_unit


class Filesystem(ABC):
    def __init__(self, disk: Disk):
        self.logger = get_logger(self.__class__.__name__)
        self.disk = disk

    @abstractmethod
    def is_valid(self) -> bool:
        """Check if the filesystem structure is valid on the disk."""
        raise NotImplementedError

    @abstractmethod
    def list_directory(self, path: str) -> List[FileInfo]:
        """List contents of a directory."""
        raise NotImplementedError

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read the contents of a file."""
        raise NotImplementedError

    @abstractmethod
    def write_file(self, path: str, data: bytes) -> None:
        """Write data to a file, overwriting if it exists."""
        raise NotImplementedError

    @abstractmethod
    def create_directory(self, path: str) -> None:
        """Create a new directory."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete a file or empty directory."""
        raise NotImplementedError

    @abstractmethod
    def delete_recursive(self, path: str) -> bool:
        """Delete a file or directory recursively (default implementation)."""
        raise NotImplementedError

    @abstractmethod
    def get_allocated_units(self) -> List[int]:
        """Get a list of allocated allocation unit numbers (e.g., clusters)."""
        raise NotImplementedError

    @abstractmethod
    def get_free_space(self) -> Tuple[int, int]:
        """Return free bytes and total bytes available in the data area."""
        raise NotImplementedError

    @abstractmethod
    def format_fs(self, profile: FormatProfile, volume_label: Optional[str] = None) -> None:
        """Format the disk according to the given profile."""
        raise NotImplementedError

    def check(self) -> bool:
        """Perform a basic consistency check (optional implementation)."""
        self.logger.warning("Filesystem check not implemented for this type.")
        return True

    @abstractmethod
    def get_display_info(self) -> Dict[str, str]:
        """Return a dictionary of filesystem-specific parameters for display."""
        raise NotImplementedError

    @abstractmethod
    def get_disk_map_layout(self) -> Dict[str, Any]:
        """
        Return layout information for the disk map.
        Expected keys:
        - 'legend': List[Tuple[str, str]] (Label, HexColor)
        - 'get_sector_type': Callable[[int], str] (takes LBA, returns type string like "boot", "data_used", "bam", etc.)
        - 'allocation_unit_size_sectors': int (e.g., sectors per cluster)
        - 'first_data_sector': int (LBA of the first sector available for general file data)
        - (Optional) 'type_color_map': Dict[str, str] (maps type string to hex color, overrides legend colors if specific types need different mapping)
        """
        raise NotImplementedError

    @abstractmethod
    def get_specific_config(self) -> Optional[Any]:
        """Return filesystem-specific configuration details (e.g., BPB, DPB)."""
        raise NotImplementedError
