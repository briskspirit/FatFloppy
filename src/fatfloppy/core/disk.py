# src/fatfloppy/core/disk.py
from dataclasses import dataclass
from typing import Optional

from ..utils.logging_config import get_logger
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
        self.logger.debug("Disk object initialized")

    def set_geometry(self, geometry: DiskGeometry) -> None:
        self.logger.info(f"Setting disk geometry from object: {geometry!r}") # Log the passed object
        self.logger.info(f"  Passed Geometry -> Cyls: {geometry.cylinders}, Heads: {geometry.heads}, SPT: {geometry.sectors_per_track}, Size: {geometry.sector_size}")
        self.geometry = geometry
        self.logger.info(f"  Result self.geometry -> Cyls: {self.geometry.cylinders}, Heads: {self.geometry.heads}, SPT: {self.geometry.sectors_per_track}, Size: {self.geometry.sector_size}")

        # Update the driver's physical format with geometry settings
        if not hasattr(self.driver, "physical_format") or not self.driver.physical_format:
            # Create a default physical format if none exists
            self.logger.debug("Creating default physical format for driver")
            self.driver.set_physical_format(PhysicalFormat(
                encoding="MFM",
                rate=500,
                rpm=300,
                gap3=84,
                sectors_per_track=geometry.sectors_per_track,
                heads=geometry.heads,
                sector_size=geometry.sector_size
            ))
        else:
            # Update existing physical format
            self.logger.debug("Updating existing physical format in driver")
            self.driver.physical_format.sectors_per_track = geometry.sectors_per_track
            self.driver.physical_format.heads = geometry.heads
            self.driver.physical_format.sector_size = geometry.sector_size

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        if not self.geometry:
            error_msg = "Disk geometry not set"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if (cylinder >= self.geometry.cylinders or
            head >= self.geometry.heads or
            sector < 1 or sector > self.geometry.sectors_per_track):
            error_msg = f"Invalid sector address: C:{cylinder} H:{head} S:{sector}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        data = self.driver.read_sector(cylinder, head, sector)
        self.logger.debug(f"Read {len(data)} bytes from sector C:{cylinder} H:{head} S:{sector}")
        return data

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.geometry:
            error_msg = "Disk geometry not set"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if (cylinder >= self.geometry.cylinders or
            head >= self.geometry.heads or
            sector < 1 or sector > self.geometry.sectors_per_track):
            error_msg = f"Invalid sector address: C:{cylinder} H:{head} S:{sector}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if len(data) != self.geometry.sector_size:
            error_msg = f"Data size {len(data)} does not match sector size {self.geometry.sector_size}"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        self.logger.debug(f"Writing {len(data)} bytes to sector C:{cylinder} H:{head} S:{sector}")
        self.driver.write_sector(cylinder, head, sector, data)

    def read_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                     num_sectors: int) -> bytes:
        self.logger.debug(f"Reading {num_sectors} sectors starting at C:{start_cylinder} H:{start_head} S:{start_sector}")
        result = bytearray()

        cylinder, head, sector = start_cylinder, start_head, start_sector
        for i in range(num_sectors):
            self.logger.debug(f"Reading sector {i+1}/{num_sectors}: C:{cylinder} H:{head} S:{sector}")
            result.extend(self.read_sector(cylinder, head, sector))

            sector += 1
            if sector > self.geometry.sectors_per_track:
                sector = 1
                head += 1
                if head >= self.geometry.heads:
                    head = 0
                    cylinder += 1

        self.logger.debug(f"Read {len(result)} bytes from {num_sectors} sectors")
        return bytes(result)

    def write_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                      data: bytes) -> None:
        if not self.geometry:
            error_msg = "Disk geometry not set"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if not data:
            self.logger.warning("No data to write")
            return

        sector_size = self.geometry.sector_size
        num_sectors = (len(data) + sector_size - 1) // sector_size
        self.logger.debug(f"Writing {len(data)} bytes to {num_sectors} sectors starting at C:{start_cylinder} H:{start_head} S:{start_sector}")

        cylinder, head, sector = start_cylinder, start_head, start_sector
        for i in range(num_sectors):
            chunk_start = i * sector_size
            chunk_end = min(chunk_start + sector_size, len(data))
            chunk = data[chunk_start:chunk_end]

            if len(chunk) < sector_size:
                self.logger.debug(f"Padding last sector from {len(chunk)} to {sector_size} bytes")
                chunk = chunk + b'\x00' * (sector_size - len(chunk))

            self.logger.debug(f"Writing sector {i+1}/{num_sectors}: C:{cylinder} H:{head} S:{sector}")
            self.write_sector(cylinder, head, sector, chunk)

            sector += 1
            if sector > self.geometry.sectors_per_track:
                sector = 1
                head += 1
                if head >= self.geometry.heads:
                    head = 0
                    cylinder += 1

        self.logger.debug(f"Wrote {len(data)} bytes to {num_sectors} sectors")

    def flush(self) -> None:
        self.logger.debug("Flushing disk changes")
        self.driver.flush()
