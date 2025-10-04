# src/fatfloppy/core/filesystems/formats/fat12_formats.py
"""FAT12 format definitions using shared physical formats."""

from typing import Dict
from dataclasses import replace
from ...physical_formats_library import (
    create_35_dshd_base,
    create_35_dsdd_base,
    create_35_ssdd_base,
    create_35_dsed_base,
    create_525_dsdd_base,
    create_525_ssdd_base,
    create_525_dshd_base,
    create_8inch_sssd_base,
    create_8inch_dssd_base,
    create_8inch_ssdd_base,
    create_8inch_dsdd_base,
)
from ...format_profile import FormatProfile
from ..fat12_fs import FATVolumeInfo


FAT12_FORMATS: Dict[str, FormatProfile] = {}

# ============================================================================
# 3.5" Floppy Formats
# ============================================================================

# 3.5" 1.44M
pf_144 = create_35_dshd_base(sectors_per_track=18)
FAT12_FORMATS["ibm_3.5_1.44m"] = FormatProfile(
    name="ibm_3.5_1.44m",
    description="3.5\" HD 1.44MB (80 tracks, 2 heads, 18 sectors)",
    physical_format=pf_144,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=18,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=2880,
        media_descriptor=0xF0,
        root_entries=224,
        sectors_per_fat=9,
        sectors_per_cluster=1,
    ),
)

# 3.5" 720K
pf_720 = create_35_dsdd_base(sectors_per_track=9)
FAT12_FORMATS["ibm_3.5_720k"] = FormatProfile(
    name="ibm_3.5_720k",
    description="3.5\" DD 720KB (80 tracks, 2 heads, 9 sectors)",
    physical_format=pf_720,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=9,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=1440,
        media_descriptor=0xF9,
        root_entries=112,
        sectors_per_fat=3,
        sectors_per_cluster=2,
    ),
)

# 3.5" 320K
pf_35_320 = create_35_ssdd_base(sectors_per_track=8)
FAT12_FORMATS["ibm_3.5_320k"] = FormatProfile(
    name="ibm_3.5_320k",
    description="3.5\" DD 320KB (80 tracks, 1 head, 8 sectors)",
    physical_format=pf_35_320,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=512,
        num_heads=1,
        total_sectors=640,
        media_descriptor=0xFF,
        root_entries=112,
        sectors_per_fat=1,
        sectors_per_cluster=2,
    ),
)

# 3.5" 360K
pf_35_360 = create_35_ssdd_base(sectors_per_track=9)
FAT12_FORMATS["ibm_3.5_360k"] = FormatProfile(
    name="ibm_3.5_360k",
    description="3.5\" DD 360KB (80 tracks, 1 head, 9 sectors)",
    physical_format=pf_35_360,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=9,
        bytes_per_sector=512,
        num_heads=1,
        total_sectors=720,
        media_descriptor=0xFC,
        root_entries=112,
        sectors_per_fat=2,
        sectors_per_cluster=2,
    ),
)

# 3.5" 640K
pf_35_640 = create_35_dsdd_base(sectors_per_track=8)
FAT12_FORMATS["ibm_3.5_640k"] = FormatProfile(
    name="ibm_3.5_640k",
    description="3.5\" DD 640KB (80 tracks, 2 heads, 8 sectors)",
    physical_format=pf_35_640,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=1280,
        media_descriptor=0xFF,
        root_entries=112,
        sectors_per_fat=2,
        sectors_per_cluster=2,
    ),
)

# 3.5" 800K
pf_35_800 = create_35_dsdd_base(sectors_per_track=10)
FAT12_FORMATS["ibm_3.5_800k"] = FormatProfile(
    name="ibm_3.5_800k",
    description="3.5\" DD 800KB (80 tracks, 2 heads, 10 sectors)",
    physical_format=pf_35_800,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=10,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=1600,
        media_descriptor=0xF9,
        root_entries=112,
        sectors_per_fat=3,
        sectors_per_cluster=2,
    ),
)

# 3.5" 1.68M
pf_35_168 = create_35_dshd_base(sectors_per_track=21)
pf_35_168.track_formats = [replace(pf_35_168.track_formats[0], interleave=2)]
FAT12_FORMATS["ibm_3.5_1.68m"] = FormatProfile(
    name="ibm_3.5_1.68m",
    description="3.5\" HD 1.68MB (80 tracks, 2 heads, 21 sectors)",
    physical_format=pf_35_168,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=21,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=3360,
        media_descriptor=0xF0,
        root_entries=16,
        sectors_per_fat=5,
        sectors_per_cluster=2,
    ),
)

