# src/fatfloppy/core/physical_format.py
from dataclasses import dataclass
from typing import List, Tuple, Optional
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
    interleave: int
    bytes_per_sector: Optional[int] = None
    sector_translation_table: Optional[List[int]] = None
    id_start: int = 1
    iam_present: bool = True
    gap1_bytes: Optional[int] = None
    gap2_bytes: Optional[int] = None
    gap3_bytes: Optional[int] = None
    cskew: Optional[int] = None
    hskew: Optional[int] = None

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
    image_in_sector_id_order: bool = True

    def __post_init__(self):
        """Validate that track formats cover the disk without overlaps."""
        covered = set()
        for tf in self.track_formats:
            # If a track format doesn't specify BPS, it inherits from the main PhysicalFormat
            if tf.bytes_per_sector is None:
                tf.bytes_per_sector = self.bytes_per_sector
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

    def get_bytes_per_sector(self, cylinder: int, head: int) -> int:
        """Get bytes per sector for a specific cylinder and head."""
        return self.get_track_format(cylinder, head).bytes_per_sector

    @property
    def has_variable_bps(self) -> bool:
        """Check if any track format defines a different bytes_per_sector."""
        if not self.track_formats:
            return False
        first_bps = self.track_formats[0].bytes_per_sector
        return any(tf.bytes_per_sector != first_bps for tf in self.track_formats)

    def get_physical_head(self, logical_head: int) -> int:
        """Map logical head to physical head if heads are inverted."""
        return 1 - logical_head if self.heads_inverted else logical_head

    def validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        """Validate cylinder, head, sector values."""
        if not (0 <= cylinder < self.cylinders and 0 <= head < self.heads):
            raise ValueError(f"Invalid CHS: {cylinder}, {head}, {sector}")

        track_format_for_validation = self.get_track_format(cylinder, head)
        max_sectors = track_format_for_validation.sectors_per_track
        current_id_start = track_format_for_validation.id_start

        if not (current_id_start <= sector < current_id_start + max_sectors):
            raise ValueError(f"Sector {sector} out of range ({current_id_start}-{current_id_start + max_sectors -1}) for C:{cylinder} H:{head}")

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
        if self.has_variable_bps:
            total = 0
            for c in range(self.cylinders):
                for h in range(self.heads):
                    track_format = self.get_track_format(c, h)
                    total += track_format.sectors_per_track * track_format.bytes_per_sector
            return total
        else:
            return self.total_sectors * self.bytes_per_sector

    def lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        """Convert LBA to CHS."""
        if self.has_variable_bps:
            raise NotImplementedError("LBA to CHS is not supported for disks with variable sector sizes.")
        if lba >= self.total_sectors:
            raise ValueError(f"LBA {lba} exceeds total sectors {self.total_sectors}")
        sector_count = 0
        for c in range(self.cylinders):
            for h in range(self.heads):
                current_track_format = self.get_track_format(c,h)
                spt = current_track_format.sectors_per_track
                current_id_start = current_track_format.id_start
                if sector_count + spt > lba:
                    sector_offset = lba - sector_count
                    sector = current_id_start + sector_offset
                    return (c, h, sector)
                sector_count += spt
        raise ValueError("LBA conversion failed")

    def chs_to_lba(self, cylinder: int, head: int, sector: int) -> int:
        """Convert CHS to LBA."""
        if self.has_variable_bps:
            raise NotImplementedError("CHS to LBA is not supported for disks with variable sector sizes.")
        self.validate_chs(cylinder, head, sector)
        lba = 0
        for c_iter in range(cylinder):
            for h_iter in range(self.heads):
                lba += self.get_sectors_per_track(c_iter, h_iter)

        for h_iter in range(head):
            lba += self.get_sectors_per_track(cylinder, h_iter)

        current_track_format_for_lba = self.get_track_format(cylinder, head)
        current_id_start_for_lba = current_track_format_for_lba.id_start
        lba += (sector - current_id_start_for_lba)
        return lba
    
    def _build_physical_sector_order(self, tf: TrackFormat) -> List[int]:
        spt   = tf.sectors_per_track
        inter = tf.interleave if getattr(tf, "interleave", 1) and tf.interleave > 0 else 1
        start = getattr(tf, "id_start", 1)
        order, used, idx = [], [False]*spt, 0
        for i in range(spt):
            order.append(start + idx); used[idx] = True
            if i < spt - 1:
                idx = (idx + inter) % spt
                while used[idx]: idx = (idx + 1) % spt
        return order

    def chs_to_byte_offset(self, cylinder: int, head: int, sector: int) -> int:
        """Convert CHS to a byte offset from the start of the disk image."""
        self.validate_chs(cylinder, head, sector)
        byte_offset = 0
        # Sum bytes of all full cylinders before the target cylinder
        for c_iter in range(cylinder):
            for h_iter in range(self.heads):
                track_format = self.get_track_format(c_iter, h_iter)
                byte_offset += track_format.sectors_per_track * track_format.bytes_per_sector

        # Sum bytes of all full heads on the target cylinder before the target head
        for h_iter in range(head):
            track_format = self.get_track_format(cylinder, h_iter)
            byte_offset += track_format.sectors_per_track * track_format.bytes_per_sector

        # Add the offset for the sectors on the target track before the target sector
        track_format = self.get_track_format(cylinder, head)
        order = self._build_physical_sector_order(track_format)
        if self.image_in_sector_id_order:
            sector_index_on_track = sector - track_format.id_start
        else:
            sector_index_on_track = order.index(sector)  # physical slot
        byte_offset += sector_index_on_track * track_format.bytes_per_sector
        
        return byte_offset
