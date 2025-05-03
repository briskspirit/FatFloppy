from dataclasses import dataclass
from typing import List, Tuple
import logging


@dataclass
class TrackFormat:
    track_start: int
    track_end: int
    head_start: int
    head_end: int
    sectors_per_track: int
    encoding: str
    rate: int
    gap3: int
    interleave: int

    def matches(self, cylinder: int, head: int) -> bool:
        """Check if this TrackFormat applies to the given cylinder and head."""
        return (self.track_start <= cylinder <= self.track_end and
                self.head_start <= head <= self.head_end)

@dataclass
class PhysicalFormat:
    cylinders: int
    heads: int
    rpm: int
    heads_inverted: bool
    bytes_per_sector: int
    track_formats: List[TrackFormat]

    def __post_init__(self):
        """Validate that track formats cover the disk without overlaps."""
        covered = set()
        for tf in self.track_formats:
            for c in range(tf.track_start, tf.track_end + 1):
                for h in range(tf.head_start, tf.head_end + 1):
                    if (c, h) in covered:
                        logging.warning(f"Overlap at cylinder {c}, head {h}")
                    covered.add((c, h))
        total_tracks = self.cylinders * self.heads
        if len(covered) != total_tracks:
            logging.warning(f"Track formats cover {len(covered)}/{total_tracks} tracks")

    def get_track_format(self, cylinder: int, head: int) -> TrackFormat:
        """Retrieve the TrackFormat for a specific cylinder and head."""
        for tf in self.track_formats:
            if tf.matches(cylinder, head):
                return tf
        raise ValueError(f"No TrackFormat for cylinder {cylinder}, head {head}")

    def get_sectors_per_track(self, cylinder: int, head: int) -> int:
        """Get sectors per track for a specific cylinder and head."""
        return self.get_track_format(cylinder, head).sectors_per_track

    def get_physical_head(self, logical_head: int) -> int:
        """Map logical head to physical head if heads are inverted."""
        return 1 - logical_head if self.heads_inverted else logical_head

    def validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        """Validate cylinder, head, sector values."""
        if not (0 <= cylinder < self.cylinders and 0 <= head < self.heads):
            raise ValueError(f"Invalid CHS: {cylinder}, {head}, {sector}")
        max_sectors = self.get_sectors_per_track(cylinder, head)
        if not 0 < sector <= max_sectors:
            raise ValueError(f"Sector {sector} out of range (1-{max_sectors})")

    @property
    def total_sectors(self) -> int:
        """Calculate total number of sectors on the disk."""
        total = 0
        for c in range(self.cylinders):
            for h in range(self.heads):
                total += self.get_sectors_per_track(c, h)
        return total

    @property
    def total_bytes(self) -> int:
        """Calculate total bytes on the disk."""
        if not self.bytes_per_sector or self.bytes_per_sector <= 0:
            return 0
        return self.total_sectors * self.bytes_per_sector

    def lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        """Convert LBA to CHS."""
        if lba >= self.total_sectors:
            raise ValueError(f"LBA {lba} exceeds total sectors {self.total_sectors}")
        sector_count = 0
        for c in range(self.cylinders):
            for h in range(self.heads):
                spt = self.get_sectors_per_track(c, h)
                if sector_count + spt > lba:
                    sector = lba - sector_count + 1
                    return (c, h, sector)
                sector_count += spt
        raise ValueError("LBA conversion failed")

    def chs_to_lba(self, cylinder: int, head: int, sector: int) -> int:
        """Convert CHS to LBA."""
        self.validate_chs(cylinder, head, sector)
        lba = 0
        for c in range(cylinder):
            for h in range(self.heads):
                lba += self.get_sectors_per_track(c, h)
        for h in range(head):
            lba += self.get_sectors_per_track(cylinder, h)
        lba += sector - 1
        return lba
