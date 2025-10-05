# src/fatfloppy/core/drivers/__init__.py

"""
Core Disk I/O Drivers Package
"""

from .base_driver import DiskIODriver
from .h17 import H17ImageDriver
from .imd import IMDImageDriver
from .img import IMGImageDriver
from .mits_dsk import MITSDSKDriver

try:
    from .greaseweazle import GreaseweazleDriver

    GREASEWEAZLE_AVAILABLE = True
except ImportError:
    GreaseweazleDriver = None
    GREASEWEAZLE_AVAILABLE = False

__all__ = [
    "DiskIODriver",
    "IMGImageDriver",
    "IMDImageDriver",
    "H17ImageDriver",
    "MITSDSKDriver",
    "GreaseweazleDriver",
    "GREASEWEAZLE_AVAILABLE",
]
