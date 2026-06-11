"""Format profiles for Commodore CBM DOS disks."""

from ...cbm_layout import build_physical_format, layout_for_variant
from ...format_profile import FormatProfile

CBM_FORMATS: dict[str, FormatProfile] = {
    "cbm_1541_d64": FormatProfile(
        name="cbm_1541_d64",
        description=(
            '5.25" Commodore 1541 D64 (35 tracks, 683 sectors, 664 blocks free)'
        ),
        physical_format=build_physical_format("D64", 35),
        filesystem_config=layout_for_variant("D64", 35),
        notes="GCR zone recording 21/19/18/17 sectors per track.",
    ),
    "cbm_1541_d64_40track": FormatProfile(
        name="cbm_1541_d64_40track",
        description='5.25" Commodore 1541 D64 extended (40 tracks, 768 sectors)',
        physical_format=build_physical_format("D64", 40),
        filesystem_config=layout_for_variant("D64", 40),
        notes="Dolphin/Speed DOS extension; tracks 36-40 BAM not written.",
    ),
    "cbm_1571_d71": FormatProfile(
        name="cbm_1571_d71",
        description=(
            '5.25" Commodore 1571 D71 (70 tracks, 1366 sectors, 1328 blocks free)'
        ),
        physical_format=build_physical_format("D71", 70),
        filesystem_config=layout_for_variant("D71", 70),
    ),
    "cbm_1581_d81": FormatProfile(
        name="cbm_1581_d81",
        description=(
            '3.5" Commodore 1581 D81 (80 tracks, 3200 sectors, 3160 blocks free)'
        ),
        physical_format=build_physical_format("D81", 80),
        filesystem_config=layout_for_variant("D81", 80),
    ),
}
