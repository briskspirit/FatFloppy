# src/fatfloppy/core/filesystems/formats/hdos_formats.py
"""HDOS format definitions using shared physical formats."""

from typing import Dict
from dataclasses import replace
from ...physical_formats_library import (
    create_525_sssd_base,
    create_525_dssd_base,
    create_8inch_dsdd_base,
)
from ...format_profile import FormatProfile
from ..hdos_fs import HDOSLabelRecord


# HDOS formats use hardware-specific controllers with fixed layouts
# H17: 5.25" SSSD with specific gap values (hard-sectored)
# H37: 5.25" DSSD (hard-sectored)
# H47: 8" DSDD

HDOS_FORMATS: Dict[str, FormatProfile] = {}

# H17 format (5.25" SSSD)
pf_h17 = create_525_sssd_base(sectors_per_track=10, bytes_per_sector=256)
pf_h17.track_formats = [replace(
    pf_h17.track_formats[0],
    interleave=1,  # Sequential for H17
    gap1_bytes=45,  # H17 hardware-specific
    gap2_bytes=12,
    gap3_bytes=30,
)]

HDOS_FORMATS["hdos_5.25_100k"] = FormatProfile(
    name="hdos_5.25_100k",
    description="5.25\" SSSD 100KB HDOS 2.0 (H17 format: 40 tracks, 1 head, 10 sectors/track)",
    physical_format=pf_h17,
    filesystem_config=HDOSLabelRecord(
        title="HDOS 2.0 DISK",
        volume_number=1,
        cluster_factor=2,
        dir_start_block=130,
        grt_start_block=148
    ),
    notes="H17 hard-sectored controller. Gap values are hardware-specific.",
)

# H37 format (5.25" DSSD)
pf_h37 = create_525_dssd_base(sectors_per_track=10, bytes_per_sector=256)
pf_h37.track_formats = [replace(
    pf_h37.track_formats[0],
    interleave=1,
    gap1_bytes=45,
    gap2_bytes=12,
    gap3_bytes=30,
)]

HDOS_FORMATS["hdos_5.25_200k_dssd"] = FormatProfile(
    name="hdos_5.25_200k_dssd",
    description="5.25\" DSSD 200KB HDOS 3.0 (H37 format: 40 tracks, 2 heads, 10 sectors/track)",
    physical_format=pf_h37,
    filesystem_config=HDOSLabelRecord(
        title="HDOS 3.0 DISK",
        volume_number=1,
        cluster_factor=4,
        dir_start_block=130,
        grt_start_block=148
    ),
    notes="H37 double-sided hard-sectored controller.",
)

# H47 format (8" DSDD)
pf_h47 = create_8inch_dsdd_base(sectors_per_track=26, bytes_per_sector=256)
pf_h47.track_formats = [replace(pf_h47.track_formats[0], interleave=1)]

HDOS_FORMATS["hdos_8_1m_dsdd"] = FormatProfile(
    name="hdos_8_1m_dsdd",
    description="8\" DSDD 1MB HDOS 3.0 (H47 format: 77 tracks, 2 heads, 26 sectors/track)",
    physical_format=pf_h47,
    filesystem_config=HDOSLabelRecord(
        title="HDOS 3.0 DISK",
        volume_number=1,
        cluster_factor=16,
        dir_start_block=130,
        grt_start_block=148
    ),
    notes="H47 8-inch double-density controller.",
)
