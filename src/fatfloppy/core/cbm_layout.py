"""
Shared geometry/layout definitions for Commodore CBM DOS disks.

Neutral core module (like format_profile.py): both the CBM image driver and
the CBM filesystem import from here, keeping the driver layer independent of
the filesystem layer. CBM tracks are 1-based; FatFloppy cylinders are 0-based
(cylinder = track - 1, always head 0).
"""

from dataclasses import dataclass, field
from typing import Optional

from .physical_format import PhysicalFormat, TrackFormat

CBM_BYTES_PER_SECTOR = 256

# (first_track, last_track, sectors_per_track), tracks 1-based inclusive.
ZONES_D64_35 = ((1, 17, 21), (18, 24, 19), (25, 30, 18), (31, 35, 17))
ZONES_D64_40 = ZONES_D64_35 + ((36, 40, 17),)
ZONES_D64_42 = ZONES_D64_35 + ((36, 42, 17),)
ZONES_D71 = ZONES_D64_35 + ((36, 52, 21), (53, 59, 19), (60, 65, 18), (66, 70, 17))
ZONES_D81 = ((1, 80, 40),)

# file size -> (variant family, track count, has trailing error-byte block)
CBM_SIZE_TABLE: dict[int, tuple[str, int, bool]] = {
    174848: ("D64", 35, False),
    175531: ("D64", 35, True),
    196608: ("D64", 40, False),
    197376: ("D64", 40, True),
    205312: ("D64", 42, False),
    206114: ("D64", 42, True),
    349696: ("D71", 70, False),
    351062: ("D71", 70, True),
    819200: ("D81", 80, False),
    822400: ("D81", 80, True),
}

# Sizes that collide with other formats (raw IMG, Mac/Atari 800K) and need a
# content probe before the CBM driver may claim the file.
CBM_AMBIGUOUS_SIZES = frozenset({196608, 819200})

_ZONE_MAP = {
    ("D64", 35): ZONES_D64_35,
    ("D64", 40): ZONES_D64_40,
    ("D64", 42): ZONES_D64_42,
    ("D71", 70): ZONES_D71,
    ("D81", 80): ZONES_D81,
}


@dataclass
class CBMDiskLayout:
    """Filesystem-level layout parameters for one CBM DOS variant."""

    variant: str  # "1541" | "1571" | "1581"
    tracks: int
    zones: tuple
    dir_track: int
    dir_sector: int
    interleave: int
    dir_interleave: int
    reserved_tracks: tuple
    max_dir_entries: int
    error_bytes: bool = False
    # _offsets is computed in __post_init__, never supplied by callers.
    _offsets: list = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        offsets, total = [0], 0
        for t in range(1, self.tracks + 1):
            total += self._zone_spt(t)
            offsets.append(total)
        self._offsets = offsets

    def _zone_spt(self, track: int) -> int:
        for first, last, spt in self.zones:
            if first <= track <= last:
                return spt
        raise ValueError(f"Track {track} out of range 1-{self.tracks}")

    def spt(self, track: int) -> int:
        if not 1 <= track <= self.tracks:
            raise ValueError(f"Track {track} out of range 1-{self.tracks}")
        return self._zone_spt(track)

    def sectors_before(self, track: int) -> int:
        if not 1 <= track <= self.tracks:
            raise ValueError(f"Track {track} out of range 1-{self.tracks}")
        return self._offsets[track - 1]

    @property
    def total_sectors(self) -> int:
        return self._offsets[self.tracks]

    def linear_index(self, track: int, sector: int) -> int:
        if not 0 <= sector < self.spt(track):
            raise ValueError(f"Sector {sector} out of range on track {track}")
        return self.sectors_before(track) + sector

    @staticmethod
    def matches(config1, config2) -> bool:
        return (
            isinstance(config1, CBMDiskLayout)
            and isinstance(config2, CBMDiskLayout)
            and config1.variant == config2.variant
            and config1.tracks == config2.tracks
        )

    @staticmethod
    def infer_from_geometry(pf: PhysicalFormat) -> Optional["CBMDiskLayout"]:
        """Maps a disk geometry back to a CBM layout, or None if not CBM-shaped."""
        if pf is None or pf.heads != 1 or pf.bytes_per_sector != CBM_BYTES_PER_SECTOR:
            return None
        for (family, tracks), _zones in _ZONE_MAP.items():
            if pf.cylinders != tracks:
                continue
            candidate = layout_for_variant(family, tracks)
            try:
                if all(
                    pf.get_sectors_per_track(t - 1, 0) == candidate.spt(t)
                    for t in (1, tracks)
                ):
                    return candidate
            except Exception:
                continue
        return None


def layout_for_variant(family: str, tracks: int) -> CBMDiskLayout:
    zones = _ZONE_MAP[(family, tracks)]
    if family == "D64":
        return CBMDiskLayout(
            variant="1541",
            tracks=tracks,
            zones=zones,
            dir_track=18,
            dir_sector=1,
            interleave=10,
            dir_interleave=3,
            reserved_tracks=(18,),
            max_dir_entries=144,
        )
    if family == "D71":
        return CBMDiskLayout(
            variant="1571",
            tracks=tracks,
            zones=zones,
            dir_track=18,
            dir_sector=1,
            interleave=6,
            dir_interleave=6,
            reserved_tracks=(18, 53),
            max_dir_entries=144,
        )
    return CBMDiskLayout(
        variant="1581",
        tracks=tracks,
        zones=zones,
        dir_track=40,
        dir_sector=3,
        interleave=1,
        dir_interleave=1,
        reserved_tracks=(40,),
        max_dir_entries=296,
    )


def build_physical_format(family: str, tracks: int) -> PhysicalFormat:
    """Logical CBM geometry: cylinders = CBM tracks, 1 head, 256-byte sectors.

    CBM sector IDs are 0-based (sectors 0..N-1 on each track), so id_start=0.
    """
    zones = _ZONE_MAP[(family, tracks)]
    encoding = "MFM" if family == "D81" else "GCR"
    track_formats = [
        TrackFormat(
            track_start=first - 1,
            track_end=last - 1,
            head_start=0,
            head_end=0,
            sectors_per_track=spt,
            encoding=encoding,
            rate=250,
            interleave=1,
            bytes_per_sector=CBM_BYTES_PER_SECTOR,
            id_start=0,
            iam_present=False,
        )
        for first, last, spt in zones
    ]
    return PhysicalFormat(
        cylinders=tracks,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=CBM_BYTES_PER_SECTOR,
        track_formats=track_formats,
    )
