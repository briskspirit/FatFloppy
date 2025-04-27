# src/fatfloppy/core/formats.py
from dataclasses import dataclass
from typing import Optional, Any

from .physical_format import PhysicalFormat


@dataclass
class FormatProfile:
    name: str
    description: str
    physical_format: PhysicalFormat
    boot_sector: Optional[Any] = None

    @property
    def capacity_kb(self) -> float:
        return self.physical_format.total_bytes / 1024
