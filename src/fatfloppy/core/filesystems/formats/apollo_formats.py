"""Format profiles for Apollo DOMAIN floppy images."""

from ...apollo_wbak import build_apollo_physical_format
from ...format_profile import FormatProfile

APOLLO_FORMATS: dict[str, FormatProfile] = {
    "apollo_1.2m_wbak": FormatProfile(
        name="apollo_1.2m_wbak",
        description=(
            '5.25"/8" Apollo DOMAIN 1.2MB (77x2x8x1024), wbak backup media (read-only)'
        ),
        physical_format=build_apollo_physical_format(),
        filesystem_config=None,
        notes=(
            "Single geometry for all Apollo DOMAIN wbak floppies. "
            "MFM 500 kb/s, 360 rpm.  Read-only archival images."
        ),
    ),
}
