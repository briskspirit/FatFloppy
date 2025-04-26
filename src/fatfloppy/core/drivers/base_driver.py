# src/fatfloppy/core/drivers/base_driver.py
from typing import List, Optional, Tuple, Dict

from ..utils.logging_config import get_logger
from ..physical_format import PhysicalFormat

logger = get_logger()

class DiskIODriver:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self.physical_format: Optional[PhysicalFormat] = None

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        raise NotImplementedError

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        raise NotImplementedError
