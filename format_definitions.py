# format_definitions.py

from disk import DiskGeometry
from drivers import PhysicalFormat
from formats import FormatProfile, BootSectorData

FLOPPY_FORMATS = {
    # 5.25" formats
    "ibm_5.25_160k": FormatProfile(
        name="ibm_5.25_160k",
        description="5.25\" DD 160KB (40 tracks, 1 head, 8 sectors)",
        geometry=DiskGeometry(
            cylinders=40,
            heads=1,
            sectors_per_track=8,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=250,
            rpm=300,
            gap3=84,
            sectors_per_track=8,
            heads=1,
            sector_size=512
        ),
        media_descriptor=0xFE,
        boot_sector=BootSectorData(
            sectors_per_track=8,
            num_heads=1,
            total_sectors=320,
            media_descriptor=0xFE,
            root_entries=64,
            sectors_per_fat=1,
            sectors_per_cluster=1
        )
    ),

    "ibm_5.25_180k": FormatProfile(
        name="ibm_5.25_180k",
        description="5.25\" DD 180KB (40 tracks, 1 head, 9 sectors)",
        geometry=DiskGeometry(
            cylinders=40,
            heads=1,
            sectors_per_track=9,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=250,
            rpm=300,
            gap3=84,
            sectors_per_track=9,
            heads=1,
            sector_size=512
        ),
        media_descriptor=0xFC,
        boot_sector=BootSectorData(
            sectors_per_track=9,
            num_heads=1,
            total_sectors=360,
            media_descriptor=0xFC,
            root_entries=64,
            sectors_per_fat=2,
            sectors_per_cluster=1
        )
    ),

    "ibm_5.25_320k": FormatProfile(
        name="ibm_5.25_320k",
        description="5.25\" DD 320KB (40 tracks, 2 heads, 8 sectors)",
        geometry=DiskGeometry(
            cylinders=40,
            heads=2,
            sectors_per_track=8,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=250,
            rpm=300,
            gap3=84,
            sectors_per_track=8,
            heads=2,
            sector_size=512
        ),
        media_descriptor=0xFF,
        boot_sector=BootSectorData(
            sectors_per_track=8,
            num_heads=2,
            total_sectors=640,
            media_descriptor=0xFF,
            root_entries=112,
            sectors_per_fat=1,
            sectors_per_cluster=2
        )
    ),

    "ibm_5.25_360k": FormatProfile(
        name="ibm_5.25_360k",
        description="5.25\" DD 360KB (40 tracks, 2 heads, 9 sectors)",
        geometry=DiskGeometry(
            cylinders=40,
            heads=2,
            sectors_per_track=9,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=250,
            rpm=300,
            gap3=84,
            sectors_per_track=9,
            heads=2,
            sector_size=512
        ),
        media_descriptor=0xFD,
        boot_sector=BootSectorData(
            sectors_per_track=9,
            num_heads=2,
            total_sectors=720,
            media_descriptor=0xFD,
            root_entries=112,
            sectors_per_fat=2,
            sectors_per_cluster=2
        )
    ),

    "ibm_5.25_1.2m": FormatProfile(
        name="ibm_5.25_1.2m",
        description="5.25\" HD 1.2MB (80 tracks, 2 heads, 15 sectors)",
        geometry=DiskGeometry(
            cylinders=80,
            heads=2,
            sectors_per_track=15,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=500,
            rpm=360,
            gap3=84,
            sectors_per_track=15,
            heads=2,
            sector_size=512
        ),
        media_descriptor=0xF9,
        boot_sector=BootSectorData(
            sectors_per_track=15,
            num_heads=2,
            total_sectors=2400,
            media_descriptor=0xF9,
            root_entries=224,
            sectors_per_fat=7,
            sectors_per_cluster=1
        )
    ),

    # 3.5" formats
    "ibm_3.5_360k": FormatProfile(
        name="ibm_3.5_360k",
        description="3.5\" DD 360KB (80 tracks, 1 head, 9 sectors)",
        geometry=DiskGeometry(
            cylinders=80,
            heads=1,
            sectors_per_track=9,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=250,
            rpm=300,
            gap3=84,
            sectors_per_track=9,
            heads=1
            sector_size=512
        ),
        media_descriptor=0xFC,
        boot_sector=BootSectorData(
            sectors_per_track=9,
            num_heads=1,
            total_sectors=720,
            media_descriptor=0xFC,
            root_entries=112,
            sectors_per_fat=3,
            sectors_per_cluster=2
        )
    ),

    "ibm_3.5_720k": FormatProfile(
        name="ibm_3.5_720k",
        description="3.5\" DD 720KB (80 tracks, 2 heads, 9 sectors)",
        geometry=DiskGeometry(
            cylinders=80,
            heads=2,
            sectors_per_track=9,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=250,
            rpm=300,
            gap3=84,
            sectors_per_track=9,
            heads=2,
            sector_size=512
        ),
        media_descriptor=0xF9,
        boot_sector=BootSectorData(
            sectors_per_track=9,
            num_heads=2,
            total_sectors=1440,
            media_descriptor=0xF9,
            root_entries=112,
            sectors_per_fat=3,
            sectors_per_cluster=2
        )
    ),

    "ibm_3.5_1.44m": FormatProfile(
        name="ibm_3.5_1.44m",
        description="3.5\" HD 1.44MB (80 tracks, 2 heads, 18 sectors)",
        geometry=DiskGeometry(
            cylinders=80,
            heads=2,
            sectors_per_track=18,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=500,
            rpm=300,
            gap3=84,
            sectors_per_track=18,
            heads=2,
            sector_size=512
        ),
        media_descriptor=0xF0,
        boot_sector=BootSectorData(
            sectors_per_track=18,
            num_heads=2,
            total_sectors=2880,
            media_descriptor=0xF0,
            root_entries=224,
            sectors_per_fat=9,
            sectors_per_cluster=1
        )
    ),

    "ibm_3.5_2.88m": FormatProfile(
        name="ibm_3.5_2.88m",
        description="3.5\" ED 2.88MB (80 tracks, 2 heads, 36 sectors)",
        geometry=DiskGeometry(
            cylinders=80,
            heads=2,
            sectors_per_track=36,
            sector_size=512
        ),
        physical_format=PhysicalFormat(
            encoding="MFM",
            rate=1000,
            rpm=300,
            gap3=84,
            sectors_per_track=36,
            heads=2,
            sector_size=512
        ),
        media_descriptor=0xF0,
        boot_sector=BootSectorData(
            sectors_per_track=36,
            num_heads=2,
            total_sectors=5760,
            media_descriptor=0xF0,
            root_entries=224,
            sectors_per_fat=9,
            sectors_per_cluster=2
        )
    ),

    # 8" formats
    # "ibm_8_250k": FormatProfile(
    #     name="ibm_8_250k",
    #     description="8\" SD 250KB (77 tracks, 1 head, 26 sectors)",
    #     geometry=DiskGeometry(
    #         cylinders=77,
    #         heads=1,
    #         sectors_per_track=26,
    #         sector_size=128
    #     ),
    #     physical_format=PhysicalFormat(
    #         encoding="FM",
    #         rate=500,
    #         rpm=360,
    #         gap3=84,
    #         sectors_per_track=26,
    #         heads=1,
    #         sector_size=128
    #     ),
    #     media_descriptor=0xFE,
    #     boot_sector=BootSectorData(
    #         sectors_per_track=26,
    #         num_heads=1,
    #         total_sectors=2002,
    #         media_descriptor=0xFE,
    #         root_entries=68,
    #         sectors_per_fat=XXX,
    #         sectors_per_cluster=XXX
    #     )
    # ),

    # "ibm_8_500k": FormatProfile(
    #     name="ibm_8_500k",
    #     description="8\" SD 500KB (77 tracks, 2 heads, 26 sectors)",
    #     geometry=DiskGeometry(
    #         cylinders=77,
    #         heads=2,
    #         sectors_per_track=26,
    #         sector_size=128
    #     ),
    #     physical_format=PhysicalFormat(
    #         encoding="FM",
    #         rate=500,
    #         rpm=360,
    #         gap3=84,
    #         sectors_per_track=26,
    #         heads=2,
    #         sector_size=128
    #     ),
    #     media_descriptor=0xFD,
    #     boot_sector=BootSectorData(
    #         sectors_per_track=26,
    #         num_heads=2,
    #         total_sectors=4004,
    #         media_descriptor=0xFD,
    #         root_entries=96,
    #         sectors_per_fat=XXX,
    #         sectors_per_cluster=XXX
    #     )
    # ),

    # "ibm_8_1.2m": FormatProfile(
    #     name="ibm_8_1.2m",
    #     description="8\" DD 1.2MB (77 tracks, 2 heads, 8 sectors)",
    #     geometry=DiskGeometry(
    #         cylinders=77,
    #         heads=2,
    #         sectors_per_track=8,
    #         sector_size=1024
    #     ),
    #     physical_format=PhysicalFormat(
    #         encoding="MFM",
    #         rate=500,
    #         rpm=360,
    #         gap3=84,
    #         sectors_per_track=8,
    #         heads=2,
    #         sector_size=1024
    #     ),
    #     media_descriptor=0xFE,
    #     boot_sector=BootSectorData(
    #         sectors_per_track=8,
    #         num_heads=2,
    #         total_sectors=1232,
    #         media_descriptor=0xFE,
    #         root_entries=192,
    #         sectors_per_fat=XXX,
    #         sectors_per_cluster=XXX
    #     )
    # ),

    # # DMF formats (Distribution Media Format)
    # "ibm_3.5_dmf": FormatProfile(
    #     name="ibm_3.5_dmf",
    #     description="3.5\" HD DMF 1.7MB (80 tracks, 2 heads, 21 sectors)",
    #     geometry=DiskGeometry(
    #         cylinders=80,
    #         heads=2,
    #         sectors_per_track=21,
    #         sector_size=512
    #     ),
    #     physical_format=PhysicalFormat(
    #         encoding="MFM",
    #         rate=500,
    #         rpm=300,
    #         gap3=12,
    #         skew=3,
    #         interleave=2,
    #         sectors_per_track=21,
    #         heads=2,
    #         sector_size=512
    #     ),
    #     media_descriptor=0xF0,
    #     boot_sector=BootSectorData(
    #         sectors_per_track=21,
    #         num_heads=2,
    #         total_sectors=3360,
    #         media_descriptor=0xF0,
    #         root_entries=16,
    #         sectors_per_fat=XXX,
    #         sectors_per_cluster=XXX
    #     )
    # ),
}
