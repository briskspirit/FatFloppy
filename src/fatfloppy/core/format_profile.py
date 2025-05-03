# src/fatfloppy/core/formats.py
from dataclasses import dataclass
from typing import Optional, Any, Dict

from .physical_format import PhysicalFormat


@dataclass
class FormatProfile:
    name: str
    description: str
    physical_format: PhysicalFormat
    # filesystem_type: str # FIXME: Implement this??
    filesystem_metadata: Optional[Dict[str, Any]] = None

    @property
    def capacity_kb(self) -> float:
        return self.physical_format.total_bytes / 1024
