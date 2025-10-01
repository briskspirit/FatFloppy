# src/fatfloppy/core/driver_factory.py
"""
Self-contained driver factory with automatic plugin discovery.

This module is completely self-sufficient - it discovers, validates,
and registers all driver plugins automatically on import.
"""
from typing import Dict, Callable, Any, Type, Optional, List

from .drivers.base_driver import DiskIODriver
from .plugin_scanner import PluginScanner, PluginValidationError
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class DriverFactory:
    """Factory for creating disk I/O drivers with auto-discovery."""

    _registry: Dict[str, Type[DiskIODriver]] = {}
    _initialized: bool = False

    @classmethod
    def _validate_driver(cls, driver_class: Type[DiskIODriver]) -> None:
        """
        Validates a driver plugin meets all requirements.

        Args:
            driver_class: The driver class to validate

        Raises:
            PluginValidationError: If validation fails
        """
        # Check required class attributes
        PluginScanner.validate_has_attributes(
            driver_class,
            ['driver_type', 'driver_category']
        )

        # Check abstract methods are implemented
        PluginScanner.validate_implements_methods(driver_class, DiskIODriver)

        # Validate category
        valid_categories = ['metadata_based', 'raw', 'physical']
        if driver_class.driver_category not in valid_categories:
            raise PluginValidationError(
                f"{driver_class.__name__}.driver_category must be one of {valid_categories}"
            )

    @classmethod
    def _discover_and_register(cls) -> None:
        """Discovers and registers all driver plugins."""
        if cls._initialized:
            return

        logger.info("Starting driver plugin discovery...")

        # Discover all driver plugins
        drivers = PluginScanner.discover_plugins(
            package_name='fatfloppy.core.drivers',
            base_class=DiskIODriver,
            validator=cls._validate_driver
        )

        # Register each discovered driver
        for driver_class in drivers:
            driver_type = driver_class.driver_type
            cls._registry[driver_type] = driver_class
            logger.info(
                f"Registered driver: {driver_class.__name__} "
                f"(type={driver_type}, category={driver_class.driver_category})"
            )

        cls._initialized = True
        logger.info(f"Driver discovery complete. Registered {len(cls._registry)} drivers.")

    @classmethod
    def create(cls, disk_type: str, **kwargs) -> DiskIODriver:
        """
        Creates a driver instance for the specified disk type.
        Maps generic arguments from the controller to specific driver constructors.
        """
        if not cls._initialized:
            cls._discover_and_register()

        driver_class = cls._registry.get(disk_type)
        if not driver_class:
            available = ", ".join(cls._registry.keys())
            raise ValueError(f"Unknown disk type '{disk_type}'. Available types: {available}")

        try:
            logger.info(f"Creating driver for disk type: {disk_type}")

            # This logic correctly maps the generic arguments from the controller
            # to the specific constructor signatures of the drivers.
            args_for_constructor = {}
            if disk_type == "physical":
                # For GreaseweazleDriver
                args_for_constructor['device_name'] = kwargs.get('source')
                args_for_constructor['drive'] = kwargs.get('drive_letter', 'A')
                args_for_constructor['drive_size'] = kwargs.get('drive_size', '3.5')
            else:  # For file-based drivers like IMG, IMD, H17
                args_for_constructor['file_path'] = kwargs.get('source')
                # Do not pass drive_letter, drive_size, etc., to these drivers

            driver = driver_class(**args_for_constructor)

            logger.debug(f"Successfully created {type(driver).__name__}")
            return driver
        except Exception as e:
            logger.error(f"Failed to create driver for type '{disk_type}': {e}", exc_info=True)
            raise

    @classmethod
    def list_registered_types(cls) -> List[str]:
        """Returns a list of all registered disk types."""
        if not cls._initialized:
            cls._discover_and_register()
        return list(cls._registry.keys())

    @classmethod
    def get_driver_class(cls, disk_type: str) -> Optional[Type[DiskIODriver]]:
        """
        Gets the driver class for a given disk type.
        """
        if not cls._initialized:
            cls._discover_and_register()
        return cls._registry.get(disk_type)

    @classmethod
    def register_external(cls, disk_type: str, driver_class: Type[DiskIODriver]) -> None:
        """
        Public API for external plugins to register themselves.
        """
        cls._validate_driver(driver_class)
        cls._registry[disk_type] = driver_class
        logger.info(f"Externally registered driver: {disk_type}")


# Auto-discover on module import
DriverFactory._discover_and_register()
