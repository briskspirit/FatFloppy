# src/fatfloppy/core/format_applier_registry.py
"""
Registry for format applier strategies.

Maps driver categories to their format application strategies.
"""
from typing import Dict, Type, Optional

from .format_application import FormatApplier
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class FormatApplierRegistry:
    """Registry for format applier strategies."""

    _registry: Dict[str, Type[FormatApplier]] = {}

    @classmethod
    def register(cls, category: str, applier_class: Type[FormatApplier]) -> None:
        """
        Registers a format applier for a driver category.

        Args:
            category: The driver category ("metadata_based", "raw", "physical")
            applier_class: The FormatApplier subclass
        """
        cls._registry[category] = applier_class
        logger.debug(f"Registered format applier for category: {category}")

    @classmethod
    def get(cls, category: str) -> Optional[Type[FormatApplier]]:
        """
        Retrieves a format applier for a driver category.

        Args:
            category: The driver category

        Returns:
            The FormatApplier class, or None if not found
        """
        return cls._registry.get(category)

    @classmethod
    def list_registered_categories(cls) -> list:
        """Returns a list of all registered categories."""
        return list(cls._registry.keys())


# Register built-in appliers
def _register_builtin_appliers():
    """Registers all built-in format appliers."""
    from .format_application import (
        MetadataBasedFormatApplier,
        RawImageFormatApplier,
        PhysicalDriveFormatApplier
    )

    FormatApplierRegistry.register("metadata_based", MetadataBasedFormatApplier)
    FormatApplierRegistry.register("raw", RawImageFormatApplier)
    FormatApplierRegistry.register("physical", PhysicalDriveFormatApplier)

    logger.info(f"Registered {len(FormatApplierRegistry.list_registered_categories())} format appliers")


_register_builtin_appliers()
