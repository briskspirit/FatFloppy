# src/fatfloppy/core/plugin_scanner.py
"""
Automatic plugin discovery system.

Scans specified directories for valid plugin classes and validates them.
"""

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Callable

from .utils.logging_config import get_logger

logger = get_logger(__name__)


class PluginValidationError(Exception):
    """Raised when a plugin fails validation."""

    pass


class PluginScanner:
    """Discovers and validates plugins from specified directories."""

    @staticmethod
    def discover_plugins(
        package_name: str, base_class: type, validator: Callable[[type], None] = None
    ) -> list[type]:
        """
        Discovers all subclasses of base_class in the given package.

        Args:
            package_name: Full package name (e.g., 'fatfloppy.core.filesystems')
            base_class: The base class that plugins must inherit from
            validator: Optional function to validate each discovered class

        Returns:
            List of valid plugin classes

        Raises:
            PluginValidationError: If a plugin fails validation
        """
        discovered_plugins = []

        try:
            package = importlib.import_module(package_name)
            package_path = Path(package.__file__).parent

            for _finder, name, _ispkg in pkgutil.iter_modules([str(package_path)]):
                if name.startswith("_"):
                    continue

                module_name = f"{package_name}.{name}"

                try:
                    module = importlib.import_module(module_name)

                    for _item_name, item in inspect.getmembers(module, inspect.isclass):
                        if (
                            not issubclass(item, base_class)
                            or item is base_class
                            or item.__module__ != module_name
                        ):
                            continue

                        # An incomplete subclass is abstract; warn (and skip)
                        # instead of silently dropping it (audit plugin_scanner:65).
                        if inspect.isabstract(item):
                            logger.warning(
                                f"Skipping incomplete plugin {item.__name__} in "
                                f"{module_name}: unimplemented methods "
                                f"{sorted(getattr(item, '__abstractmethods__', ()))}"
                            )
                            continue

                        if validator:
                            try:
                                validator(item)
                            except Exception as e:
                                # One bad plugin only excludes itself; it must not
                                # abort discovery of the others (audit:76).
                                logger.error(
                                    f"Plugin validation failed for "
                                    f"{item.__name__} in {module_name}: {e}"
                                )
                                continue

                        discovered_plugins.append(item)
                        logger.debug(
                            f"Discovered plugin: {item.__name__} from {module_name}"
                        )

                except ImportError as e:
                    logger.warning(f"Failed to import module {module_name}: {e}")
                except Exception as e:
                    logger.error(f"Error processing module {module_name}: {e}")

        except ImportError as e:
            logger.error(f"Failed to import package {package_name}: {e}")
            return []

        return discovered_plugins

    @staticmethod
    def validate_has_attributes(cls: type, required_attrs: list[str]) -> None:
        """
        Validates that a class has all required class attributes.

        Args:
            cls: The class to validate
            required_attrs: List of required attribute names

        Raises:
            PluginValidationError: If any required attribute is missing
        """
        missing = []
        for attr in required_attrs:
            if (
                not hasattr(cls, attr)
                or getattr(cls, attr) == ""
                or getattr(cls, attr) == []
            ):
                missing.append(attr)

        if missing:
            raise PluginValidationError(
                f"Plugin {cls.__name__} missing required attributes: {missing}"
            )

    @staticmethod
    def validate_implements_methods(cls: type, _base_class: type) -> None:
        """
        Validates that a class implements all abstract methods from base.

        Args:
            cls: The class to validate
            _base_class: The base class (unused; ``cls.__abstractmethods__`` is
                authoritative for which abstract methods remain unimplemented).

        Raises:
            PluginValidationError: If any abstract method is not implemented
        """
        # Python populates a concrete class's __abstractmethods__ with exactly the
        # abstract methods it has NOT overridden. Inheriting an abstract method
        # leaves the attribute present, so the old hasattr/None check never fired
        # (audit plugin_scanner.py:144).
        unimplemented = sorted(getattr(cls, "__abstractmethods__", frozenset()))
        if unimplemented:
            raise PluginValidationError(
                f"Plugin {cls.__name__} does not implement: {unimplemented}"
            )
