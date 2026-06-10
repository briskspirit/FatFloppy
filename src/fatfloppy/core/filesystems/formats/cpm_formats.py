from dataclasses import replace

from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...physical_formats_library import (
    create_8inch_sssd_base,
    create_525_sssd_base,
)
from ..cpm_fs import CPMDiskParameterBlock

DPB_8INCH_SSSD = CPMDiskParameterBlock(
    spt=26,
    bsh=3,
    blm=7,
    exm=0,
    dsm=242,
    drm=63,
    al0=0xC0,
    al1=0x00,
    cks=0,
    off=2,
)

DPB_8INCH_SSDD_IMSAI = CPMDiskParameterBlock(
    spt=52,
    bsh=4,
    blm=15,
    exm=0,
    dsm=242,
    drm=63,
    al0=0xC0,
    al1=0x00,
    cks=0,
    off=2,
)

DPB_8INCH_MITS = CPMDiskParameterBlock(
    spt=32,
    bsh=4,
    blm=15,
    exm=0,
    dsm=147,
    drm=63,
    al0=0xC0,
    al1=0x00,
    cks=0,
    off=2,
)

# Altair 5.25" minifloppy CP/M (35 tracks x 16 sectors x 137-byte hard sectors).
# The disk stores 128-byte logical data behind the MITS 137-byte framing; the
# 2:1 Altair interleave is recovered as skew=2 at the CP/M layer.
DPB_525_MITS_MINI = CPMDiskParameterBlock(
    spt=16,
    bsh=3,
    blm=7,
    exm=0,
    dsm=61,
    drm=31,
    al0=0xC0,
    al1=0x00,
    cks=0,
    off=4,
    skew=2,
)

DPB_525_SSSD = CPMDiskParameterBlock(
    spt=20,
    bsh=3,
    blm=7,
    exm=0,
    # 37 data tracks x 20 logical sectors / 8 sectors-per-block = 92 blocks
    # (0..91), so the highest block number is 91. dsm=92 let the allocator hand
    # out a block that runs past the end of the disk (audit cpm_formats.py:55).
    dsm=91,
    drm=63,
    al0=0xC0,
    al1=0x00,
    cks=0,
    off=3,
)

MITS_SYSTEM_TRACK_END = 5
MITS_DATA_TRACK_START = 6
MITS_DATA_TRACK_END = 76
MITS_SECTORS_PER_TRACK = 32
MITS_SYSTEM_SECTOR_TRANSLATION = [
    1,
    9,
    17,
    25,
    3,
    11,
    19,
    27,
    5,
    13,
    21,
    29,
    7,
    15,
    23,
    31,
    2,
    10,
    18,
    26,
    4,
    12,
    20,
    28,
    6,
    14,
    22,
    30,
    8,
    16,
    24,
    32,
]
MITS_DATA_SECTOR_TRANSLATION = [
    1,
    9,
    17,
    25,
    3,
    11,
    19,
    27,
    5,
    13,
    21,
    29,
    7,
    15,
    23,
    31,
    18,
    26,
    2,
    10,
    20,
    28,
    4,
    12,
    22,
    30,
    6,
    14,
    24,
    32,
    8,
    16,
]


def _create_8inch_sssd_variants() -> list[tuple[str, PhysicalFormat]]:
    """
    Creates 8" SSSD physical format variants with different sector orderings.

    Returns:
        List of (variant_name, PhysicalFormat) tuples.
    """
    base = create_8inch_sssd_base()
    variants = []

    pf_i6 = replace(base)
    pf_i6.track_formats = [
        replace(base.track_formats[0], interleave=6, sector_translation_table=None)
    ]
    variants.append(("interleave6", pf_i6))

    pf_i4 = replace(base)
    pf_i4.track_formats = [
        replace(base.track_formats[0], interleave=4, sector_translation_table=None)
    ]
    variants.append(("interleave4", pf_i4))

    pf_seq = replace(base)
    pf_seq.track_formats = [
        replace(base.track_formats[0], interleave=1, sector_translation_table=None)
    ]
    variants.append(("sequential", pf_seq))

    return variants


def _create_8inch_ssdd_imsai_mixed() -> PhysicalFormat:
    """
    Creates IMSAI mixed-density format (Track 0 FM, Tracks 1-76 MFM).

    This is a special case where different tracks use different encodings.

    Returns:
        A PhysicalFormat object configured for IMSAI mixed density.
    """
    return PhysicalFormat(
        cylinders=77,
        heads=1,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=128,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=0,
                head_start=0,
                head_end=0,
                sectors_per_track=26,
                bytes_per_sector=128,
                encoding="FM",
                rate=250,
                interleave=6,
                iam_present=True,
                gap3_bytes=26,
            ),
            TrackFormat(
                track_start=1,
                track_end=76,
                head_start=0,
                head_end=0,
                sectors_per_track=26,
                bytes_per_sector=256,
                encoding="MFM",
                rate=500,
                interleave=9,
                iam_present=True,
                gap3_bytes=54,
            ),
        ],
    )


