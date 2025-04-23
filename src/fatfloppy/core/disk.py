# src/fatfloppy/core/disk.py
from dataclasses import dataclass
from typing import Optional, Tuple

from .utils.logging_config import get_logger
from .drivers import DiskIODriver, PhysicalFormat

logger = get_logger()

@dataclass
class DiskGeometry:
    cylinders: int
    heads: int
    sectors_per_track: int
    sector_size: int

    @property
    def total_sectors(self) -> int:
        return self.cylinders * self.heads * self.sectors_per_track

    @property
    def total_bytes(self) -> int:
        return self.total_sectors * self.sector_size

class Disk:
    def __init__(self, driver: DiskIODriver):
        self.logger = get_logger(self.__class__.__name__)
        self.driver = driver
        self.geometry: Optional[DiskGeometry] = None

    def set_geometry(self, geometry: DiskGeometry) -> None:
        if not isinstance(geometry, DiskGeometry):
            raise TypeError("geometry must be a DiskGeometry object")
        self.geometry = geometry
        if hasattr(self.driver, "set_physical_format"):
            try:
                existing_pf = getattr(self.driver, "physical_format", None)
                if not existing_pf:
                    physical_format = PhysicalFormat(
                        encoding="MFM", rate=500, rpm=300, gap3=84,
                        sectors_per_track=geometry.sectors_per_track,
                        heads=geometry.heads, sector_size=geometry.sector_size,
                    )
                    self.driver.set_physical_format(physical_format)
                else:
                    existing_pf.sectors_per_track = geometry.sectors_per_track
                    existing_pf.heads = geometry.heads
                    existing_pf.sector_size = geometry.sector_size
                    self.driver.set_physical_format(existing_pf)
            except Exception as e:
                self.logger.error(f"Failed to set physical format: {e}", exc_info=True)

    def read_boot_sector(self) -> bytes:
        """Read the first 512 bytes of the disk as the boot sector."""
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        sector_size = self.geometry.sector_size
        num_sectors = (512 + sector_size - 1) // sector_size  # Ceiling division
        data = self.read_sectors(0, 0, 1, num_sectors)
        return data[:512]  # Return exactly 512 bytes

    def write_boot_sector(self, data: bytes) -> None:
        """Write a 512-byte boot sector to the disk."""
        if len(data) != 512:
            raise ValueError("Boot sector must be 512 bytes")
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        sector_size = self.geometry.sector_size
        num_sectors = (512 + sector_size - 1) // sector_size
        # Pad data if needed to align with sector boundaries
        padded_data = data + b'\0' * (num_sectors * sector_size - 512)
        self.write_sectors(0, 0, 1, padded_data)

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self._validate_chs(cylinder, head, sector)
        try:
            data = self.driver.read_sector(cylinder, head, sector)
            if len(data) < self.geometry.sector_size:
                data += bytes(self.geometry.sector_size - len(data))
            elif len(data) > self.geometry.sector_size:
                data = data[:self.geometry.sector_size]
            return data
        except Exception as e:
            raise IOError(f"Failed to read sector C:{cylinder} H:{head} S:{sector}") from e

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        self._validate_chs(cylinder, head, sector)
        if len(data) != self.geometry.sector_size:
            raise ValueError(f"Data size {len(data)} != sector size {self.geometry.sector_size}")
        try:
            self.driver.write_sector(cylinder, head, sector, data)
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
        for _ in range(num_sectors):
            if not (0 <= cylinder < self.geometry.cylinders and
                    0 <= head < self.geometry.heads and
                    1 <= sector <= self.geometry.sectors_per_track):
                raise ValueError(f"Invalid address: C:{cylinder} H:{head} S:{sector}")
            result.extend(self.read_sector(cylinder, head, sector))
            sector += 1
            if sector > self.geometry.sectors_per_track:
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
        sector_size = self.geometry.sector_size
        num_sectors = (len(data) + sector_size - 1) // sector_size
        cylinder, head, sector = start_cylinder, start_head, start_sector
        data_pos = 0
        for i in range(num_sectors):
            if not (0 <= cylinder < self.geometry.cylinders and
                    0 <= head < self.geometry.heads and
                    1 <= sector <= self.geometry.sectors_per_track):
                raise ValueError(f"Invalid address: C:{cylinder} H:{head} S:{sector}")
            chunk = data[data_pos:data_pos + sector_size]
            if len(chunk) < sector_size:
                chunk = chunk + bytes(sector_size - len(chunk))
            self.write_sector(cylinder, head, sector, chunk)
            data_pos += sector_size
            sector += 1
            if sector > self.geometry.sectors_per_track:
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
        geom = self.geometry
        if geom.sectors_per_track == 0 or geom.heads == 0:
            raise ValueError(f"Invalid geometry: SPT={geom.sectors_per_track}, H={geom.heads}")
        max_lba = geom.total_sectors - 1
        if not (0 <= lba <= max_lba):
            raise IndexError(f"LBA {lba} out of bounds (0-{max_lba})")
        sector = (lba % geom.sectors_per_track) + 1
        temp = lba // geom.sectors_per_track
        head = temp % geom.heads
        cylinder = temp // geom.heads
        return cylinder, head, sector

    def _validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        if not (0 <= cylinder < self.geometry.cylinders and
                0 <= head < self.geometry.heads and
                1 <= sector <= self.geometry.sectors_per_track):
            raise ValueError(f"Invalid sector address: C:{cylinder} H:{head} S:{sector}")
