from ...format_detection import MetadataBasedDetector


class IMDFormatDetector(MetadataBasedDetector):
    """
    Format detector for ImageDisk (.IMD) files.

    This detector uses the embedded metadata in IMD files to determine the disk
    format. IMD files contain track-by-track format information including sector
    numbering, encoding (FM/MFM), and data rate, making format detection
    straightforward through metadata inspection.
    """

    detector_for_driver = "IMDImageDriver"
