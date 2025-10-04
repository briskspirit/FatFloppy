# src/fatfloppy/core/physical_format.py
"""
Defines the data structures for representing a disk's physical geometry.

This module provides the necessary classes to describe the physical layout of a
floppy disk, including its dimensions (cylinders, heads), track characteristics
(sectors, encoding), and support for variable formats across different tracks.

Classes:
    TrackFormat: Describes the format of a single track or a range of tracks.
    PhysicalFormat: Encapsulates the complete physical geometry of a disk.
"""
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
import logging


@dataclass
class TrackFormat:
    """
    Describes the layout and properties of a range of tracks.

    Attributes:
        track_start: The starting cylinder number for this format.
        track_end: The ending cylinder number for this format.
        head_start: The starting head number for this format.
        head_end: The ending head number for this format.
        sectors_per_track: The number of sectors on tracks with this format.
        encoding: The data encoding method (e.g., "MFM", "FM").
        rate: The data transfer rate in kbps (e.g., 250, 500).
        interleave: The sector interleave factor.
        bytes_per_sector: The size of each sector in bytes. Inherits from
                          PhysicalFormat if not specified.
        sector_translation_table: An optional explicit mapping of logical to
                                  physical sector IDs. If None, will be auto-generated
                                  from interleave during __post_init__.
        id_start: The starting sector ID number (usually 0 or 1).
        iam_present: Whether an Index Address Mark is present.
        gap1_bytes, gap2_bytes, gap3_bytes: Optional gap sizes in bytes.
        cskew, hskew: Optional cylinder and head skew values.
    """
    track_start: int
    track_end: int
    head_start: int
    head_end: int
    sectors_per_track: int
    encoding: str
    rate: int
    interleave: Optional[int] = 1
    bytes_per_sector: Optional[int] = None
    sector_translation_table: Optional[List[int]] = field(default=None)
    id_start: int = 1
    iam_present: bool = True
    gap1_bytes: Optional[int] = None
    gap2_bytes: Optional[int] = None
    gap3_bytes: Optional[int] = None
    cskew: Optional[int] = None
    hskew: Optional[int] = None

    def __post_init__(self):
        """
        Ensures sector_translation_table is always populated.

        If not explicitly provided, builds it automatically from interleave.
        """
        if self.sector_translation_table is None:
            self.sector_translation_table = self._build_translation_table()

    def _build_translation_table(self) -> List[int]:
        """
        Builds a sector translation table based on interleave.

        Maps logical sector indices (0-based) to physical sector IDs.

        Returns:
            A list where index is logical sector index and value is physical sector ID.
        """
        spt = self.sectors_per_track
        interleave = self.interleave if self.interleave > 0 else 1
        start_id = self.id_start

        order: List[int] = []
        used: List[bool] = [False] * spt
        idx = 0

        for i in range(spt):
            order.append(start_id + idx)
            used[idx] = True
            if i < spt - 1:
                idx = (idx + interleave) % spt
                while used[idx]:
                    idx = (idx + 1) % spt

        return order

    def logical_to_physical_sector(self, logical_index: int) -> int:
        """
        Converts a logical sector index (0-based) to a physical sector ID.

        Args:
            logical_index: 0-based index into the track

        Returns:
            The physical sector ID

        Raises:
            IndexError: If logical_index is out of range
        """
        if not (0 <= logical_index < self.sectors_per_track):
            raise IndexError(
                f"Logical sector index {logical_index} out of range "
                f"(0-{self.sectors_per_track-1})"
            )
        return self.sector_translation_table[logical_index]

    def physical_to_logical_sector(self, physical_id: int) -> int:
        """
        Converts a physical sector ID to a logical sector index (0-based).

        Args:
            physical_id: The physical sector ID

        Returns:
            The 0-based logical index

        Raises:
            ValueError: If physical_id is not found in translation table
        """
        try:
            return self.sector_translation_table.index(physical_id)
        except ValueError:
            raise ValueError(
                f"Physical sector ID {physical_id} not found in translation table"
            )

    def matches(self, cylinder: int, head: int) -> bool:
        """
        Checks if this TrackFormat applies to the given cylinder and head.

        Args:
            cylinder: The cylinder number to check.
            head: The head number to check.

        Returns:
            True if the CH coordinates fall within this format's range.
        """
        return (self.track_start <= cylinder <= self.track_end and
                self.head_start <= head <= self.head_end)


