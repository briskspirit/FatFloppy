"""Format detector for Apollo DOMAIN floppy images."""

from ...format_detection import MetadataBasedDetector


class ApolloDetector(MetadataBasedDetector):
    """Detector for Apollo DOMAIN 77×2×8×1024 floppy images.

    The Apollo driver derives geometry from fixed constants and validates by
    magic bytes; the standard MetadataBasedDetector flow (set geometry from
    driver.physical_format, attempt filesystem parse) applies directly.

    Detection outcome depends on content:
    - Images whose first sector begins with the Apollo PV-label magic and
      carry a parseable wbak tape stream are claimed by
      ``ApolloWbakFilesystem`` (validity score ≥ 30).
    - Bare AEGIS-native containers (PV label present but no wbak stream)
      score below the auto-detection floor and yield
      ``(None, None, physical_format)`` — the controller opens them with
      geometry only, leaving filesystem access undefined.
    """

    detector_for_driver = "ApolloFloppyDriver"
