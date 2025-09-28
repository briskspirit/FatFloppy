"""
Provides factory functions for creating and identifying filesystem handlers.

This module contains the logic for dynamically selecting the correct filesystem
implementation (e.g., FAT12, CP/M) based on scoring the contents of a disk. It
maintains a registry of available filesystem types and provides utilities to
instantiate them.

Functions:
    create_filesystem: Detects and creates the most likely filesystem for a disk.
    get_filesystem_class_by_type: Retrieves a filesystem class by its name.
"""
from typing import Optional, List, Type

from .disk import Disk
from .filesystems.fs_base import Filesystem
from .filesystems.fat12fs import FATFilesystem
from .filesystems.cpm_fs import CPMFilesystem
from .filesystems.hdos_fs import HDOSFilesystem
from .utils.logging_config import get_logger

logger = get_logger(__name__)

# A registry of all available filesystem implementation classes.
FILESYSTEM_TYPES: List[Type[Filesystem]] = [
    FATFilesystem,
    CPMFilesystem,
    HDOSFilesystem,
    # CBMFilesystem, # Example of where another FS would be added
]

# A minimum score required for a filesystem to be considered a valid candidate.
MINIMUM_VALIDITY_SCORE = 30


def create_filesystem(disk: Disk) -> Optional[Filesystem]:
    """
    Detects and instantiates the most appropriate filesystem for a given disk.

    This function iterates through all registered filesystem types, calculates a
    validity score for each one against the provided disk, and returns an
    instance of the class with the highest score, provided it meets the minimum
    threshold.

    Args:
        disk: The Disk object for which to create a filesystem handler.

    Returns:
        An instance of a Filesystem subclass if a suitable one is found,
        otherwise None.
    """
    if not disk or not disk.physical_format:
        logger.warning("Cannot create filesystem: Disk or physical format not available.")
        return None

    logger.debug("Attempting to detect filesystem on disk by scoring...")

    best_fs_instance: Optional[Filesystem] = None
    highest_score: int = -1
    all_scores: dict = {}

    for fs_class in FILESYSTEM_TYPES:
        original_pf = disk.physical_format
        try:
            logger.debug(f"Scoring filesystem type: {fs_class.__name__}")
            
            # Temporarily apply a canonical geometry if the FS provides one
            if hasattr(fs_class, 'get_canonical_format') and callable(getattr(fs_class, 'get_canonical_format')):
                canonical_format = fs_class.get_canonical_format()
                if canonical_format.physical_format != original_pf:
                    logger.debug(f"Applying canonical geometry for {fs_class.__name__} scoring.")
                    disk.set_geometry(canonical_format.physical_format)

            fs_instance = fs_class(disk)
            score = fs_instance.get_validity_score()
            all_scores[fs_class.__name__] = score

            if score > highest_score:
                highest_score = score
                best_fs_instance = fs_instance

        except Exception as e:
            logger.error(f"Error while scoring {fs_class.__name__}: {e}", exc_info=False)
            all_scores[fs_class.__name__] = f"Error: {e}"
        finally:
            # Always restore the original geometry
            if disk.physical_format != original_pf:
                disk.set_geometry(original_pf)


    # Log all scores for debugging purposes
    logger.debug(f"Filesystem scores: {all_scores}")

    if highest_score >= MINIMUM_VALIDITY_SCORE and best_fs_instance:
        logger.info(f"Selected best match: {best_fs_instance.__class__.__name__} with a score of {highest_score}.")
        # Re-initialize the best instance with the final, correct geometry
        if disk.physical_format != original_pf:
            best_fs_instance = type(best_fs_instance)(disk)
            best_fs_instance.get_validity_score() # Re-run to initialize internal state
        return best_fs_instance

    logger.warning(f"No valid filesystem type detected (highest score {highest_score} was below threshold {MINIMUM_VALIDITY_SCORE}).")
    return None


def get_filesystem_class_by_type(fs_type_name: str) -> Optional[Type[Filesystem]]:
    """
    Retrieves a filesystem class from the registry by its type name.

    Args:
        fs_type_name: The name of the filesystem type (e.g., "FAT12", "CPM").

    Returns:
        The corresponding Filesystem subclass, or None if not found.
    """
    for fs_class in FILESYSTEM_TYPES:
        # This mapping allows for user-friendly names like "FAT12" to map
        # to the internal class name "FATFilesystem".
        if fs_type_name == "FAT12" and fs_class.__name__ == "FATFilesystem":
            return fs_class
        if fs_type_name == "CPM" and fs_class.__name__ == "CPMFilesystem":
            return fs_class
        if fs_type_name == "HDOS" and fs_class.__name__ == "HDOSFilesystem":
            return fs_class

    logger.warning(f"No filesystem class found for type '{fs_type_name}'")
    return None
