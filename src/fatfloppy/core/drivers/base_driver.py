# src/fatfloppy/core/drivers/base_driver.py
from typing import List, Optional, Tuple, Dict

from ..utils.logging_config import get_logger
from ..physical_format import PhysicalFormat # Keep necessary imports for type hints

logger = get_logger()

class DiskIODriver:
    """Abstract base class for all disk I/O drivers."""
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.physical_format: Optional[PhysicalFormat] = None # Add type hint here

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Reads a single sector."""
        raise NotImplementedError

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """Writes a single sector."""
        raise NotImplementedError

    def flush(self) -> None:
        """Flushes any buffered writes to the underlying storage."""
        # Default implementation does nothing, subclasses can override
        pass

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """Sets the physical disk format information for the driver."""
        raise NotImplementedError
