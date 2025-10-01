# src/fatfloppy/core/filesystem_registry.py
"""
Registry for filesystem implementations.

This module provides a centralized registry pattern for filesystem types,
allowing new filesystem implementations to be registered dynamically without
modifying core controller logic.
"""

from typing import Dict, Type, List, Optional

from .filesystems.fs_base import Filesystem
from .utils.logging_config import get_logger

logger = get_logger(__name__)


class FilesystemRegistry:
    """
    A registry for filesystem implementation classes.

    This allows filesystem types to be registered once and then discovered
    dynamically throughout the application without hardcoded type checks.
    """

    _registry: Dict[str, Type[Filesystem]] = {}
    _name_map: Dict[str, str] = {}  # Friendly name -> internal name mapping

    @classmethod
    def register(
        cls,
        fs_type: str,
        fs_class: Type[Filesystem],
        aliases: Optional[List[str]] = None
    ) -> None:
        """
        Registers a filesystem implementation class.

        Args:
            fs_type: The internal type name (typically the class name).
            fs_class: The filesystem class to register.
            aliases: Optional list of friendly names for this filesystem
                    (e.g., ["FAT12", "FAT"] for FATFilesystem).

        Example:
            FilesystemRegistry.register(
                "FATFilesystem",
                FATFilesystem,
                aliases=["FAT12", "FAT"]
            )
        """
        cls._registry[fs_type] = fs_class
        cls._name_map[fs_type] = fs_type

        if aliases:
            for alias in aliases:
                cls._name_map[alias] = fs_type

        logger.debug(f"Registered filesystem type: {fs_type} with aliases: {aliases}")

    @classmethod
    def get_by_name(cls, name: str) -> Optional[Type[Filesystem]]:
        """
        Retrieves a filesystem class by name or alias.

        Args:
            name: The filesystem type name or alias (e.g., "FAT12", "FATFilesystem").

        Returns:
            The corresponding Filesystem class, or None if not found.
        """
        internal_name = cls._name_map.get(name)
        if internal_name:
            return cls._registry.get(internal_name)
        logger.warning(f"Filesystem type '{name}' not found in registry")
        return None

    @classmethod
    def get_all(cls) -> List[Type[Filesystem]]:
        """
        Returns a list of all registered filesystem classes.

        Returns:
            A list of Filesystem class types.
        """
        return list(cls._registry.values())

    @classmethod
    def list_registered_types(cls) -> List[str]:
        """
        Returns a list of all registered filesystem type names.

        Returns:
            A list of filesystem type identifiers.
        """
        return list(cls._registry.keys())


# ============================================================================
# Auto-register Built-in Filesystems
# ============================================================================

def _register_builtin_filesystems() -> None:
    """Registers all built-in filesystem implementations."""
    from .filesystems.fat12fs import FATFilesystem
    from .filesystems.cpm_fs import CPMFilesystem
    from .filesystems.hdos_fs import HDOSFilesystem

    FilesystemRegistry.register(
        "FATFilesystem",
        FATFilesystem,
        aliases=["FAT12", "FAT"]
    )

    FilesystemRegistry.register(
        "CPMFilesystem",
        CPMFilesystem,
        aliases=["CPM", "CP/M"]
    )

    FilesystemRegistry.register(
        "HDOSFilesystem",
        HDOSFilesystem,
        aliases=["HDOS"]
    )

    logger.info(f"Registered {len(FilesystemRegistry.list_registered_types())} built-in filesystem types")


# Register built-in filesystems when module is imported
_register_builtin_filesystems()
