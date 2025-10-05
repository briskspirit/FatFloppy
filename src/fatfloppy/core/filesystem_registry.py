# src/fatfloppy/core/filesystem_registry.py
"""
Self-contained filesystem registry with automatic plugin discovery.

This module is completely self-sufficient - it discovers, validates,
and registers all filesystem plugins automatically on import.
"""
from typing import Dict, List, Optional, Type

from .filesystems.fs_base import Filesystem
from .format_profile import FormatProfile
from .plugin_scanner import PluginScanner, PluginValidationError
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class FilesystemRegistry:
    """Registry for filesystem implementations with auto-discovery."""

    _registry: Dict[str, Type[Filesystem]] = {}
    _name_map: Dict[str, str] = {}
    _initialized: bool = False
    _all_formats: Dict[str, FormatProfile] = {}

    @classmethod
    def get_all(cls) -> List[Type[Filesystem]]:
        """
        Returns a list of all registered filesystem classes.

        Returns:
            List of all registered Filesystem classes.
        """
        if not cls._initialized:
            cls._discover_and_register()
        return list(cls._registry.values())

    @classmethod
    def get_all_formats(cls) -> Dict[str, FormatProfile]:
        """
        Returns all format definitions from all filesystem plugins.

        Returns:
            Dictionary mapping format names to FormatProfile objects
        """
        if not cls._initialized:
            cls._discover_and_register()
        return cls._all_formats.copy()

    @classmethod
    def get_by_name(cls, name: str) -> Optional[Type[Filesystem]]:
        """
        Retrieves a filesystem class by name or alias.

        Args:
            name: The filesystem type name or alias

        Returns:
            The corresponding Filesystem class, or None if not found
        """
        if not cls._initialized:
            cls._discover_and_register()

        internal_name = cls._name_map.get(name)
        if internal_name:
            return cls._registry.get(internal_name)

        logger.warning(f"Filesystem type '{name}' not found in registry")
        return None

    @classmethod
    def list_registered_types(cls) -> List[str]:
        """
        Returns a list of all registered filesystem type names.

        Returns:
            List of filesystem type names.
        """
        if not cls._initialized:
            cls._discover_and_register()
        return list(cls._registry.keys())

    @classmethod
    def register_external(
        cls,
        fs_type: str,
        fs_class: Type[Filesystem],
        aliases: Optional[List[str]] = None
    ) -> None:
        """
        Public API for external plugins to register themselves.

        Args:
            fs_type: The filesystem type name
            fs_class: The filesystem class
            aliases: Optional list of aliases

        Raises:
            PluginValidationError: If validation fails
        """
        cls._validate_filesystem(fs_class)

        cls._registry[fs_type] = fs_class
        cls._name_map[fs_type] = fs_type

        if aliases:
            for alias in aliases:
                cls._name_map[alias] = fs_type

        logger.info(f"Externally registered filesystem: {fs_type}")

    @classmethod
    def _aggregate_format_definitions(cls) -> None:
        """Collects format definitions from all registered filesystem plugins."""
        logger.info("Aggregating format definitions from filesystem plugins...")

        for fs_type, fs_class in cls._registry.items():
            if hasattr(fs_class, 'get_format_definitions'):
                try:
                    formats = fs_class.get_format_definitions()
                    if formats:
                        cls._all_formats.update(formats)
                        logger.info(
                            f"Loaded {len(formats)} formats from {fs_class.__name__}"
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to load formats from {fs_class.__name__}: {e}"
                    )

        logger.info(f"Total formats registered: {len(cls._all_formats)}")

    @classmethod
    def _discover_and_register(cls) -> None:
        """Discovers and registers all filesystem plugins."""
        if cls._initialized:
            return

        logger.info("Starting filesystem plugin discovery...")

        filesystems = PluginScanner.discover_plugins(
            package_name='fatfloppy.core.filesystems',
            base_class=Filesystem,
            validator=cls._validate_filesystem
        )

        for fs_class in filesystems:
            fs_type = fs_class.filesystem_type
            aliases = getattr(fs_class, 'filesystem_aliases', [])

            cls._registry[fs_type] = fs_class
            cls._name_map[fs_type] = fs_type

            for alias in aliases:
                cls._name_map[alias] = fs_type

            logger.info(
                f"Registered filesystem: {fs_class.__name__} "
                f"(type={fs_type}, aliases={aliases})"
            )

        cls._aggregate_format_definitions()

        cls._initialized = True
        logger.info(
            f"Filesystem discovery complete. Registered {len(cls._registry)} "
            f"filesystems."
        )

    @classmethod
    def _validate_filesystem(cls, fs_class: Type[Filesystem]) -> None:
        """
        Validates a filesystem plugin meets all requirements.

        Args:
            fs_class: The filesystem class to validate

        Raises:
            PluginValidationError: If validation fails
        """
        PluginScanner.validate_has_attributes(
            fs_class,
            ['filesystem_type', 'validity_threshold']
        )

        PluginScanner.validate_implements_methods(fs_class, Filesystem)

        if not isinstance(fs_class.validity_threshold, int):
            raise PluginValidationError(
                f"{fs_class.__name__}.validity_threshold must be an integer"
            )

        if not (0 <= fs_class.validity_threshold <= 100):
            raise PluginValidationError(
                f"{fs_class.__name__}.validity_threshold must be between 0-100"
            )


FilesystemRegistry._discover_and_register()
