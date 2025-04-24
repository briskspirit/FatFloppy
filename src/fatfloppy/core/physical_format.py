# src/fatfloppy/core/physical_format.py
from dataclasses import dataclass

@dataclass
class PhysicalFormat:
    encoding: str                   # FM/MFM
    rate: int                       # Data rate (kbps)
    rpm: int                        # Rotations per minute
    gap3: int = 84                  # Gap3 size
    cskew: int = 0                  # Sector skew
    interleave: int = 1             # Interleave factor

    cylinders: int = 80             # Number of cylinders
    sectors_per_track: int = 18      # Sectors per track
    heads: int = 2                  # Number of heads
    bytes_per_sector: int = 512          # Size of each sector in bytes

    @property
    def total_sectors(self) -> int:
        return self.cylinders * self.heads * self.sectors_per_track

    @property
    def total_bytes(self) -> int:
        return self.total_sectors * self.bytes_per_sector
