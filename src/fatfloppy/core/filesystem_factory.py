# src/fatfloppy/core/filesystem_factory.py
"""
Provides factory functions for creating and identifying filesystem handlers.

This module uses the FilesystemRegistry for all filesystem operations.
"""

from typing import Optional

from .disk import Disk
from .filesystem_registry import FilesystemRegistry
from .filesystems.fs_base import Filesystem
from .utils.logging_config import get_logger

logger = get_logger(__name__)


def create_filesystem(disk: Disk) -> Optional[Filesystem]:
    """
    Detects and instantiates the most appropriate filesystem for a given disk.

    A filesystem is only claimed when its score reaches its own
    ``validity_threshold``; among claimable candidates, the highest score wins.

    Args:
        disk: The Disk object for which to create a filesystem handler

    Returns:
        An instance of a Filesystem subclass if suitable, otherwise None
    """
    if not disk or not disk.physical_format:
        logger.warning(
            "Cannot create filesystem: Disk or physical format not available."
        )
        return None

    logger.debug("Attempting to detect filesystem on disk by scoring...")

    best_fs_instance: Optional[Filesystem] = None
    highest_score: int = -1
    all_scores: dict = {}
    original_pf = disk.physical_format

    for fs_class in FilesystemRegistry.get_all():
        try:
            logger.debug(f"Scoring filesystem type: {fs_class.__name__}")

            if hasattr(fs_class, "get_canonical_format") and callable(
                fs_class.get_canonical_format
            ):
                canonical_format = fs_class.get_canonical_format()
                if canonical_format.physical_format != original_pf:
                    logger.debug(
                        f"Applying canonical geometry for {fs_class.__name__} scoring."
                    )
                    disk.set_geometry(canonical_format.physical_format)

            fs_instance = fs_class(disk)
            score = fs_instance.get_validity_score()
            all_scores[fs_class.__name__] = score

            if score >= fs_class.validity_threshold and score > highest_score:
                highest_score = score
                best_fs_instance = fs_instance

        except Exception as e:
            logger.error(
                f"Error while scoring {fs_class.__name__}: {e}", exc_info=False
            )
            all_scores[fs_class.__name__] = f"Error: {e}"
        finally:
            if disk.physical_format != original_pf:
                disk.set_geometry(original_pf)

    logger.debug(f"Filesystem scores: {all_scores}")

    if best_fs_instance:
        logger.info(
            f"Selected best match: {best_fs_instance.__class__.__name__} "
            f"with score {highest_score}."
        )
        if disk.physical_format != original_pf:
            best_fs_instance = type(best_fs_instance)(disk)
            best_fs_instance.get_validity_score()
        return best_fs_instance

    logger.warning(
        f"No filesystem reached its own validity threshold (scores: {all_scores})."
    )
    return None


def get_filesystem_class_by_type(fs_type_name: str) -> Optional[type[Filesystem]]:
    """
    Retrieves a filesystem class from the registry by its type name.

    Args:
        fs_type_name: The name of the filesystem type

    Returns:
        The corresponding Filesystem subclass, or None if not found
    """
    return FilesystemRegistry.get_by_name(fs_type_name)
