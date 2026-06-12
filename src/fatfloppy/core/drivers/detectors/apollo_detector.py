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
      ``ApolloWbakFilesystem`` (validity score >= its validity_threshold).
    - Bare AEGIS-native containers (PV label present but no wbak stream)
      score at most 25 as wbak, below ``ApolloWbakFilesystem``'s threshold
      of 40, and are instead claimed by ``ApolloAegisFilesystem``
      (threshold 40) through the same scoring path: SR9 volumes are
      claimed; SR10+ volumes are recognized but deliberately not claimed
      (score capped at 25). Pinned by tests/software/test_54_aegis_fs.py.
    """

    detector_for_driver = "ApolloFloppyDriver"
