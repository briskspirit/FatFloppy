# src/fatfloppy/core/drivers/detectors/h17.py
from ...format_detection import MetadataBasedDetector


class H17FormatDetector(MetadataBasedDetector):
    """Format detector for H17 (.h17disk) files."""
    detector_for_driver = "H17ImageDriver"
