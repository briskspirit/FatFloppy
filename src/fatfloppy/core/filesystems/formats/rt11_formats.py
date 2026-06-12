"""Format profiles for DEC RT-11 floppy volumes.

Two families of profiles:

- Raw physical-order profiles (``rt11_rx01``/``rt11_rx02``/``rt11_rx50``):
  the image stores physical sectors in track order and the DEC handler
  interleave (see rt11_layout) spreads the 512-byte logical blocks across
  them. The profile supplies the real drive geometry; the filesystem
  resolves the actual view (physical vs logical order) by scoring the
  directory structure, so a logical-order image that happens to be exactly
  raw-RX02-sized still opens correctly under ``rt11_rx02``.

- Logical-block-order profiles (``rt11_logical_*``): archives frequently
  store RT-11 volumes as plain logical block streams whose sizes match no
  raw geometry (track 0 stripped, or the wrapped tail included). These use
  a synthetic uniform geometry of one 512-byte sector per track and one
  head, ``cylinders == total_blocks``, which makes LBA == logical block ==
  byte offset / 512. The raw IMG driver handles any uniform geometry, so
  this is purely a 1:1 byte container.
"""

from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat
from ...physical_formats_library import (
    create_8inch_ssdd_base,
    create_8inch_sssd_base,
)
from ...rt11_layout import VIEW_GEOMETRY, RT11Config


def _rx50_base() -> PhysicalFormat:
    """RX50: 5.25" single-sided quad-density, 80 tracks x 10 x 512, MFM
    250 kbps at 300 rpm (no library base exists for this shape)."""
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
                sectors_per_track=10,
                encoding="MFM",
                rate=250,
                interleave=1,
                iam_present=True,
                gap3_bytes=84,
            )
        ],
    )


def _logical_base(total_blocks: int) -> PhysicalFormat:
    """Synthetic uniform geometry for logical-block-order images:
    ``total_blocks`` cylinders x 1 head x 1 sector x 512 bytes."""
    return PhysicalFormat(
        cylinders=total_blocks,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[
            TrackFormat(
                track_start=0,
                track_end=total_blocks - 1,
                head_start=0,
                head_end=0,
                sectors_per_track=1,
                encoding="MFM",
                rate=250,
                interleave=1,
            )
        ],
    )


RT11_FORMATS: dict[str, FormatProfile] = {
    "rt11_rx01": FormatProfile(
        name="rt11_rx01",
        description='8" RX01 RT-11 (77x26x128 raw, 494 blocks)',
        physical_format=create_8inch_sssd_base(),
        filesystem_config=RT11Config(
            view="rx01", total_blocks=VIEW_GEOMETRY["rx01"].total_blocks
        ),
        notes=(
            "Raw physical sector order; the DEC DY.MAC 2:1 interleave with "
            "6-sector track skew maps blocks, physical track 0 unused."
        ),
    ),
    "rt11_rx02": FormatProfile(
        name="rt11_rx02",
        description='8" RX02 RT-11 (77x26x256 raw, 988 blocks)',
        physical_format=create_8inch_ssdd_base(),
        filesystem_config=RT11Config(
            view="rx02", total_blocks=VIEW_GEOMETRY["rx02"].total_blocks
        ),
        notes=(
            "Raw physical sector order, same interleave scheme as RX01. "
            "Logical-order images of the identical 512,512-byte size also "
            "open here; the filesystem resolves the view from the contents."
        ),
    ),
    "rt11_rx50": FormatProfile(
        name="rt11_rx50",
        description='5.25" RX50 RT-11 (80x10x512 raw, 800 blocks)',
        physical_format=_rx50_base(),
        filesystem_config=RT11Config(
            view="rx50", total_blocks=VIEW_GEOMETRY["rx50"].total_blocks
        ),
        notes=(
            "Raw physical sector order; P/OS DZDRV.MAC 2:1 interleave with "
            "2-sector track skew and track-0 wraparound (all 80 tracks used)."
        ),
    ),
    "rt11_logical_494": FormatProfile(
        name="rt11_logical_494",
        description="RT-11 logical block image, 494 blocks (RX01 sans track 0)",
        physical_format=_logical_base(494),
        filesystem_config=RT11Config(view="logical", total_blocks=494),
        notes=(
            "Synthetic uniform geometry (494 cyl x 1 head x 1 spt x 512): "
            "LBA == logical block. 252,928-byte logical RX01 dumps."
        ),
    ),
    "rt11_logical_500": FormatProfile(
        name="rt11_logical_500",
        description="RT-11 logical block image, 500 blocks (RX01 incl. track 0)",
        physical_format=_logical_base(500),
        filesystem_config=RT11Config(view="logical", total_blocks=500),
        notes=(
            "Synthetic uniform geometry (500 cyl x 1 head x 1 spt x 512). "
            "256,000-byte logical RX01 dumps that keep the wrapped track-0 "
            "tail (filesystem capacity is still 494 blocks)."
        ),
    ),
    "rt11_logical_800": FormatProfile(
        name="rt11_logical_800",
        description="RT-11 logical block image, 800 blocks (RX50)",
        physical_format=_logical_base(800),
        filesystem_config=RT11Config(view="logical", total_blocks=800),
        notes=(
            "Synthetic uniform geometry (800 cyl x 1 head x 1 spt x 512). "
            "409,600-byte logical RX50 dumps; raw physical RX50 dumps of "
            "the same size open under rt11_rx50 via view resolution."
        ),
    ),
    "rt11_logical_988": FormatProfile(
        name="rt11_logical_988",
        description="RT-11 logical block image, 988 blocks (RX02 sans track 0)",
        physical_format=_logical_base(988),
        filesystem_config=RT11Config(view="logical", total_blocks=988),
        notes=(
            "Synthetic uniform geometry (988 cyl x 1 head x 1 spt x 512): "
            "505,856-byte logical RX02 dumps."
        ),
    ),
}
