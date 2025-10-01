# src/fatfloppy/core/drivers/detectors/imd.py
from ...format_detection import MetadataBasedDetector


class IMDFormatDetector(MetadataBasedDetector):
    """Format detector for ImageDisk (.IMD) files."""
    detector_for_driver = "IMDImageDriver"
