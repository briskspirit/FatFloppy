# disk.py

from dataclasses import dataclass
from typing import Optional, List, Dict, Any

from drivers import DiskIODriver, PhysicalFormat

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
        self.driver = driver
        self.geometry: Optional[DiskGeometry] = None

    def set_geometry(self, geometry: DiskGeometry) -> None:
        self.geometry = geometry

        # Update the driver's physical format with geometry settings
        if not hasattr(self.driver, "physical_format") or not self.driver.physical_format:
            # Create a default physical format if none exists
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
            self.driver.physical_format.sectors_per_track = geometry.sectors_per_track
            self.driver.physical_format.heads = geometry.heads
            self.driver.physical_format.sector_size = geometry.sector_size

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        if not self.geometry:
            raise ValueError("Disk geometry not set")

        if (cylinder >= self.geometry.cylinders or
            head >= self.geometry.heads or
            sector < 1 or sector > self.geometry.sectors_per_track):
            raise ValueError(f"Invalid sector address: {cylinder},{head},{sector}")

        return self.driver.read_sector(cylinder, head, sector)

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.geometry:
            raise ValueError("Disk geometry not set")

        if (cylinder >= self.geometry.cylinders or
            head >= self.geometry.heads or
            sector < 1 or sector > self.geometry.sectors_per_track):
            raise ValueError(f"Invalid sector address: {cylinder},{head},{sector}")

        if len(data) != self.geometry.sector_size:
            if len(data) < self.geometry.sector_size:
                data = data + bytes(self.geometry.sector_size - len(data))
            else:
                data = data[:self.geometry.sector_size]

        self.driver.write_sector(cylinder, head, sector, data)

    def read_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                     num_sectors: int) -> bytes:
        result = bytearray()

        cylinder, head, sector = start_cylinder, start_head, start_sector
        for _ in range(num_sectors):
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
        if not data:
            return

        sector_size = self.geometry.sector_size
        num_sectors = (len(data) + sector_size - 1) // sector_size

        cylinder, head, sector = start_cylinder, start_head, start_sector
        for i in range(num_sectors):
            chunk_start = i * sector_size
            chunk_end = min(chunk_start + sector_size, len(data))
            chunk = data[chunk_start:chunk_end]

            self.write_sector(cylinder, head, sector, chunk)

            sector += 1
            if sector > self.geometry.sectors_per_track:
                sector = 1
                head += 1
                if head >= self.geometry.heads:
                    head = 0
                    cylinder += 1

    def flush(self) -> None:
        self.driver.flush()
