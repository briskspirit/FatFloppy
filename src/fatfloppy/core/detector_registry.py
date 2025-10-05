# src/fatfloppy/core/detector_registry.py
"""
Self-contained detector registry with automatic plugin discovery.

This module is completely self-sufficient - it discovers, validates,
and registers all format detector plugins automatically on import.
"""
from typing import Dict, Optional, Type

from .format_detection import FormatDetector
from .plugin_scanner import PluginScanner
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class DetectorRegistry:
    """Registry for format detector classes with auto-discovery."""

    _registry: Dict[str, Type[FormatDetector]] = {}
    _initialized: bool = False

    @classmethod
    def get_detector(cls, driver) -> Optional[Type[FormatDetector]]:
        """
        Retrieves the detector class for a given driver instance.

        Args:
            driver: The driver instance to find a detector for.

        Returns:
            The detector class for the driver, or None if not found.
        """
        if not cls._initialized:
            cls._discover_and_register()

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
            List of driver class names that have registered detectors.
        """
        if not cls._initialized:
            cls._discover_and_register()
        return list(cls._registry.keys())

    @classmethod
    def register_external(
        cls,
        driver_class_name: str,
        detector_class: Type[FormatDetector]
    ) -> None:
        """
        Public API for external plugins to register themselves.

        Args:
            driver_class_name: The name of the driver class.
            detector_class: The detector class to register.
        """
        cls._validate_detector(detector_class)
        cls._registry[driver_class_name] = detector_class
        logger.info(f"Externally registered detector for: {driver_class_name}")

    @classmethod
    def _discover_and_register(cls) -> None:
        """
        Discovers and registers all detector plugins from the drivers.detectors package.
        """
        if cls._initialized:
            return

        logger.info("Starting detector plugin discovery...")

        detector_classes = PluginScanner.discover_plugins(
            package_name='fatfloppy.core.drivers.detectors',
            base_class=FormatDetector,
            validator=cls._validate_detector
        )

        for detector_class in detector_classes:
            driver_name = detector_class.detector_for_driver
            cls._registry[driver_name] = detector_class
            logger.info(
                f"Registered detector: {detector_class.__name__} "
                f"for driver {driver_name}"
            )

        cls._initialized = True
        logger.info(
            f"Detector discovery complete. Registered {len(cls._registry)} detectors."
        )

    @classmethod
    def _validate_detector(cls, detector_class: Type[FormatDetector]) -> None:
        """
        Validates a detector plugin.

        Args:
            detector_class: The detector class to validate.
        """
        PluginScanner.validate_has_attributes(detector_class, ['detector_for_driver'])
        PluginScanner.validate_implements_methods(detector_class, FormatDetector)


DetectorRegistry._discover_and_register()
