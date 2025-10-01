# src/fatfloppy/core/plugin_scanner.py
"""
Automatic plugin discovery system.

Scans specified directories for valid plugin classes and validates them.
"""
import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Type, List, Dict, Any, Callable
from abc import ABCMeta

from .utils.logging_config import get_logger

logger = get_logger(__name__)


class PluginValidationError(Exception):
    """Raised when a plugin fails validation."""
    pass


class PluginScanner:
    """Discovers and validates plugins from specified directories."""

    @staticmethod
    def discover_plugins(
        package_name: str,
        base_class: Type,
        validator: Callable[[Type], None] = None
    ) -> List[Type]:
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
            # Import the package
            package = importlib.import_module(package_name)
            package_path = Path(package.__file__).parent

            # Scan all Python files in the directory
            for finder, name, ispkg in pkgutil.iter_modules([str(package_path)]):
                if name.startswith('_'):
                    continue  # Skip private modules

                module_name = f"{package_name}.{name}"

                try:
                    module = importlib.import_module(module_name)

                    # Find all classes that inherit from base_class
                    for item_name, item in inspect.getmembers(module, inspect.isclass):
                        # Must be subclass but not the base class itself
                        if (issubclass(item, base_class) and
                            item is not base_class and
                            not inspect.isabstract(item)):

                            # Run validation if provided
                            if validator:
                                try:
                                    validator(item)
                                except Exception as e:
                                    logger.error(
                                        f"Plugin validation failed for {item.__name__} "
                                        f"in {module_name}: {e}"
                                    )
                                    raise PluginValidationError(
                                        f"{item.__name__} failed validation: {e}"
                                    ) from e

                            discovered_plugins.append(item)
                            logger.debug(f"Discovered plugin: {item.__name__} from {module_name}")

                except ImportError as e:
                    logger.warning(f"Failed to import module {module_name}: {e}")
                except Exception as e:
                    logger.error(f"Error processing module {module_name}: {e}")

        except ImportError as e:
            logger.error(f"Failed to import package {package_name}: {e}")
            return []

        return discovered_plugins

    @staticmethod
    def validate_has_attributes(cls: Type, required_attrs: List[str]) -> None:
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
            if not hasattr(cls, attr):
                missing.append(attr)
            elif getattr(cls, attr) == "" or getattr(cls, attr) == []:
                # Empty string or list also counts as missing
                missing.append(attr)

        if missing:
            raise PluginValidationError(
                f"Plugin {cls.__name__} missing required attributes: {missing}"
            )

    @staticmethod
    def validate_implements_methods(cls: Type, base_class: Type) -> None:
        """
        Validates that a class implements all abstract methods from base.

        Args:
            cls: The class to validate
            base_class: The base class with abstract methods

        Raises:
            PluginValidationError: If any abstract method is not implemented
        """
        abstract_methods = set()

        # Collect all abstract methods from base class and its parents
        for base in inspect.getmro(base_class):
            if hasattr(base, '__abstractmethods__'):
                abstract_methods.update(base.__abstractmethods__)

        # Check if all are implemented
        unimplemented = []
        for method_name in abstract_methods:
            if not hasattr(cls, method_name) or getattr(cls, method_name) is None:
                unimplemented.append(method_name)

        if unimplemented:
            raise PluginValidationError(
                f"Plugin {cls.__name__} does not implement: {unimplemented}"
            )