# 3.5" 1.72M
pf_35_172 = create_35_dshd_base(sectors_per_track=21, cylinders=82)
pf_35_172.track_formats = [replace(pf_35_172.track_formats[0], interleave=2)]
FAT12_FORMATS["ibm_3.5_1.72m"] = FormatProfile(
    name="ibm_3.5_1.72m",
    description="3.5\" HD 1.72MB (82 tracks, 2 heads, 21 sectors)",
    physical_format=pf_35_172,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=21,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=3444,
        media_descriptor=0xF0,
        root_entries=16,
        sectors_per_fat=6,
        sectors_per_cluster=2,
    ),
)

# 3.5" 2.88M
pf_288 = create_35_dsed_base(sectors_per_track=36)
FAT12_FORMATS["ibm_3.5_2.88m"] = FormatProfile(
    name="ibm_3.5_2.88m",
    description="3.5\" ED 2.88MB (80 tracks, 2 heads, 36 sectors)",
    physical_format=pf_288,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=36,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=5760,
        reserved_sectors=2,
        media_descriptor=0xF0,
        root_entries=224,
        sectors_per_fat=9,
        sectors_per_cluster=2,
    ),
)


# ============================================================================
# 5.25" Floppy Formats
# ============================================================================

# 5.25" 160K
pf_525_160 = create_525_ssdd_base(sectors_per_track=8)
FAT12_FORMATS["ibm_5.25_160k"] = FormatProfile(
    name="ibm_5.25_160k",
    description="5.25\" DD 160KB (40 tracks, 1 head, 8 sectors)",
    physical_format=pf_525_160,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=512,
        num_heads=1,
        total_sectors=320,
        media_descriptor=0xFE,
        root_entries=64,
        sectors_per_fat=1,
        sectors_per_cluster=1,
    ),
)

# 5.25" 180K
pf_525_180 = create_525_ssdd_base(sectors_per_track=9)
FAT12_FORMATS["ibm_5.25_180k"] = FormatProfile(
    name="ibm_5.25_180k",
    description="5.25\" DD 180KB (40 tracks, 1 head, 9 sectors)",
    physical_format=pf_525_180,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=9,
        bytes_per_sector=512,
        num_heads=1,
        total_sectors=360,
        media_descriptor=0xFC,
        root_entries=64,
        sectors_per_fat=2,
        sectors_per_cluster=1,
    ),
)

# 5.25" 320K
pf_525_320 = create_525_dsdd_base(sectors_per_track=8)
FAT12_FORMATS["ibm_5.25_320k"] = FormatProfile(
    name="ibm_5.25_320k",
    description="5.25\" DD 320KB (40 tracks, 2 heads, 8 sectors)",
    physical_format=pf_525_320,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=640,
        media_descriptor=0xFF,
        root_entries=112,
        sectors_per_fat=1,
        sectors_per_cluster=2,
    ),
)

# 5.25" 360K
pf_525_360 = create_525_dsdd_base(sectors_per_track=9)
FAT12_FORMATS["ibm_5.25_360k"] = FormatProfile(
    name="ibm_5.25_360k",
    description="5.25\" DD 360KB (40 tracks, 2 heads, 9 sectors)",
    physical_format=pf_525_360,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=9,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=720,
        media_descriptor=0xFD,
        root_entries=112,
        sectors_per_fat=2,
        sectors_per_cluster=2,
    ),
)

# 5.25" 1.2M
pf_12 = create_525_dshd_base(sectors_per_track=15)
FAT12_FORMATS["ibm_5.25_1.2m"] = FormatProfile(
    name="ibm_5.25_1.2m",
    description="5.25\" HD 1.2MB (80 tracks, 2 heads, 15 sectors)",
    physical_format=pf_12,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=15,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=2400,
        media_descriptor=0xF9,
        root_entries=224,
        sectors_per_fat=7,
        sectors_per_cluster=1,
    ),
)

# ============================================================================
# 8" Floppy Formats
# ============================================================================

# 8" 250K
pf_8_250 = create_8inch_sssd_base()
FAT12_FORMATS["ibm_8_250k"] = FormatProfile(
    name="ibm_8_250k",
    description="8\" SD 250KB (77 tracks, 1 head, 26 sectors)",
    physical_format=pf_8_250,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=26,
        bytes_per_sector=128,
        num_heads=1,
        total_sectors=2002,
        media_descriptor=0xFE,
        root_entries=68,
        sectors_per_fat=6,
        sectors_per_cluster=4,
    ),
)

