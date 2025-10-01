# src/fatfloppy/core/detector_registry.py
"""
Self-contained detector registry with automatic plugin discovery.

This module is completely self-sufficient - it discovers, validates,
and registers all format detector plugins automatically on import.
"""
from typing import Dict, Type, Optional

from .format_detection import FormatDetector
from .plugin_scanner import PluginScanner, PluginValidationError
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class DetectorRegistry:
    """Registry for format detector classes with auto-discovery."""

    _registry: Dict[str, Type[FormatDetector]] = {}
    _initialized: bool = False

    @classmethod
    def _validate_detector(cls, detector_class: Type[FormatDetector]) -> None:
        """
        Validates a detector plugin meets all requirements.

        Args:
            detector_class: The detector class to validate

        Raises:
            PluginValidationError: If validation fails
        """
        # Check required class attributes
        PluginScanner.validate_has_attributes(
            detector_class,
            ['detector_for_driver']
        )

        # Check abstract methods are implemented
        PluginScanner.validate_implements_methods(detector_class, FormatDetector)

    @classmethod
    def _discover_and_register(cls) -> None:
        """Discovers and registers all detector plugins."""
        if cls._initialized:
            return

        logger.info("Starting detector plugin discovery...")

        # We need to scan format_detection module, not a package
        # Import the module to get all its classes
        from . import format_detection
        import inspect

        detector_classes = []
        for name, obj in inspect.getmembers(format_detection, inspect.isclass):
            if (issubclass(obj, FormatDetector) and
                obj is not FormatDetector and
                not inspect.isabstract(obj) and
                hasattr(obj, 'detector_for_driver') and
                obj.detector_for_driver):

                try:
                    cls._validate_detector(obj)
                    detector_classes.append(obj)
                except PluginValidationError as e:
                    logger.error(f"Detector validation failed: {e}")

        # Register each discovered detector
        for detector_class in detector_classes:
            driver_name = detector_class.detector_for_driver

            cls._registry[driver_name] = detector_class

            logger.info(
                f"Registered detector: {detector_class.__name__} "
                f"for driver {driver_name}"
            )

        cls._initialized = True
        logger.info(f"Detector discovery complete. Registered {len(cls._registry)} detectors.")

    @classmethod
    def get_detector(cls, driver) -> Optional[Type[FormatDetector]]:
        """
        Retrieves the detector class for a given driver instance.

        Args:
            driver: The driver instance to get a detector for

        Returns:
            The corresponding FormatDetector class, or None if not found
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
        """Returns a list of all driver class names with registered detectors."""
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
            driver_class_name: The name of the driver class
            detector_class: The FormatDetector subclass

        Raises:
            PluginValidationError: If validation fails
        """
        # Validate the external plugin
        cls._validate_detector(detector_class)

        # Register it
        cls._registry[driver_class_name] = detector_class

        logger.info(f"Externally registered detector for: {driver_class_name}")


# Auto-discover on module import
DetectorRegistry._discover_and_register()
