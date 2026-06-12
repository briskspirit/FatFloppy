"""Format detector for Teledisk TD0 archives."""

from .imd_detector import IMDFormatDetector


class TD0FormatDetector(IMDFormatDetector):
    """
    Format detector for Teledisk (.TD0) container files.

    TD0 archives, like IMD files, carry complete embedded geometry
    (track-by-track sector counts, sizes, IDs and FM/MFM encoding), so the
    detection problem is identical: parse the filesystem over the driver's
    derived geometry, then fall back to lenient profile matching.  The
    detector therefore subclasses IMDFormatDetector to inherit its exact
    variant-testing flow — lenient (rate-tolerant) profile matching when a
    filesystem parses, and geometry-only matching with profile-config
    re-validation when it does not — so CP/M DPB inference and the no-BPB
    FAT12 synthesis run over TD0 geometry exactly as they do over IMD.

    Only `detector_for_driver` differs; PluginScanner registers this class
    for the TD0 driver (imported base classes are skipped by discovery, so
    the IMD registration is untouched).
    """

    detector_for_driver = "TD0ImageDriver"
