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
                existing_pf = getattr(self.driver, "physical_format", None)
                if not existing_pf:
                    # TODO: not sure that defaults are a good idea
                    physical_format = PhysicalFormat(
                        encoding="MFM", rate=500, rpm=300, gap3=84,
                        sectors_per_track=geometry.sectors_per_track,
                        heads=geometry.heads, bytes_per_sector=geometry.bytes_per_sector,
                        cskew=0, interleave=1, cylinders=geometry.cylinders
                    )
                    self.driver.set_physical_format(physical_format)
                else:
                    existing_pf.sectors_per_track = geometry.sectors_per_track
                    existing_pf.heads = geometry.heads
                    existing_pf.bytes_per_sector = geometry.bytes_per_sector
                    existing_pf.cylinders = geometry.cylinders
                    self.driver.set_physical_format(existing_pf)
            except Exception as e:
                self.logger.error(f"Failed to set physical format: {e}", exc_info=True)

    def read_boot_sector(self) -> bytes:
        """Read the boot sector (first sector) of the disk."""
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        bytes_per_sector = self.geometry.bytes_per_sector
        sectors_to_read = 4096 // bytes_per_sector # TODO: always read max possible sector size before we know actual sector size from BPB?
        data = self.read_sectors(0, 0, 1, sectors_to_read)  # Read exactly one sector
        return data

    def write_boot_sector(self, data: bytes) -> None:
        """Write the boot sector (first sector) to the disk."""
        if not self.geometry:
            raise ValueError("Disk geometry not set")
        bytes_per_sector = self.geometry.bytes_per_sector
        if len(data) != bytes_per_sector:
            raise ValueError(f"Boot sector must be {bytes_per_sector} bytes, got {len(data)} bytes")
        self.write_sectors(0, 0, 1, data)

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self._validate_chs(cylinder, head, sector)
        try:
            data = self.driver.read_sector(cylinder, head, sector)
            # self.logger.debug(f"read_sector: starting C:{cylinder} H:{head} S:{sector}, bytes_per_sector={self.geometry.bytes_per_sector}")
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
        self.logger.debug(f"read_sectors: starting C:{cylinder} H:{head} S:{sector}, num_sectors={num_sectors}, bytes_per_sector={self.geometry.bytes_per_sector}")
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
        bytes_per_sector = self.geometry.bytes_per_sector
        num_sectors = (len(data) + bytes_per_sector - 1) // bytes_per_sector
        cylinder, head, sector = start_cylinder, start_head, start_sector
        data_pos = 0
        for i in range(num_sectors):
            if not (0 <= cylinder < self.geometry.cylinders and
                    0 <= head < self.geometry.heads and
                    1 <= sector <= self.geometry.sectors_per_track):
                raise ValueError(f"Invalid address: C:{cylinder} H:{head} S:{sector}")
            chunk = data[data_pos:data_pos + bytes_per_sector]
            if len(chunk) < bytes_per_sector:
                chunk = chunk + bytes(bytes_per_sector - len(chunk))
            self.write_sector(cylinder, head, sector, chunk)
            data_pos += bytes_per_sector
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
        total_sectors = geom.cylinders * geom.heads * geom.sectors_per_track
        if not (0 <= lba < total_sectors):
            raise IndexError(f"LBA {lba} out of bounds (0-{total_sectors - 1})")
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
