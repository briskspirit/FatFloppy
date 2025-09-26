# src/fatfloppy/core/format_profile.py
"""
Defines the data structure for a complete disk format profile.

This module contains the FormatProfile dataclass, which encapsulates all the
necessary information to describe a specific floppy disk format. This includes
its physical geometry, the expected filesystem type, and the filesystem-specific
configuration parameters (like a FAT BPB or CP/M DPB).

Classes:
    FormatProfile: A dataclass representing a complete disk format profile.
"""
from dataclasses import dataclass
from typing import Optional, Any

from .physical_format import PhysicalFormat


@dataclass
class FormatProfile:
    """
    Encapsulates all parameters for a specific, named disk format.

    This class ties together the physical layout of a disk with its logical
    filesystem structure, creating a complete and reusable definition for
    formatting, detection, and interaction.

    Attributes:
        name: A short, unique identifier for the profile (e.g., "ibm_3.5_1.44m").
        description: A human-readable description of the format.
        physical_format: A PhysicalFormat object defining the disk's geometry.
        filesystem_type: A string indicating the filesystem (e.g., "FAT12", "CPM").
        filesystem_config: An optional object containing filesystem-specific
                           parameters, such as a FATVolumeInfo or
                           CPMDiskParameterBlock.
    """
    name: str
    description: str
    physical_format: PhysicalFormat
    filesystem_type: str
    filesystem_config: Optional[Any] = None

    @property
    def capacity_kb(self) -> float:
        """
        Calculates the total storage capacity of the disk in kilobytes.

        Returns:
            The total capacity in KB.
        """
        return self.physical_format.total_bytes / 1024
