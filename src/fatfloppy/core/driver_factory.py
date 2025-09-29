# src/fatfloppy/core/driver_factory.py
"""
Factory for creating disk I/O drivers.

This module provides a centralized, extensible way to create driver instances.
New driver types can be registered without modifying core controller logic.
"""

from typing import Dict, Callable, Any
from .drivers.base_driver import DiskIODriver
from .drivers import IMGImageDriver, IMDImageDriver, H17ImageDriver, GreaseweazleDriver
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class DriverFactory:
    """
    Factory for creating disk I/O drivers with a registry pattern.

    This allows for easy addition of new driver types without modifying
    the controller or this factory class.
    """

    # Registry mapping disk_type string to creator function
    _registry: Dict[str, Callable[..., DiskIODriver]] = {}

    @classmethod
    def register(cls, disk_type: str, creator_func: Callable[..., DiskIODriver]) -> None:
        """
        Registers a driver creator function for a given disk type.

        Args:
            disk_type: The disk type identifier (e.g., "IMG", "IMD", "physical")
            creator_func: A callable that takes keyword arguments and returns a DiskIODriver

        Example:
            def my_custom_driver_creator(source: str, **kwargs) -> MyDriver:
                return MyDriver(source)

            DriverFactory.register("CUSTOM", my_custom_driver_creator)
        """
        cls._registry[disk_type] = creator_func
        logger.debug(f"Registered driver type: {disk_type}")

    @classmethod
    def create(cls, disk_type: str, **kwargs) -> DiskIODriver:
        """
        Creates a driver instance for the specified disk type.

        Args:
            disk_type: The type of disk/driver to create (e.g., "IMG", "IMD", "physical")
            **kwargs: Driver-specific parameters (e.g., source, drive_letter, drive_size)

        Returns:
            An initialized DiskIODriver instance

        Raises:
            ValueError: If the disk_type is not registered

        Example:
            driver = DriverFactory.create("IMG", source="/path/to/disk.img")
            driver = DriverFactory.create("physical", source=None, drive_letter="A", drive_size="3.5")
        """
        creator = cls._registry.get(disk_type)
        if not creator:
            available = ", ".join(cls._registry.keys())
            raise ValueError(f"Unknown disk type '{disk_type}'. Available types: {available}")

        try:
            logger.info(f"Creating driver for disk type: {disk_type}")
            driver = creator(**kwargs)
            logger.debug(f"Successfully created {type(driver).__name__}")
            return driver
        except Exception as e:
            logger.error(f"Failed to create driver for type '{disk_type}': {e}")
            raise

    @classmethod
    def list_registered_types(cls) -> list[str]:
        """
        Returns a list of all registered disk types.

        Returns:
            List of disk type identifiers
        """
        return list(cls._registry.keys())


# ============================================================================
# Built-in Driver Creator Functions
# ============================================================================

def _create_img_driver(source: str, **kwargs) -> IMGImageDriver:
    """
    Creates an IMG (raw image) driver.

    Args:
        source: Path to the .img file
        **kwargs: Additional arguments (currently unused)

    Returns:
        An IMGImageDriver instance
    """
    return IMGImageDriver(file_path=source)


def _create_imd_driver(source: str, **kwargs) -> IMDImageDriver:
    """
    Creates an IMD (ImageDisk) driver.

    Args:
        source: Path to the .imd file
        **kwargs: Additional arguments (currently unused)

    Returns:
        An IMDImageDriver instance
    """
    return IMDImageDriver(file_path=source)


def _create_h17_driver(source: str, **kwargs) -> H17ImageDriver:
    """
    Creates an H17 (Heathkit) driver.

    Args:
        source: Path to the .h17disk file
        **kwargs: Additional arguments (currently unused)

    Returns:
        An H17ImageDriver instance
    """
    return H17ImageDriver(file_path=source)


def _create_greaseweazle_driver(source: str, drive_letter: str = "A",
                               drive_size: str = "3.5", **kwargs) -> GreaseweazleDriver:
    """
    Creates a Greaseweazle (physical drive) driver.

    Args:
        source: Device name for the Greaseweazle (can be None for auto-detect)
        drive_letter: The floppy drive letter (e.g., "A", "B")
        drive_size: The physical drive size ("3.5", "5.25", "8")
        **kwargs: Additional arguments (currently unused)

    Returns:
        An initialized GreaseweazleDriver instance

    Note:
        This driver requires initialization which measures the disk's RPM
        and performs hardware setup.
    """
    device_name = source if source else None
    driver = GreaseweazleDriver(
        device_name=device_name,
        drive=drive_letter,
        drive_size=drive_size
    )

    # Greaseweazle needs explicit initialization for RPM measurement
    driver.initialize()
    logger.debug(f"Initialized Greaseweazle driver for {drive_size}\" drive {drive_letter}")

    return driver


# ============================================================================
# Auto-register Built-in Drivers
# ============================================================================

# Register all built-in drivers when this module is imported
DriverFactory.register("IMG", _create_img_driver)
DriverFactory.register("IMD", _create_imd_driver)
DriverFactory.register("H17", _create_h17_driver)
DriverFactory.register("physical", _create_greaseweazle_driver)

logger.info(f"DriverFactory initialized with {len(DriverFactory.list_registered_types())} driver types")
