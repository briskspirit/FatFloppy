from dataclasses import replace

from ...format_profile import FormatProfile
from ...physical_formats_library import (
    create_8inch_dsdd_base,
    create_525_dssd_base,
    create_525_sssd_base,
)
from ..hdos_fs import HDOSLabelRecord

H17_SECTORS_PER_TRACK = 10
H17_BYTES_PER_SECTOR = 256
H17_INTERLEAVE = 1
H17_GAP1_BYTES = 45
H17_GAP2_BYTES = 12
H17_GAP3_BYTES = 30
H17_CLUSTER_FACTOR = 2

H37_SECTORS_PER_TRACK = 10
H37_BYTES_PER_SECTOR = 256
H37_INTERLEAVE = 1
H37_GAP1_BYTES = 45
H37_GAP2_BYTES = 12
H37_GAP3_BYTES = 30
H37_CLUSTER_FACTOR = 4

H47_SECTORS_PER_TRACK = 26
H47_BYTES_PER_SECTOR = 256
H47_INTERLEAVE = 1
H47_CLUSTER_FACTOR = 16

HDOS_DEFAULT_VOLUME_NUMBER = 1
HDOS_DEFAULT_DIR_START_BLOCK = 130
HDOS_DEFAULT_GRT_START_BLOCK = 148

CYLINDERS_525 = 40
CYLINDERS_8INCH = 77


HDOS_FORMATS: dict[str, FormatProfile] = {}

pf_h17 = create_525_sssd_base(
    sectors_per_track=H17_SECTORS_PER_TRACK, bytes_per_sector=H17_BYTES_PER_SECTOR
)
pf_h17.track_formats = [
    replace(
        pf_h17.track_formats[0],
        interleave=H17_INTERLEAVE,
        gap1_bytes=H17_GAP1_BYTES,
        gap2_bytes=H17_GAP2_BYTES,
        gap3_bytes=H17_GAP3_BYTES,
    )
]

HDOS_FORMATS["hdos_5.25_100k"] = FormatProfile(
    name="hdos_5.25_100k",
    description=(
        '5.25" SSSD 100KB HDOS 2.0 (H17 format: 40 tracks, 1 head, 10 sectors/track)'
    ),
    physical_format=pf_h17,
    filesystem_config=HDOSLabelRecord(
        title="HDOS 2.0 DISK",
        volume_number=HDOS_DEFAULT_VOLUME_NUMBER,
        cluster_factor=H17_CLUSTER_FACTOR,
        dir_start_block=HDOS_DEFAULT_DIR_START_BLOCK,
        grt_start_block=HDOS_DEFAULT_GRT_START_BLOCK,
    ),
    notes="H17 hard-sectored controller. Gap values are hardware-specific.",
)

pf_h37 = create_525_dssd_base(
    sectors_per_track=H37_SECTORS_PER_TRACK, bytes_per_sector=H37_BYTES_PER_SECTOR
)
pf_h37.track_formats = [
    replace(
        pf_h37.track_formats[0],
        interleave=H37_INTERLEAVE,
        gap1_bytes=H37_GAP1_BYTES,
        gap2_bytes=H37_GAP2_BYTES,
        gap3_bytes=H37_GAP3_BYTES,
    )
]

HDOS_FORMATS["hdos_5.25_200k_dssd"] = FormatProfile(
    name="hdos_5.25_200k_dssd",
    description=(
        '5.25" DSSD 200KB HDOS 3.0 (H37 format: 40 tracks, 2 heads, 10 sectors/track)'
    ),
    physical_format=pf_h37,
    filesystem_config=HDOSLabelRecord(
        title="HDOS 3.0 DISK",
        volume_number=HDOS_DEFAULT_VOLUME_NUMBER,
        cluster_factor=H37_CLUSTER_FACTOR,
        dir_start_block=HDOS_DEFAULT_DIR_START_BLOCK,
        grt_start_block=HDOS_DEFAULT_GRT_START_BLOCK,
    ),
    notes="H37 double-sided hard-sectored controller.",
)

pf_h47 = create_8inch_dsdd_base(
    sectors_per_track=H47_SECTORS_PER_TRACK, bytes_per_sector=H47_BYTES_PER_SECTOR
)
pf_h47.track_formats = [replace(pf_h47.track_formats[0], interleave=H47_INTERLEAVE)]

HDOS_FORMATS["hdos_8_1m_dsdd"] = FormatProfile(
    name="hdos_8_1m_dsdd",
    description=(
        '8" DSDD 1MB HDOS 3.0 (H47 format: 77 tracks, 2 heads, 26 sectors/track)'
    ),
    physical_format=pf_h47,
    filesystem_config=HDOSLabelRecord(
        title="HDOS 3.0 DISK",
        volume_number=HDOS_DEFAULT_VOLUME_NUMBER,
        cluster_factor=H47_CLUSTER_FACTOR,
        dir_start_block=HDOS_DEFAULT_DIR_START_BLOCK,
        grt_start_block=HDOS_DEFAULT_GRT_START_BLOCK,
    ),
    notes="H47 8-inch double-density controller.",
)
