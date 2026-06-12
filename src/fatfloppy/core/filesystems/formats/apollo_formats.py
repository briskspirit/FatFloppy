"""Format profiles for Apollo DOMAIN floppy images.

Both Apollo filesystems share the single 77x2x8x1024 geometry; each
registers ONLY its own profile slice via ``get_format_definitions`` so the
registry sees each profile exactly once (the wbak filesystem returns
``APOLLO_WBAK_FORMATS``, the AEGIS filesystem ``APOLLO_AEGIS_FORMATS``).
``APOLLO_FORMATS`` is the combined view for callers that want every Apollo
profile.
"""

from ...apollo_wbak import build_apollo_physical_format
from ...format_profile import FormatProfile
from ..apollo_aegis_fs import AegisConfig
from ..apollo_wbak_fs import ApolloWbakConfig

APOLLO_WBAK_FORMATS: dict[str, FormatProfile] = {
    "apollo_1.2m_wbak": FormatProfile(
        name="apollo_1.2m_wbak",
        description=(
            '5.25"/8" Apollo DOMAIN 1.2MB (77x2x8x1024), wbak backup media (read-only)'
        ),
        physical_format=build_apollo_physical_format(),
        # volume_id=None sentinel: matches any parsed wbak volume (volume
        # IDs are per-disk); the config type names the filesystem for
        # profile matching in detection.
        filesystem_config=ApolloWbakConfig(),
        notes=(
            "Single geometry for all Apollo DOMAIN wbak floppies. "
            "MFM 500 kb/s, 360 rpm.  Read-only archival images."
        ),
    ),
}

APOLLO_AEGIS_FORMATS: dict[str, FormatProfile] = {
    "apollo_1.2m_aegis": FormatProfile(
        name="apollo_1.2m_aegis",
        description=(
            '5.25"/8" Apollo DOMAIN 1.2MB (77x2x8x1024), '
            "AEGIS native volume (read-only)"
        ),
        physical_format=build_apollo_physical_format(),
        # lv_uid=None sentinel: matches any parsed AEGIS volume (LV UIDs
        # are per-disk); the config type names the filesystem for profile
        # matching in detection.
        filesystem_config=AegisConfig(),
        notes=(
            "SR9-class AEGIS-native boot/utility floppies (disk5-class). "
            "MFM 500 kb/s, 360 rpm.  Read-only archival images."
        ),
    ),
}

APOLLO_FORMATS: dict[str, FormatProfile] = {
    **APOLLO_WBAK_FORMATS,
    **APOLLO_AEGIS_FORMATS,
}
