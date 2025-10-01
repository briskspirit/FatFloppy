# src/fatfloppy/core/detector_registry.py
"""
Registry for format detection strategies.

This module provides a centralized registry for mapping driver types to their
corresponding format detectors, allowing new detectors to be registered without
modifying core detection logic.
"""

from typing import Dict, Type, Optional

from .utils.logging_config import get_logger

logger = get_logger(__name__)


class DetectorRegistry:
    """
    A registry for format detector classes mapped to driver types.

    This allows each driver type to have its own format detection strategy
    without requiring hardcoded isinstance() checks in the detection code.
    """

    _registry: Dict[str, Type] = {}  # Driver class name -> Detector class

    @classmethod
    def register(cls, driver_class_name: str, detector_class: Type) -> None:
        """
        Registers a detector class for a specific driver type.

        Args:
            driver_class_name: The name of the driver class (e.g., "IMDImageDriver").
            detector_class: The FormatDetector subclass for this driver.

        Example:
            DetectorRegistry.register("IMDImageDriver", IMDFormatDetector)
        """
        cls._registry[driver_class_name] = detector_class
        logger.debug(f"Registered detector for driver: {driver_class_name}")

    @classmethod
    def get_detector(cls, driver) -> Optional[Type]:
        """
        Retrieves the detector class for a given driver instance.

        Args:
            driver: The driver instance to get a detector for.

        Returns:
            The corresponding FormatDetector class, or None if not found.
        """
        driver_class_name = driver.__class__.__name__
        detector_class = cls._registry.get(driver_class_name)

        if not detector_class:
            logger.warning(f"No detector registered for driver: {driver_class_name}")

        return detector_class

    @classmethod
    def list_registered_drivers(cls) -> list:
        """
        Returns a list of all driver class names with registered detectors.

        Returns:
            A list of driver class name strings.
        """
        return list(cls._registry.keys())


# ============================================================================
# Auto-register Built-in Detectors
# ============================================================================

def _register_builtin_detectors() -> None:
    """Registers all built-in format detectors."""
    # Import here to avoid circular dependencies
    from .format_detection import (
        IMDFormatDetector,
        H17FormatDetector,
        IMGFormatDetector,
        GreaseweazleFormatDetector
    )

    DetectorRegistry.register("IMDImageDriver", IMDFormatDetector)
    DetectorRegistry.register("H17ImageDriver", H17FormatDetector)
    DetectorRegistry.register("IMGImageDriver", IMGFormatDetector)
    DetectorRegistry.register("GreaseweazleDriver", GreaseweazleFormatDetector)

    logger.info(f"Registered {len(DetectorRegistry.list_registered_drivers())} built-in format detectors")


# Register built-in detectors when module is imported
_register_builtin_detectors()
