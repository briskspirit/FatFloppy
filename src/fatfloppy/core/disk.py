# src/fatfloppy/core/disk.py
from typing import Optional, Tuple

from .utils.logging_config import get_logger
from .drivers import DiskIODriver, PhysicalFormat

logger = get_logger()

class Disk:
    def __init__(self, driver: DiskIODriver):
        self.logger = get_logger(self.__class__.__name__)
        self.driver = driver
        self.geometry: Optional[PhysicalFormat] = None

    def set_geometry(self, geometry: PhysicalFormat) -> None:
        if not isinstance(geometry, PhysicalFormat):
            raise TypeError("geometry must be a PhysicalFormat object")
        self.geometry = geometry
        self.logger.info(f"Setting geometry: bytes_per_sector={geometry.bytes_per_sector}")
        if hasattr(self.driver, "set_physical_format"):
            try:
                self.driver.set_physical_format(geometry)
            except Exception as e:
                self.logger.error(f"Failed to set physical format: {e}", exc_info=True)

    def read_boot_sector(self) -> bytes:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        bytes_per_sector = self.geometry.bytes_per_sector
        sectors_to_read = 4096 // bytes_per_sector
        data = self.read_sectors(0, 0, 1, sectors_to_read)
        return data

    def write_boot_sector(self, data: bytes) -> None:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        bytes_per_sector = self.geometry.bytes_per_sector
        if len(data) != bytes_per_sector:
            raise ValueError(f"Boot sector must be {bytes_per_sector} bytes, got {len(data)} bytes")
        self.write_sectors(0, 0, 1, data)

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self._validate_chs(cylinder, head, sector)
        try:
            physical_head = self.geometry.get_physical_head(head) if getattr(self.driver, 'uses_physical_heads', False) else head
            data = self.driver.read_sector(cylinder, physical_head, sector)
            if len(data) < self.geometry.bytes_per_sector:
                data += bytes(self.geometry.bytes_per_sector - len(data))
            elif len(data) > self.geometry.bytes_per_sector:
                data = data[:self.geometry.bytes_per_sector]
            return data
        except Exception as e:
            raise IOError(f"Failed to read sector C:{cylinder} H:{head} S:{sector}") from e

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        self._validate_chs(cylinder, head, sector)
        if len(data) != self.geometry.bytes_per_sector:
            raise ValueError(f"Data size {len(data)} != sector size {self.geometry.bytes_per_sector}")
        try:
            physical_head = self.geometry.get_physical_head(head) if getattr(self.driver, 'uses_physical_heads', False) else head
            self.driver.write_sector(cylinder, physical_head, sector, data)
        except Exception as e:
            raise IOError(f"Failed to write sector C:{cylinder} H:{head} S:{sector}") from e

    def read_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                     num_sectors: int) -> bytes:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        if num_sectors <= 0:
            return b''
        result = bytearray()
        cylinder, head, sector = start_cylinder, start_head, start_sector
        self.logger.debug(f"read_sectors: starting C:{cylinder} H:{head} S:{sector}, num_sectors={num_sectors}, bytes_per_sector={self.geometry.bytes_per_sector}")
        for _ in range(num_sectors):
            self.geometry.validate_chs(cylinder, head, sector)
            result.extend(self.read_sector(cylinder, head, sector))
            sector += 1
            if sector > self.geometry.get_sectors_per_track(cylinder, head):
                sector = 1
                head += 1
                if head >= self.geometry.heads:
                    head = 0
                    cylinder += 1
        return bytes(result)

    def write_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                      data: bytes) -> None:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        if not data:
            return
        bytes_per_sector = self.geometry.bytes_per_sector
        num_sectors = (len(data) + bytes_per_sector - 1) // bytes_per_sector
        cylinder, head, sector = start_cylinder, start_head, start_sector
        data_pos = 0
        for i in range(num_sectors):
            self.geometry.validate_chs(cylinder, head, sector)
            chunk = data[data_pos:data_pos + bytes_per_sector]
            if len(chunk) < bytes_per_sector:
                chunk = chunk + bytes(bytes_per_sector - len(chunk))
            self.write_sector(cylinder, head, sector, chunk)
            data_pos += bytes_per_sector
            sector += 1
            if sector > self.geometry.get_sectors_per_track(cylinder, head):
                sector = 1
                head += 1
                if head >= self.geometry.heads:
                    head = 0
                    cylinder += 1

    def flush(self) -> None:
        if hasattr(self.driver, "flush"):
            try:
                self.driver.flush()
            except Exception as e:
                raise IOError("Disk flush failed") from e

    def lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        return self.geometry.lba_to_chs(lba)

    def _validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        self.geometry.validate_chs(cylinder, head, sector)
