# src/fatfloppy/core/physical_formats_library.py
"""
Shared library of base physical disk formats.

These are pure physical geometries without filesystem-specific details.
Filesystem plugins import these and create variants with different
sector orderings (interleave, sector_translation_table) as needed.
"""

from .physical_format import PhysicalFormat, TrackFormat


def create_8inch_sssd_base(
    sectors_per_track: int = 26, bytes_per_sector: int = 128
) -> PhysicalFormat:
    """
    8" Single-Sided Single-Density base format.

    77 tracks, 1 head, variable sectors/track and bytes/sector, FM encoding.

    Args:
        sectors_per_track: Number of sectors per track (commonly 26).
        bytes_per_sector: Number of bytes per sector (commonly 128).

    Returns:
        PhysicalFormat instance with 8" SSSD geometry.
    """
    return PhysicalFormat(
        cylinders=77,
        heads=1,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=76,
                head_start=0,
                head_end=0,
                sectors_per_track=sectors_per_track,
                encoding="FM",
                rate=250,
                iam_present=True,
                gap3_bytes=26,
            )
        ],
    )


def create_8inch_dssd_base(
    sectors_per_track: int = 26, bytes_per_sector: int = 128
) -> PhysicalFormat:
    """
    8" Double-Sided Single-Density base format.

    77 tracks, 2 heads, variable sectors/track and bytes/sector, FM encoding.

    Args:
        sectors_per_track: Number of sectors per track (commonly 26).
        bytes_per_sector: Number of bytes per sector (commonly 128).

    Returns:
        PhysicalFormat instance with 8" DSSD geometry.
    """
    return PhysicalFormat(
        cylinders=77,
        heads=2,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=76,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="FM",
                rate=250,
                iam_present=True,
                gap3_bytes=26,
            )
        ],
    )


def create_8inch_ssdd_base(
    sectors_per_track: int = 26, bytes_per_sector: int = 256
) -> PhysicalFormat:
    """
    8" Single-Sided Double-Density base format.

    77 tracks, 1 head, variable sectors and sector size, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8, 15, or 26)
        bytes_per_sector: Sector size (commonly 256, 512, or 1024)

    Returns:
        PhysicalFormat instance with 8" SSDD geometry.
    """
    return PhysicalFormat(
        cylinders=77,
        heads=1,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=76,
                head_start=0,
                head_end=0,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=500,
                iam_present=True,
                gap3_bytes=54,
            )
        ],
    )


def create_8inch_dsdd_base(
    sectors_per_track: int = 26, bytes_per_sector: int = 256
) -> PhysicalFormat:
    """
    8" Double-Sided Double-Density base format.

    77 tracks, 2 heads, variable sectors and sector size, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8, 15, or 26)
        bytes_per_sector: Sector size (commonly 256, 512, or 1024)

    Returns:
        PhysicalFormat instance with 8" DSDD geometry.
    """
    return PhysicalFormat(
        cylinders=77,
        heads=2,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=76,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=500,
                iam_present=True,
                gap3_bytes=54,
            )
        ],
    )


def create_525_sssd_base(
    sectors_per_track: int = 10, bytes_per_sector: int = 256
) -> PhysicalFormat:
    """
    5.25" Single-Sided Single-Density base format.

    40 tracks, 1 head, variable sectors, FM encoding.
    Common for early systems like H17 hard-sectored drives.

    Args:
        sectors_per_track: Number of sectors (commonly 8, 9, or 10)
        bytes_per_sector: Sector size (commonly 256 or 512)

    Returns:
        PhysicalFormat instance with 5.25" SSSD geometry.
    """
    return PhysicalFormat(
        cylinders=40,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=39,
                head_start=0,
                head_end=0,
                sectors_per_track=sectors_per_track,
                encoding="FM",
                rate=250,
                iam_present=True,
                gap3_bytes=30,
            )
        ],
    )