def _create_mits_altair_variants() -> list[tuple[str, PhysicalFormat]]:
    """
    Creates MITS Altair 8" SSSD variants with hard-sectored split interleave.

    The MITS/Altair used 32 hard sectors with a specific 2:1 split interleave
    pattern that differs between system tracks and data tracks.

    Returns:
        List of (variant_name, PhysicalFormat) tuples.
    """
    variants = []

    pf_mits = PhysicalFormat(
        cylinders=77,
        heads=1,
        rpm=360,
        heads_inverted=False,
        bytes_per_sector=128,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=MITS_SYSTEM_TRACK_END,
                head_start=0,
                head_end=0,
                sectors_per_track=MITS_SECTORS_PER_TRACK,
                bytes_per_sector=128,
                encoding="FM",
                rate=250,
                interleave=1,
                sector_translation_table=MITS_SYSTEM_SECTOR_TRANSLATION,
                iam_present=False,
                gap3_bytes=0,
            ),
            TrackFormat(
                track_start=MITS_DATA_TRACK_START,
                track_end=MITS_DATA_TRACK_END,
                head_start=0,
                head_end=0,
                sectors_per_track=MITS_SECTORS_PER_TRACK,
                bytes_per_sector=128,
                encoding="FM",
                rate=250,
                interleave=1,
                sector_translation_table=MITS_DATA_SECTOR_TRANSLATION,
                iam_present=False,
                gap3_bytes=0,
            ),
        ],
    )
    variants.append(("mits_split", pf_mits))

    return variants


CPM_FORMATS: dict[str, FormatProfile] = {}

for variant_name, physical_format in _create_8inch_sssd_variants():
    format_name = f"cpm_8_sssd_250k_{variant_name}"

    CPM_FORMATS[format_name] = FormatProfile(
        name=format_name,
        description=f'8" SSSD 250KB CP/M with {variant_name}',
        physical_format=physical_format,
        filesystem_config=DPB_8INCH_SSSD,
    )

CPM_FORMATS["cpm_8_sssd_250k"] = CPM_FORMATS["cpm_8_sssd_250k_interleave6"]

CPM_FORMATS["cpm_8_ssdd_imsai_mixed"] = FormatProfile(
    name="cpm_8_ssdd_imsai_mixed",
    description='8" SSDD IMSAI Mixed Density (T0 FM, T1-76 MFM, ~500KB)',
    physical_format=_create_8inch_ssdd_imsai_mixed(),
    filesystem_config=DPB_8INCH_SSDD_IMSAI,
)

for variant_name, physical_format in _create_mits_altair_variants():
    format_name = f"cpm_8_mits_altair_308k_{variant_name}"

    CPM_FORMATS[format_name] = FormatProfile(
        name=format_name,
        description=f'8" SSSD MITS Altair 308KB with {variant_name}',
        physical_format=physical_format,
        filesystem_config=DPB_8INCH_MITS,
    )

CPM_FORMATS["cpm_8_mits_dsk_308k"] = CPM_FORMATS["cpm_8_mits_altair_308k_mits_split"]

# Altair 5.25" minifloppy: 35 tracks, 1 head, 16 logical 128-byte sectors. The
# MITS driver de-frames the 137-byte hard sectors and exposes them in physical
# order; the CP/M layer recovers the 2:1 interleave (skew=2 in the DPB).
pf_mits_mini = PhysicalFormat(
    cylinders=35,
    heads=1,
    rpm=360,
    heads_inverted=False,
    bytes_per_sector=128,
    track_formats=[
        TrackFormat(
            track_start=0,
            track_end=34,
            head_start=0,
            head_end=0,
            sectors_per_track=16,
            bytes_per_sector=128,
            encoding="FM",
            rate=250,
            interleave=1,
            iam_present=False,
            gap3_bytes=0,
        )
    ],
)
CPM_FORMATS["cpm_5.25_mits_mini_70k"] = FormatProfile(
    name="cpm_5.25_mits_mini_70k",
    description='MITS Altair 5.25" minifloppy CP/M (35t/1h/16s, 137-byte sectors)',
    physical_format=pf_mits_mini,
    filesystem_config=DPB_525_MITS_MINI,
)

pf_525 = create_525_sssd_base(sectors_per_track=10, bytes_per_sector=256)
pf_525.track_formats = [
    replace(
        pf_525.track_formats[0],
        interleave=4,
        # Drop the inherited (interleave=1) translation table so __post_init__
        # rebuilds it from interleave=4 (audit cpm_formats.py:302).
        sector_translation_table=None,
        gap1_bytes=45,
        gap2_bytes=12,
        gap3_bytes=30,
    )
]

CPM_FORMATS["cpm_5.25_100k"] = FormatProfile(
    name="cpm_5.25_100k",
    description='5.25" SSSD 100KB CP/M 2.2 (40 tracks, 1 head, 10 sectors/track)',
    physical_format=pf_525,
    filesystem_config=DPB_525_SSSD,
)
