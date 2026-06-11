"""Format detector for Apollo DOMAIN floppy images."""

from ...format_detection import MetadataBasedDetector


class ApolloDetector(MetadataBasedDetector):
    """Detector for Apollo DOMAIN 77×2×8×1024 floppy images.

    The Apollo driver derives geometry from fixed constants and validates by
    magic bytes; the standard MetadataBasedDetector flow (set geometry from
    driver.physical_format, attempt filesystem parse) applies directly.
    Until Task 5 lands, detect() returns (None, None, physical_format) —
    the controller treats that as a valid geometry-only open.
    """

    detector_for_driver = "ApolloFloppyDriver"
