# src/fatfloppy/core/filesystem_factory.py
from typing import Optional, List, Type
from .disk import Disk
from .filesystems.fs_base import Filesystem
from .filesystems.fat12fs import FATFilesystem
from .filesystems.cpm_fs import CPMFilesystem # Add this import
from .utils.logging_config import get_logger

logger = get_logger(__name__)

FILESYSTEM_TYPES: List[Type[Filesystem]] = [
    FATFilesystem,
    CPMFilesystem, # Add CPMFilesystem here
    # CBMFilesystem,
]

# A minimum score required for a filesystem to be considered a valid candidate
MINIMUM_VALIDITY_SCORE = 30

def create_filesystem(disk: Disk) -> Optional[Filesystem]:
    if not disk or not disk.physical_format:
        logger.warning("Cannot create filesystem: Disk or physical format not available.")
        return None

    logger.debug(f"Attempting to detect filesystem on disk by scoring...")
    
    best_fs_instance: Optional[Filesystem] = None
    highest_score = -1
    all_scores = {}

    for fs_class in FILESYSTEM_TYPES:
        try:
            logger.debug(f"Scoring filesystem type: {fs_class.__name__}")
            fs_instance = fs_class(disk)
            score = fs_instance.get_validity_score()
            
            all_scores[fs_class.__name__] = score

            if score > highest_score:
                highest_score = score
                best_fs_instance = fs_instance

        except Exception as e:
            logger.error(f"Error while scoring {fs_class.__name__}: {e}", exc_info=False)
            all_scores[fs_class.__name__] = f"Error: {e}"

    # Log all scores for debugging purposes
    logger.debug(f"Filesystem scores: {all_scores}")

    if highest_score >= MINIMUM_VALIDITY_SCORE:
        logger.info(f"Selected best match: {best_fs_instance.__class__.__name__} with a score of {highest_score}.")
        return best_fs_instance
    else:
        logger.warning(f"No valid filesystem type detected (highest score {highest_score} was below threshold {MINIMUM_VALIDITY_SCORE}).")
        return None

def get_filesystem_class_by_type(fs_type_name: str) -> Optional[Type[Filesystem]]:
    for fs_class in FILESYSTEM_TYPES:
        if fs_type_name == "FAT12" and fs_class.__name__ == "FATFilesystem":
            return fs_class
        elif fs_type_name == "CPM" and fs_class.__name__ == "CPMFilesystem": # Add this condition
            return fs_class
    logger.warning(f"No filesystem class found for type '{fs_type_name}'")
    return None
