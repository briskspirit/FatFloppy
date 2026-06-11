"""Format profiles for Apollo DOMAIN floppy images."""

from ...format_profile import FormatProfile
from ...physical_format import PhysicalFormat, TrackFormat

_CYLINDERS = 77
_HEADS = 2
_SPT = 8
_BPS = 1024
_RPM = 360
_RATE = 500


def _build_apollo_physical_format() -> PhysicalFormat:
    """Build the fixed PhysicalFormat for Apollo DOMAIN floppies (77×2×8×1024)."""
    tf = TrackFormat(
        track_start=0,
        track_end=_CYLINDERS - 1,
        head_start=0,
        head_end=_HEADS - 1,
        sectors_per_track=_SPT,
        encoding="MFM",
        rate=_RATE,
        interleave=1,
        bytes_per_sector=_BPS,
        id_start=0,
        iam_present=True,
    )
    return PhysicalFormat(
        cylinders=_CYLINDERS,
        heads=_HEADS,
        rpm=_RPM,
        heads_inverted=False,
        bytes_per_sector=_BPS,
        track_formats=[tf],
    )


APOLLO_FORMATS: dict[str, FormatProfile] = {
    "apollo_1.2m_wbak": FormatProfile(
        name="apollo_1.2m_wbak",
        description=(
            '5.25"/8" Apollo DOMAIN 1.2MB (77x2x8x1024), wbak backup media (read-only)'
        ),
        physical_format=_build_apollo_physical_format(),
        filesystem_config=None,
        notes=(
            "Single geometry for all Apollo DOMAIN wbak floppies. "
            "MFM 500 kb/s, 360 rpm.  Read-only archival images."
        ),
    ),
}