def create_525_dssd_base(
    sectors_per_track: int = 10, bytes_per_sector: int = 256
) -> PhysicalFormat:
    """
    5.25" Double-Sided Single-Density base format.

    40 tracks, 2 heads, variable sectors, FM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8, 9, or 10)
        bytes_per_sector: Sector size (commonly 256 or 512)

    Returns:
        PhysicalFormat instance with 5.25" DSSD geometry.
    """
    return PhysicalFormat(
        cylinders=40,
        heads=2,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=39,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="FM",
                rate=250,
                iam_present=True,
                gap3_bytes=30,
            )
        ],
    )


def create_525_ssdd_base(
    sectors_per_track: int = 9, bytes_per_sector: int = 512
) -> PhysicalFormat:
    """
    5.25" Single-Sided Double-Density base format.

    40 tracks, 1 head, variable sectors, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8 or 9)
        bytes_per_sector: Sector size (typically 512)

    Returns:
        PhysicalFormat instance with 5.25" SSDD geometry.
    """
    return PhysicalFormat(
        cylinders=40,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=39,
                head_start=0,
                head_end=0,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=250,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def create_525_dsdd_base(
    sectors_per_track: int = 9, bytes_per_sector: int = 512
) -> PhysicalFormat:
    """
    5.25" Double-Sided Double-Density base format (360KB standard).

    40 tracks, 2 heads, variable sectors, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8 or 9)
        bytes_per_sector: Sector size (typically 512)

    Returns:
        PhysicalFormat instance with 5.25" DSDD geometry.
    """
    return PhysicalFormat(
        cylinders=40,
        heads=2,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=39,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=250,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def create_525_dshd_base(sectors_per_track: int = 15) -> PhysicalFormat:
    """
    5.25" Double-Sided High-Density base format (1.2MB standard).

    80 tracks, 2 heads, 15 sectors/track, 512 bytes/sector, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (typically 15)

    Returns:
        PhysicalFormat instance with 5.25" DSHD geometry.
    """
    return PhysicalFormat(
        cylinders=80,
        heads=2,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=79,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=500,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def create_35_ssdd_base(sectors_per_track: int = 9) -> PhysicalFormat:
    """
    3.5" Single-Sided Double-Density base format.

    80 tracks, 1 head, variable sectors, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8 or 9)

    Returns:
        PhysicalFormat instance with 3.5" SSDD geometry.
    """
    return PhysicalFormat(
        cylinders=80,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=79,
                head_start=0,
                head_end=0,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=250,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def create_35_dsdd_base(sectors_per_track: int = 9) -> PhysicalFormat:
    """
    3.5" Double-Sided Double-Density base format (720KB standard).

    80 tracks, 2 heads, variable sectors, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 8, 9, or 10)

    Returns:
        PhysicalFormat instance with 3.5" DSDD geometry.
    """
    return PhysicalFormat(
        cylinders=80,
        heads=2,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=79,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=250,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def create_35_dshd_base(
    sectors_per_track: int = 18, cylinders: int = 80
) -> PhysicalFormat:
    """
    3.5" Double-Sided High-Density base format (1.44MB standard).

    Variable tracks, 2 heads, variable sectors, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (commonly 18 or 21)
        cylinders: Number of cylinders (tracks per side), typically 80 or 82.

    Returns:
        PhysicalFormat instance with 3.5" DSHD geometry.
    """
    return PhysicalFormat(
        cylinders=cylinders,
        heads=2,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=cylinders - 1,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=500,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def create_35_dsed_base(sectors_per_track: int = 36) -> PhysicalFormat:
    """
    3.5" Double-Sided Extra-Density base format (2.88MB standard).

    80 tracks, 2 heads, 36 sectors/track, 512 bytes/sector, MFM encoding.

    Args:
        sectors_per_track: Number of sectors (typically 36)

    Returns:
        PhysicalFormat instance with 3.5" DSED geometry.
    """
    return PhysicalFormat(
        cylinders=80,
        heads=2,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=79,
                head_start=0,
                head_end=1,
                sectors_per_track=sectors_per_track,
                encoding="MFM",
                rate=1000,
                iam_present=True,
                gap3_bytes=41,
            )
        ],
    )
