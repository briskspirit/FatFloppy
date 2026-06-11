"""Format detector for CBM D64/D71/D81 images (embedded geometry)."""

from ...format_detection import MetadataBasedDetector


class CBMDetector(MetadataBasedDetector):
    """Detector for CBM 1541/1571/1581 sector-dump images.

    The CBM driver derives geometry from file size; the standard
    MetadataBasedDetector flow (set geometry from driver.physical_format,
    attempt filesystem parse) applies directly.  No CBM filesystem exists yet,
    so detect() will return (None, None, physical_format) for blank images —
    the controller treats that as a valid geometry-only open.
    """

    detector_for_driver = "CBMImageDriver"
