# src/fatfloppy/core/physical_format.py
from dataclasses import dataclass

@dataclass
class PhysicalFormat:
    encoding: str  # FM/MFM
    rate: int      # Data rate (kbps)
    rpm: int       # Rotations per minute
    gap3: int = 84 # Gap3 size
    cskew: int = 0 # Sector skew
    interleave: int = 1
    sectors_per_track: int = 18
    heads: int = 2
    sector_size: int = 512
