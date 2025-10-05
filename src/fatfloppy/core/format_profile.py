# src/fatfloppy/core/format_profile.py
"""
Defines the data structure for a complete disk format profile.
"""
from dataclasses import dataclass
from typing import Any, Optional

from .physical_format import PhysicalFormat


@dataclass
class FormatProfile:
    """
    Encapsulates all parameters for a specific, named disk format.

    The filesystem type is now inferred from the filesystem_config object type
    via the plugin registry, rather than being stored as a hardcoded string.

    Attributes:
        name: A short, unique identifier for the profile (e.g., "ibm_3.5_1.44m").
        description: A human-readable description of the format.
        physical_format: A PhysicalFormat object defining the disk's geometry.
        filesystem_config: An optional object containing filesystem-specific
                           parameters, such as a FATVolumeInfo or
                           CPMDiskParameterBlock. The type of this object
                           determines the filesystem type.
        is_bootable: Whether this format is typically bootable.
        notes: Optional additional notes about the format.
    """

    name: str
    description: str
    physical_format: PhysicalFormat
    filesystem_config: Optional[Any] = None
    is_bootable: bool = False
    notes: Optional[str] = None

    def get_filesystem_type(self) -> Optional[str]:
        """
        Infers filesystem type from config class via plugin registry.

        Returns:
            Filesystem type name (e.g., "FAT12", "CPM") or None if no config
        """
        if self.filesystem_config is None:
            return None

        from .filesystem_registry import FilesystemRegistry

        config_type = type(self.filesystem_config)

        for fs_class in FilesystemRegistry.get_all():
            if (hasattr(fs_class, 'config_class') and
                    fs_class.config_class is not None):
                if fs_class.config_class == config_type:
                    return fs_class.filesystem_type

        return None
