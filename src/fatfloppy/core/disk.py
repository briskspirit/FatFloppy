# src/fatfloppy/core/disk.py
from typing import Optional, Tuple

from .utils.logging_config import get_logger
from .drivers.base_driver import DiskIODriver
from .physical_format import PhysicalFormat

logger = get_logger()

class Disk:
    def __init__(self, driver: DiskIODriver):
        self.logger = get_logger(self.__class__.__name__)
        self.driver = driver
        self.physical_format: Optional[PhysicalFormat] = None

    def set_geometry(self, geometry: PhysicalFormat) -> None:
        if not isinstance(geometry, PhysicalFormat):
            raise TypeError("geometry must be a PhysicalFormat object")
        self.physical_format = geometry
        self.logger.info(f"Disk geometry set: bytes_per_sector={geometry.bytes_per_sector}")
        if hasattr(self.driver, "set_physical_format"):
            try:
                self.driver.set_physical_format(geometry)
                self.logger.debug("Physical format applied to driver")
            except Exception as e:
                self.logger.error(f"Failed to set physical format: {e}", exc_info=True)
                raise

    # FIXME: Should be removed, filesystem should decide what it needs and what to read, Disk doesn't care
    def read_boot_sector(self) -> bytes:
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        bytes_per_sector = self.physical_format.bytes_per_sector
        sectors_to_read = 4096 // bytes_per_sector
        self.logger.debug(f"Reading boot sector: {sectors_to_read} sectors")
        data = self.read_sectors(0, 0, 1, sectors_to_read)
        return data

    def write_boot_sector(self, data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        bytes_per_sector = self.physical_format.bytes_per_sector
        if len(data) != bytes_per_sector:
            raise ValueError(f"Boot sector must be {bytes_per_sector} bytes, got {len(data)} bytes")
        self.logger.debug("Writing boot sector")
        self.write_sectors(0, 0, 1, data)

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self._validate_chs(cylinder, head, sector)
        self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        try:
            physical_head = self.physical_format.get_physical_head(head) if getattr(self.driver, 'uses_physical_heads', False) else head
            data = self.driver.read_sector(cylinder, physical_head, sector)
            if len(data) < self.physical_format.bytes_per_sector:
                data += bytes(self.physical_format.bytes_per_sector - len(data))
            elif len(data) > self.physical_format.bytes_per_sector:
                data = data[:self.physical_format.bytes_per_sector]
            return data
        except Exception as e:
            self.logger.error(f"Failed to read sector C:{cylinder} H:{head} S:{sector}: {e}", exc_info=True)
            raise IOError(f"Failed to read sector C:{cylinder} H:{head} S:{sector}") from e

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        self._validate_chs(cylinder, head, sector)
        if len(data) != self.physical_format.bytes_per_sector:
            raise ValueError(f"Data size {len(data)} != sector size {self.physical_format.bytes_per_sector}")
        self.logger.debug(f"Writing sector C:{cylinder} H:{head} S:{sector}")
        try:
            physical_head = self.physical_format.get_physical_head(head) if getattr(self.driver, 'uses_physical_heads', False) else head
            self.driver.write_sector(cylinder, physical_head, sector, data)
        except Exception as e:
            self.logger.error(f"Failed to write sector C:{cylinder} H:{head} S:{sector}: {e}", exc_info=True)
            raise IOError(f"Failed to write sector C:{cylinder} H:{head} S:{sector}") from e

    def read_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                     num_sectors: int) -> bytes:
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        if num_sectors <= 0:
            return b''
        self.logger.debug(f"Reading {num_sectors} sectors starting at C:{start_cylinder} H:{start_head} S:{start_sector}")
        result = bytearray()
        cylinder, head, sector = start_cylinder, start_head, start_sector
        for _ in range(num_sectors):
            self.physical_format.validate_chs(cylinder, head, sector)
            result.extend(self.read_sector(cylinder, head, sector))
            sector += 1
            if sector > self.physical_format.get_sectors_per_track(cylinder, head):
                sector = 1
                head += 1
                if head >= self.physical_format.heads:
                    head = 0
                    cylinder += 1
        return bytes(result)

    def write_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                      data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        if not data:
            return
        bytes_per_sector = self.physical_format.bytes_per_sector
        num_sectors = (len(data) + bytes_per_sector - 1) // bytes_per_sector
        self.logger.debug(f"Writing {num_sectors} sectors starting at C:{start_cylinder} H:{start_head} S:{start_sector}")
        cylinder, head, sector = start_cylinder, start_head, start_sector
        data_pos = 0
        for _ in range(num_sectors):
            self.physical_format.validate_chs(cylinder, head, sector)
            chunk = data[data_pos:data_pos + bytes_per_sector]
            if len(chunk) < bytes_per_sector:
                chunk = chunk + bytes(bytes_per_sector - len(chunk))
            self.write_sector(cylinder, head, sector, chunk)
            data_pos += bytes_per_sector
            sector += 1
            if sector > self.physical_format.get_sectors_per_track(cylinder, head):
                sector = 1
                head += 1
                if head >= self.physical_format.heads:
                    head = 0
                    cylinder += 1

    def flush(self) -> None:
        if hasattr(self.driver, "flush"):
            self.logger.debug("Flushing disk")
            try:
                self.driver.flush()
            except Exception as e:
                self.logger.error(f"Disk flush failed: {e}", exc_info=True)
                raise IOError("Disk flush failed") from e

    def lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        self.logger.debug(f"Converting LBA {lba} to CHS")
        return self.physical_format.lba_to_chs(lba)

    def _validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        self.physical_format.validate_chs(cylinder, head, sector)
