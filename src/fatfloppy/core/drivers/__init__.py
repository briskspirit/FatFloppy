# src/fatfloppy/core/drivers/__init__.py

"""
Core Disk I/O Drivers Package
"""

from .base_driver import DiskIODriver
from .img import IMGImageDriver
from .imd import IMDImageDriver

# Conditionally import Greaseweazle to handle missing dependency
try:
    from .greaseweazle import GreaseweazleDriver
    GREASEWEAZLE_AVAILABLE = True
except ImportError:
    # Define placeholder if Greaseweazle is not installed
    # This allows the rest of the application to check the flag
    # without crashing on import if the library is missing.
    GreaseweazleDriver = None
    GREASEWEAZLE_AVAILABLE = False

__all__ = [
    'DiskIODriver',
    'IMGImageDriver',
    'IMDImageDriver',
    'GreaseweazleDriver',
    'GREASEWEAZLE_AVAILABLE',
]
