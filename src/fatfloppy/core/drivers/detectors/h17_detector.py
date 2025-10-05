from ...format_detection import MetadataBasedDetector


class H17FormatDetector(MetadataBasedDetector):
    """
    Format detector for H17 (.h17disk) files.

    This detector uses the embedded metadata in H17 files to determine the disk
    format. H17 files contain complete geometry and sector header information,
    making format detection straightforward through metadata inspection.
    """

    detector_for_driver = "H17ImageDriver"
