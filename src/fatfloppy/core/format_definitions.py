# src/fatfloppy/core/format_definitions.py
from .physical_format import PhysicalFormat, TrackFormat
from .format_profile import FormatProfile
from .filesystem import FATVolumeInfo

# TODO: Adjust gap3 based on doc: https://www.isdaman.com/alsos/hardware/fdc/floppy.htm
# TODO: Verify against https://retrocmp.de/fdd/general/floppy-formats.htm

FLOPPY_FORMATS = {
    "ibm_5.25_160k": FormatProfile(
        name="ibm_5.25_160k",
        description="5.25\" DD 160KB (40 tracks, 1 head, 8 sectors)",
        physical_format=PhysicalFormat(
            cylinders=40,
            heads=1,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=39,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=8,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=512, num_heads=1, total_sectors=320, media_descriptor=0xFE, root_entries=64, sectors_per_fat=1, sectors_per_cluster=1)
    ),
    "ibm_5.25_180k": FormatProfile(
        name="ibm_5.25_180k",
        description="5.25\" DD 180KB (40 tracks, 1 head, 9 sectors)",
        physical_format=PhysicalFormat(
            cylinders=40,
            heads=1,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=39,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=9,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=9, bytes_per_sector=512, num_heads=1, total_sectors=360, media_descriptor=0xFC, root_entries=64, sectors_per_fat=2, sectors_per_cluster=1)
    ),
    "ibm_5.25_320k": FormatProfile(
        name="ibm_5.25_320k",
        description="5.25\" DD 320KB (40 tracks, 2 heads, 8 sectors)",
        physical_format=PhysicalFormat(
            cylinders=40,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=39,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=8,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=512, num_heads=2, total_sectors=640, media_descriptor=0xFF, root_entries=112, sectors_per_fat=1, sectors_per_cluster=2)
    ),
    "ibm_5.25_360k": FormatProfile(
        name="ibm_5.25_360k",
        description="5.25\" DD 360KB (40 tracks, 2 heads, 9 sectors)",
        physical_format=PhysicalFormat(
            cylinders=40,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=39,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=9,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=9, bytes_per_sector=512, num_heads=2, total_sectors=720, media_descriptor=0xFD, root_entries=112, sectors_per_fat=2, sectors_per_cluster=2)
    ),
    "ibm_5.25_1.2m": FormatProfile(
        name="ibm_5.25_1.2m",
        description="5.25\" HD 1.2MB (80 tracks, 2 heads, 15 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=15,
                    encoding="MFM",
                    rate=500,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=15, bytes_per_sector=512, num_heads=2, total_sectors=2400, media_descriptor=0xF9, root_entries=224, sectors_per_fat=7, sectors_per_cluster=1)
    ),
    "ibm_3.5_320k": FormatProfile(
        name="ibm_3.5_320k",
        description="3.5\" DD 320KB (80 tracks, 1 head, 8 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=8,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=512, num_heads=1, total_sectors=640, media_descriptor=0xFF, root_entries=112, sectors_per_fat=1, sectors_per_cluster=2)
    ),
    "ibm_3.5_360k": FormatProfile(
        name="ibm_3.5_360k",
        description="3.5\" DD 360KB (80 tracks, 1 head, 9 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=9,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=9, bytes_per_sector=512, num_heads=1, total_sectors=720, media_descriptor=0xFC, root_entries=112, sectors_per_fat=2, sectors_per_cluster=2)
    ),
    "ibm_3.5_640k": FormatProfile(
        name="ibm_3.5_640k",
        description="3.5\" DD 640KB (80 tracks, 2 heads, 8 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=8,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=512, num_heads=2, total_sectors=1280, media_descriptor=0xFF, root_entries=112, sectors_per_fat=2, sectors_per_cluster=2)
    ),
    "ibm_3.5_720k": FormatProfile(
        name="ibm_3.5_720k",
        description="3.5\" DD 720KB (80 tracks, 2 heads, 9 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=9,
                    encoding="MFM",
                    rate=250,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=9, bytes_per_sector=512, num_heads=2, total_sectors=1440, media_descriptor=0xF9, root_entries=112, sectors_per_fat=3, sectors_per_cluster=2)
    ),
    "ibm_3.5_800k": FormatProfile(
        name="ibm_3.5_800k",
        description="3.5\" DD 800KB (80 tracks, 2 heads, 10 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=10,
                    encoding="MFM",
                    rate=250,
                    gap3=30,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=10, bytes_per_sector=512, num_heads=2, total_sectors=1600, media_descriptor=0xF9, root_entries=112, sectors_per_fat=3, sectors_per_cluster=2)
    ),
    "ibm_3.5_1.44m": FormatProfile(
        name="ibm_3.5_1.44m",
        description="3.5\" HD 1.44MB (80 tracks, 2 heads, 18 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=18,
                    encoding="MFM",
                    rate=500,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=18, bytes_per_sector=512, num_heads=2, total_sectors=2880, media_descriptor=0xF0, root_entries=224, sectors_per_fat=9, sectors_per_cluster=1)
    ),
    "ibm_3.5_2.88m": FormatProfile(
        name="ibm_3.5_2.88m",
        description="3.5\" ED 2.88MB (80 tracks, 2 heads, 36 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=36,
                    encoding="MFM",
                    rate=1000,
                    gap3=41,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=36, bytes_per_sector=512, num_heads=2, total_sectors=5760, reserved_sectors=2, media_descriptor=0xF0, root_entries=224, sectors_per_fat=9, sectors_per_cluster=2)
    ),
    "ibm_3.5_1.68m": FormatProfile(
        name="ibm_3.5_1.68m",
        description="3.5\" HD 1.68MB (80 tracks, 2 heads, 21 sectors)",
        physical_format=PhysicalFormat(
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
                    sectors_per_track=21,
                    encoding="MFM",
                    rate=500,
                    gap3=12,
                    interleave=2
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=21, bytes_per_sector=512, num_heads=2, total_sectors=3360, media_descriptor=0xF0, root_entries=16, sectors_per_fat=5, sectors_per_cluster=2)
    ),
    "ibm_3.5_1.72m": FormatProfile(
        name="ibm_3.5_1.72m",
        description="3.5\" HD 1.72MB (82 tracks, 2 heads, 21 sectors)",
        physical_format=PhysicalFormat(
            cylinders=82,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=81,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=21,
                    encoding="MFM",
                    rate=500,
                    gap3=12,
                    interleave=2
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=21, bytes_per_sector=512, num_heads=2, total_sectors=3444, media_descriptor=0xF0, root_entries=16, sectors_per_fat=6, sectors_per_cluster=2)
    ),
    "ibm_8_250k": FormatProfile(
        name="ibm_8_250k",
        description="8\" SD 250KB (77 tracks, 1 head, 26 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=1,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=128,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=26,
                    encoding="FM",
                    rate=250,
                    gap3=26,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=26, bytes_per_sector=128, num_heads=1, total_sectors=2002, media_descriptor=0xFE, root_entries=68, sectors_per_fat=6, sectors_per_cluster=4)
    ),
    "ibm_8_298k": FormatProfile(
        name="ibm_8_298k",
        description="8\" SD 298KB (77 tracks, 1 head, 15 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=1,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=256,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=15,
                    encoding="FM",
                    rate=250,
                    gap3=26,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=15, bytes_per_sector=256, num_heads=1, total_sectors=1155, media_descriptor=0xFE, root_entries=56, sectors_per_fat=4, sectors_per_cluster=2)
    ),
    "ibm_8_315k": FormatProfile(
        name="ibm_8_315k",
        description="8\" SD 315KB (77 tracks, 1 head, 8 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=1,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=8,
                    encoding="FM",
                    rate=250,
                    gap3=26,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=512, num_heads=1, total_sectors=616, media_descriptor=0xFE, root_entries=64, sectors_per_fat=2, sectors_per_cluster=1)
    ),
    "ibm_8_500k": FormatProfile(
        name="ibm_8_500k",
        description="8\" SD 500KB (77 tracks, 2 heads, 26 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=2,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=128,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=26,
                    encoding="FM",
                    rate=250,
                    gap3=26,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=26, bytes_per_sector=128, num_heads=2, total_sectors=4004, reserved_sectors=2, media_descriptor=0xFD, root_entries=96, sectors_per_fat=12, sectors_per_cluster=4)
    ),
    "ibm_8_590k": FormatProfile(
        name="ibm_8_590k",
        description="8\" SD 590KB (77 tracks, 2 heads, 15 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=2,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=256,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=15,
                    encoding="FM",
                    rate=250,
                    gap3=26,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=15, bytes_per_sector=256, num_heads=2, total_sectors=2310, reserved_sectors=2, media_descriptor=0xFD, root_entries=96, sectors_per_fat=7, sectors_per_cluster=2)
    ),
    "ibm_8_630k": FormatProfile(
        name="ibm_8_630k",
        description="8\" DD 630KB (77 tracks, 1 head, 8 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=1,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=1024,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=8,
                    encoding="MFM",
                    rate=500,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=1024, num_heads=1, total_sectors=616, media_descriptor=0x00, root_entries=96, sectors_per_fat=1, sectors_per_cluster=1)
    ),
    "ibm_8_1025k": FormatProfile(
        name="ibm_8_1025k",
        description="8\" DD 1MB (77 tracks, 2 heads, 26 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=2,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=256,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=26,
                    encoding="MFM",
                    rate=500,
                    gap3=54,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=26, bytes_per_sector=256, num_heads=2, total_sectors=4004, media_descriptor=0xFE, root_entries=192, sectors_per_fat=6, sectors_per_cluster=4)
    ),
    "ibm_8_1180k": FormatProfile(
        name="ibm_8_1180k",
        description="8\" DD 1.2MB (77 tracks, 2 heads, 15 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=2,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=15,
                    encoding="MFM",
                    rate=500,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=15, bytes_per_sector=512, num_heads=2, total_sectors=2310, reserved_sectors=2, media_descriptor=0xFE, root_entries=192, sectors_per_fat=4, sectors_per_cluster=2)
    ),
    "ibm_8_1260k": FormatProfile(
        name="ibm_8_1260k",
        description="8\" DD 1.25MB (77 tracks, 2 heads, 8 sectors)",
        physical_format=PhysicalFormat(
            cylinders=77,
            heads=2,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=1024,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=8,
                    encoding="MFM",
                    rate=500,
                    gap3=84,
                    interleave=1
                )
            ]
        ),
        filesystem_metadata=FATVolumeInfo(sectors_per_track=8, bytes_per_sector=1024, num_heads=2, total_sectors=1232, media_descriptor=0xFE, root_entries=192, sectors_per_fat=2, sectors_per_cluster=1)
    ),
}
