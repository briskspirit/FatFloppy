# src/fatfloppy/core/physical_format.py
from dataclasses import dataclass

@dataclass
class PhysicalFormat:
    encoding: str
    rate: int
    rpm: int
    cylinders: int
    heads: int
    sectors_per_track: int
    bytes_per_sector: int
    gap3: int = 84
    cskew: int = 0
    interleave: int = 1

    @property
    def total_sectors(self) -> int:
        return self.cylinders * self.heads * self.sectors_per_track

    @property
    def total_bytes(self) -> int:
        return self.total_sectors * self.bytes_per_sector

    def validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        if not (0 <= cylinder < self.cylinders and
                0 <= head < self.heads and
                1 <= sector <= self.sectors_per_track):
            raise ValueError(f"Invalid sector address: C:{cylinder} H:{head} S:{sector}")
