# src/fatfloppy/core/driver_factory.py
"""
Factory for creating disk I/O driver instances with automatic driver selection.

This module provides the DriverFactory class, which handles:
- Automatic plugin discovery for driver implementations
- Driver selection based on file extension and priority
- Format disambiguation when multiple drivers support the same extension
- Driver validation before instantiation
"""

from pathlib import Path
from typing import Any, Optional

from .drivers.base_driver import DiskIODriver
from .plugin_scanner import PluginScanner
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class DriverFactory:
    """
    Factory class for creating and managing disk I/O drivers.

    Provides automatic driver discovery and intelligent selection based on
    file format detection and driver priorities.
    """

    _registry: dict[str, type[DiskIODriver]] = {}
    _extension_map: dict[str, list[type[DiskIODriver]]] = {}
    _initialized: bool = False

    @classmethod
    def create(cls, driver_type: str, source: str, **kwargs) -> DiskIODriver:
        """
        Creates a driver instance based on type or file extension.

        If driver_type is explicitly specified, uses that driver.
        Otherwise, attempts to detect the correct driver based on file extension
        and format validation.

        Args:
            driver_type: The driver type (e.g., "IMG", "IMD", "MITS_DSK").
                        Can be "auto" for automatic detection.
            source: The file path or device identifier.
            **kwargs: Additional driver-specific parameters.

        Returns:
            An initialized DiskIODriver instance.

        Raises:
            ValueError: If driver type is unknown or no suitable driver found.
            IOError: If driver creation fails.
        """
        if not cls._initialized:
            cls._discover_and_register()

        if driver_type and driver_type.upper() != "AUTO":
            return cls._create_explicit_driver(driver_type, source, **kwargs)

        return cls._create_auto_driver(source, **kwargs)

    @classmethod
    def get_driver_class(cls, disk_type: str) -> Optional[type[DiskIODriver]]:
        """
        Gets the driver class for a given disk type.

        Args:
            disk_type: The driver type name (e.g., 'IMG', 'IMD').

        Returns:
            The driver class, or None if not found.
        """
        if not cls._initialized:
            cls._discover_and_register()

        driver = cls._registry.get(disk_type.upper())
        if driver:
            return driver

        for key, driver_class in cls._registry.items():
            if key.upper() == disk_type.upper():
                return driver_class

        return None

    @classmethod
    def get_drivers_for_extension(cls, extension: str) -> list[str]:
        """
        Gets all driver types that support a given file extension.

        Args:
            extension: The file extension (with or without leading dot).

        Returns:
            List of driver type names in priority order.
        """
        if not cls._initialized:
            cls._discover_and_register()

        ext = extension.lower()
        if not ext.startswith('.'):
            ext = '.' + ext

        drivers = cls._extension_map.get(ext, [])
        return [d.driver_type for d in drivers]

    @classmethod
    def get_extension_map(cls) -> dict[str, str]:
        """
        Returns a mapping of file extensions to their primary driver types.

        For extensions with multiple drivers, returns "AUTO" to signal that
        auto-detection should be used.

        Returns:
            Dictionary mapping extensions to driver type names or "AUTO".
            Example: {'.img': 'IMG', '.dsk': 'AUTO', '.imd': 'IMD'}
        """
        if not cls._initialized:
            cls._discover_and_register()

        result = {}
        for ext, driver_classes in cls._extension_map.items():
            if len(driver_classes) > 1:
                result[ext] = "AUTO"
            elif driver_classes:
                result[ext] = driver_classes[0].driver_type

        return result

    @classmethod
    def list_drivers(cls) -> list[dict[str, Any]]:
        """
        Returns information about all registered drivers.

        Returns:
            List of dictionaries containing driver information.
        """
        if not cls._initialized:
            cls._discover_and_register()

        drivers_info = []
        for driver_type, driver_class in cls._registry.items():
            info = {
                'type': driver_type,
                'class': driver_class.__name__,
                'category': driver_class.driver_category,
                'extensions': driver_class.driver_file_extensions,
                'priority': getattr(driver_class, 'driver_priority', 50),
                'description': driver_class.driver_description,
            }
            drivers_info.append(info)

        return sorted(drivers_info, key=lambda d: d['type'])

    @classmethod
    def _create_auto_driver(cls, source: str, **kwargs) -> DiskIODriver:
        """
        Automatically detects and creates the appropriate driver.

        Uses file extension and format validation to determine the best driver.

        Args:
            source: The file path or device identifier.
            **kwargs: Additional driver-specific parameters.

        Returns:
            The created driver instance.

        Raises:
            ValueError: If no suitable driver can be found.
        """
        logger.info(f"Auto-detecting driver for: {source}")

        ext = Path(source).suffix.lower() if source else ""

        candidates = cls._extension_map.get(ext, [])

        if not candidates:
            if 'PHYSICAL' in cls._registry:
                candidates = [cls._registry['PHYSICAL']]
            else:
                raise ValueError(
                    f"No drivers registered for extension '{ext}' "
                    f"and no physical driver available"
                )

        logger.debug(
            f"Found {len(candidates)} candidate drivers for extension {ext}: "
            f"{[d.driver_type for d in candidates]}"
        )

        last_error = None
        for driver_class in candidates:
            driver_type = driver_class.driver_type
            logger.debug(f"Trying driver: {driver_type}")

            try:
                temp_instance = driver_class.__new__(driver_class)
                temp_instance.__dict__.update({
                    'logger': logger,
                    'physical_format': None
                })

                is_valid, error = temp_instance.validate_for_opening(source, **kwargs)

                if is_valid:
                    logger.info(f"Selected driver: {driver_type}")
                    return cls._instantiate_driver(driver_class, source, **kwargs)
                else:
                    logger.debug(f"Driver {driver_type} rejected {source}: {error}")
                    last_error = error

            except Exception as e:
                logger.debug(f"Driver {driver_type} validation failed: {e}")
                last_error = str(e)
                continue

        if candidates:
            tried = ', '.join(d.driver_type for d in candidates)
            raise ValueError(
                f"No suitable driver found for {source}. "
                f"Tried: {tried}. Last error: {last_error}"
            )
        else:
            raise ValueError(f"No drivers available for extension '{ext}'")

    @classmethod
    def _create_explicit_driver(
        cls,
        driver_type: str,
        source: str,
        **kwargs
    ) -> DiskIODriver:
        """
        Creates a driver of explicitly specified type.

        Args:
            driver_type: The driver type to create.
            source: The file path or device identifier.
            **kwargs: Additional driver-specific parameters.

        Returns:
            The created driver instance.

        Raises:
            ValueError: If driver type is unknown.
        """
        driver_type_upper = driver_type.upper()
        driver_class = cls._registry.get(driver_type_upper)

        if not driver_class:
            available = ', '.join(cls._registry.keys())
            raise ValueError(
                f"Unknown driver type: {driver_type}. "
                f"Available drivers: {available}"
            )

        logger.info(f"Creating explicit driver: {driver_type}")

        try:
            return cls._instantiate_driver(driver_class, source, **kwargs)

        except Exception as e:
            logger.error(f"Failed to create {driver_type} driver: {e}")
            raise

    @classmethod
    def _discover_and_register(cls) -> None:
        """Discovers and registers all driver plugins."""
        if cls._initialized:
            return

        logger.info("Starting driver plugin discovery...")

        try:
            drivers = PluginScanner.discover_plugins(
                package_name='fatfloppy.core.drivers',
                base_class=DiskIODriver,
                validator=cls._validate_driver
            )
        except Exception as e:
            logger.error(f"Plugin discovery failed: {e}", exc_info=True)
            drivers = []

        if not drivers:
            logger.warning("No drivers discovered! This is unusual.")

        for driver_class in drivers:
            driver_type = driver_class.driver_type
            extensions = driver_class.driver_file_extensions

            driver_type_upper = driver_type.upper()
            cls._registry[driver_type_upper] = driver_class

            for ext in extensions:
                ext_lower = ext.lower()
                if ext_lower not in cls._extension_map:
                    cls._extension_map[ext_lower] = []
                cls._extension_map[ext_lower].append(driver_class)

            logger.info(
                f"Registered driver: {driver_class.__name__} "
                f"(type={driver_type_upper}, extensions={extensions}, "
                f"category={driver_class.driver_category})"
            )

        for ext in cls._extension_map:
            cls._extension_map[ext].sort(
                key=lambda d: getattr(d, 'driver_priority', 50),
                reverse=True
            )
            logger.debug(
                f"Extension {ext} driver priority order: "
                f"{[d.driver_type for d in cls._extension_map[ext]]}"
            )

        cls._initialized = True
        logger.info(
            f"Driver discovery complete. Registered {len(cls._registry)} drivers "
            f"for {len(cls._extension_map)} extensions."
        )

        if not cls._registry:
            logger.critical(
                "NO DRIVERS REGISTERED! Check for import errors in driver modules."
            )

    @classmethod
    def _instantiate_driver(
        cls,
        driver_class: type[DiskIODriver],
        source: str,
        **kwargs
    ) -> DiskIODriver:
        """
        Instantiates a driver with appropriate parameters.

        Args:
            driver_class: The driver class to instantiate.
            source: The file path or device identifier.
            **kwargs: Additional driver-specific parameters.

        Returns:
            The created driver instance.

        Raises:
            IOError: If driver instantiation fails.
        """
        try:
            driver_category = driver_class.driver_category

            if driver_category == 'physical':
                drive_letter = kwargs.get('drive_letter', 'A')
                drive_size = kwargs.get('drive_size', '3.5')
                return driver_class(
                    device_name=source,
                    drive=drive_letter,
                    drive_size=drive_size
                )
            else:
                return driver_class(file_path=source)

        except Exception as e:
            logger.error(
                f"Failed to instantiate {driver_class.driver_type} driver: {e}"
            )
            raise OSError(
                f"Driver instantiation failed for {driver_class.driver_type}: {e}"
            ) from e

    @classmethod
    def _validate_driver(cls, driver_class: type[DiskIODriver]) -> None:
        """
        Validates that a driver plugin meets all requirements.

        Args:
            driver_class: The driver class to validate

        Raises:
            PluginValidationError: If validation fails
        """
        from .plugin_scanner import PluginValidationError

        required_attrs = ['driver_type', 'driver_category', 'driver_file_extensions']
        missing = []
        for attr in required_attrs:
            if not hasattr(driver_class, attr):
                missing.append(attr)

        if missing:
            raise PluginValidationError(
                f"{driver_class.__name__} missing required attributes: {missing}"
            )

        if not driver_class.driver_type or driver_class.driver_type == "":
            raise PluginValidationError(
                f"{driver_class.__name__}.driver_type cannot be empty"
            )

        PluginScanner.validate_implements_methods(driver_class, DiskIODriver)

        valid_categories = ['metadata_based', 'raw', 'physical']
        if driver_class.driver_category not in valid_categories:
            raise PluginValidationError(
                f"{driver_class.__name__}.driver_category must be one of "
                f"{valid_categories}"
            )