@dataclass
class PhysicalFormat:
    """
    Encapsulates the complete physical geometry and properties of a disk.

    This class holds the overall dimensions of the disk and a list of
    TrackFormat objects that describe the layout of its tracks.

    Attributes:
        cylinders: The total number of cylinders.
        heads: The total number of heads.
        rpm: The rotational speed of the disk in revolutions per minute.
        heads_inverted: Whether the head numbering is physically inverted.
        bytes_per_sector: The default size of a sector in bytes.
        track_formats: A list of TrackFormat objects describing the disk's layout.
        image_in_sector_id_order: Whether a raw image is laid out by sector ID
                                  or by physical position on the track.
    """
    cylinders: int
    heads: int
    rpm: int
    heads_inverted: bool
    bytes_per_sector: int
    track_formats: List[TrackFormat]
    image_in_sector_id_order: bool = True

    def __post_init__(self) -> None:
        """
        Validates the provided track formats after initialization.

        Checks for overlaps and ensures all tracks are covered. Also propagates
        the default bytes_per_sector to any TrackFormat that doesn't define its own.
        """
        covered = set()
        for tf in self.track_formats:
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

    # --- Properties ---

    @property
    def has_variable_bps(self) -> bool:
        """
        Checks if any track format defines a different bytes_per_sector.

        Returns:
            True if sector sizes vary across the disk, False otherwise.
        """
        if not self.track_formats:
            return False
        first_bps = self.track_formats[0].bytes_per_sector
        return any(tf.bytes_per_sector != first_bps for tf in self.track_formats)

    @property
    def total_bytes(self) -> int:
        """
        Calculates the total storage capacity of the disk in bytes.

        Handles both uniform and variable sector sizes.

        Returns:
            The total capacity in bytes.
        """
        if self.has_variable_bps:
            total = 0
            for c in range(self.cylinders):
                for h in range(self.heads):
                    track_format = self.get_track_format(c, h)
                    total += track_format.sectors_per_track * track_format.bytes_per_sector
            return total
        return self.total_sectors * self.bytes_per_sector

    @property
    def total_sectors(self) -> int:
        """
        Calculates the total number of sectors on the disk.

        Returns:
            The total sector count.
        """
        total = 0
        for c in range(self.cylinders):
            for h in range(self.heads):
                total += self.get_sectors_per_track(c, h)
        return total

    # --- Public Methods ---

    @classmethod
    def create_default(cls) -> 'PhysicalFormat':
        """Creates a default PhysicalFormat instance (e.g., 1.44MB)."""
        default_track_format = TrackFormat(
            track_start=0, track_end=79, head_start=0, head_end=1,
            sectors_per_track=18, encoding="MFM", rate=500, interleave=1,
            id_start=1, iam_present=True, gap3_bytes=84
        )
        return cls(
            cylinders=80, heads=2, rpm=300, heads_inverted=False,
            bytes_per_sector=512, track_formats=[default_track_format]
        )

    def chs_to_byte_offset(self, cylinder: int, head: int, sector: int) -> int:
        """
        Converts CHS coordinates to a byte offset from the start of the disk image.

        This calculation uses the sector_translation_table to determine physical
        ordering when image_in_sector_id_order is False.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number (physical sector ID).

        Returns:
            The calculated byte offset.
        """
        self.validate_chs(cylinder, head, sector)
        byte_offset = 0

        # Sum bytes of all full cylinders before the target
        for c_iter in range(cylinder):
            for h_iter in range(self.heads):
                tf = self.get_track_format(c_iter, h_iter)
                byte_offset += tf.sectors_per_track * tf.bytes_per_sector

        # Sum bytes of all full heads on the target cylinder before the target
        for h_iter in range(head):
            tf = self.get_track_format(cylinder, h_iter)
            byte_offset += tf.sectors_per_track * tf.bytes_per_sector

        # Add the offset for the sectors on the target track
        tf = self.get_track_format(cylinder, head)

        if self.image_in_sector_id_order:
            # Image is ordered by sector ID, calculate directly
            sector_index = sector - tf.id_start
        else:
            # Image is in physical order, use translation table
            sector_index = tf.physical_to_logical_sector(sector)

        byte_offset += sector_index * tf.bytes_per_sector
        return byte_offset

    def chs_to_lba(self, cylinder: int, head: int, sector: int) -> int:
        """
        Converts CHS coordinates to a Logical Block Address (LBA).

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number (physical sector ID).

        Returns:
            The calculated LBA.

        Raises:
            NotImplementedError: If the disk has variable sector sizes.
        """
        if self.has_variable_bps:
            raise NotImplementedError("CHS to LBA is not supported for variable sector sizes.")
        self.validate_chs(cylinder, head, sector)

        lba = 0
        # Sum sectors of all full cylinders before the target
        for c_iter in range(cylinder):
            for h_iter in range(self.heads):
                lba += self.get_sectors_per_track(c_iter, h_iter)

        # Sum sectors of all full heads on the target cylinder before the target
        for h_iter in range(head):
            lba += self.get_sectors_per_track(cylinder, h_iter)

        tf = self.get_track_format(cylinder, head)
        lba += (sector - tf.id_start)
        return lba

    def get_bytes_per_sector(self, cylinder: int, head: int) -> int:
        """
        Gets the bytes per sector for a specific cylinder and head.

        Args:
            cylinder: The cylinder number.
            head: The head number.

        Returns:
            The sector size in bytes.
        """
        return self.get_track_format(cylinder, head).bytes_per_sector

    def get_physical_head(self, logical_head: int) -> int:
        """
        Maps a logical head number to a physical head number if heads are inverted.

        Args:
            logical_head: The logical head number (usually 0 or 1).

        Returns:
            The corresponding physical head number.
        """
        return 1 - logical_head if self.heads_inverted else logical_head

    def get_sectors_per_track(self, cylinder: int, head: int) -> int:
        """
        Gets the sectors per track for a specific cylinder and head.

        Args:
            cylinder: The cylinder number.
            head: The head number.

        Returns:
            The number of sectors on the specified track.
        """
        return self.get_track_format(cylinder, head).sectors_per_track

    def get_track_format(self, cylinder: int, head: int) -> TrackFormat:
        """
        Retrieves the TrackFormat for a specific cylinder and head.

        Args:
            cylinder: The cylinder number.
            head: The head number.

        Returns:
            The matching TrackFormat object.

        Raises:
            ValueError: If no applicable TrackFormat is found.
        """
        for tf in self.track_formats:
            if tf.matches(cylinder, head):
                return tf
        raise ValueError(f"No TrackFormat for cylinder {cylinder}, head {head}")

    def lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        """
        Converts a Logical Block Address (LBA) to CHS coordinates.

        Args:
            lba: The LBA to convert.

        Returns:
            A tuple containing the (cylinder, head, sector).

        Raises:
            ValueError: If the LBA is out of range.
            NotImplementedError: If the disk has variable sector sizes.
        """
        if self.has_variable_bps:
            raise NotImplementedError("LBA to CHS is not supported for variable sector sizes.")
        if not (0 <= lba < self.total_sectors):
            raise ValueError(f"LBA {lba} exceeds total sectors {self.total_sectors}")

        sector_count = 0
        for c in range(self.cylinders):
            for h in range(self.heads):
                tf = self.get_track_format(c, h)
                spt = tf.sectors_per_track
                if sector_count + spt > lba:
                    sector_offset = lba - sector_count
                    sector = tf.id_start + sector_offset
                    return c, h, sector
                sector_count += spt
        raise ValueError("LBA conversion failed unexpectedly.")

    def validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        """
        Validates that the given CHS coordinates are within the disk's bounds.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number (physical sector ID).

        Raises:
            ValueError: If any CHS value is out of range.
        """
        if not (0 <= cylinder < self.cylinders and 0 <= head < self.heads):
            raise ValueError(f"Invalid CHS: {cylinder}, {head}, {sector}")

        track_format = self.get_track_format(cylinder, head)
        max_sectors = track_format.sectors_per_track
        id_start = track_format.id_start

        if not (id_start <= sector < id_start + max_sectors):
            raise ValueError(
                f"Sector {sector} out of range ({id_start}-{id_start + max_sectors - 1}) "
                f"for C:{cylinder} H:{head}"
            )