# 8" 298k
pf_8_298 = create_8inch_sssd_base(sectors_per_track=15, bytes_per_sector=256)
FAT12_FORMATS["ibm_8_298k"] = FormatProfile(
    name="ibm_8_298k",
    description="8\" SD 298KB (77 tracks, 1 head, 15 sectors)",
    physical_format=pf_8_298,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=15,
        bytes_per_sector=256,
        num_heads=1,
        total_sectors=1155,
        media_descriptor=0xFE,
        root_entries=56,
        sectors_per_fat=4,
        sectors_per_cluster=2,
    ),
)

# 8" 315k
pf_8_315 = create_8inch_sssd_base(sectors_per_track=8, bytes_per_sector=512)
FAT12_FORMATS["ibm_8_315k"] = FormatProfile(
    name="ibm_8_315k",
    description="8\" SD 315KB (77 tracks, 1 head, 8 sectors)",
    physical_format=pf_8_315,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=512,
        num_heads=1,
        total_sectors=616,
        media_descriptor=0xFE,
        root_entries=64,
        sectors_per_fat=2,
        sectors_per_cluster=1,
    ),
)

# 8" 500K
pf_8_500 = create_8inch_dssd_base()
FAT12_FORMATS["ibm_8_500k"] = FormatProfile(
    name="ibm_8_500k",
    description="8\" SD 500KB (77 tracks, 2 heads, 26 sectors)",
    physical_format=pf_8_500,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=26,
        bytes_per_sector=128,
        num_heads=2,
        total_sectors=4004,
        reserved_sectors=2,
        media_descriptor=0xFD,
        root_entries=96,
        sectors_per_fat=12,
        sectors_per_cluster=4,
    ),
)

# 8" 590k
pf_8_590 = create_8inch_dssd_base(sectors_per_track=15, bytes_per_sector=256)
FAT12_FORMATS["ibm_8_590k"] = FormatProfile(
    name="ibm_8_590k",
    description="8\" SD 590KB (77 tracks, 2 heads, 15 sectors)",
    physical_format=pf_8_590,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=15,
        bytes_per_sector=256,
        num_heads=2,
        total_sectors=2310,
        reserved_sectors=2,
        media_descriptor=0xFD,
        root_entries=96,
        sectors_per_fat=7,
        sectors_per_cluster=2,
    ),
)

# 8" 630k
pf_8_630 = create_8inch_ssdd_base(sectors_per_track=8, bytes_per_sector=1024)
FAT12_FORMATS["ibm_8_630k"] = FormatProfile(
    name="ibm_8_630k",
    description="8\" DD 630KB (77 tracks, 1 head, 8 sectors)",
    physical_format=pf_8_630,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=1024,
        num_heads=1,
        total_sectors=616,
        media_descriptor=0x00,
        root_entries=96,
        sectors_per_fat=1,
        sectors_per_cluster=1,
    ),
)

# 8" 1025k
pf_8_1025 = create_8inch_dsdd_base()
FAT12_FORMATS["ibm_8_1025k"] = FormatProfile(
    name="ibm_8_1025k",
    description="8\" DD 1MB (77 tracks, 2 heads, 26 sectors)",
    physical_format=pf_8_1025,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=26,
        bytes_per_sector=256,
        num_heads=2,
        total_sectors=4004,
        media_descriptor=0xFE,
        root_entries=192,
        sectors_per_fat=6,
        sectors_per_cluster=4,
    ),
)

# 8" 1180k
pf_8_1180 = create_8inch_dsdd_base(sectors_per_track=15, bytes_per_sector=512)
FAT12_FORMATS["ibm_8_1180k"] = FormatProfile(
    name="ibm_8_1180k",
    description="8\" DD 1.2MB (77 tracks, 2 heads, 15 sectors)",
    physical_format=pf_8_1180,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=15,
        bytes_per_sector=512,
        num_heads=2,
        total_sectors=2310,
        reserved_sectors=2,
        media_descriptor=0xFE,
        root_entries=192,
        sectors_per_fat=4,
        sectors_per_cluster=2,
    ),
)

# 8" 1260k
pf_8_1260 = create_8inch_dsdd_base(sectors_per_track=8, bytes_per_sector=1024)
FAT12_FORMATS["ibm_8_1260k"] = FormatProfile(
    name="ibm_8_1260k",
    description="8\" DD 1.25MB (77 tracks, 2 heads, 8 sectors)",
    physical_format=pf_8_1260,
    filesystem_config=FATVolumeInfo(
        sectors_per_track=8,
        bytes_per_sector=1024,
        num_heads=2,
        total_sectors=1232,
        media_descriptor=0xFE,
        root_entries=192,
        sectors_per_fat=2,
        sectors_per_cluster=1,
    ),
)
