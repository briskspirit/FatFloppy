# src/fatfloppy/core/filesystem_factory.py
from typing import Optional, List, Type
from .disk import Disk
from .filesystems.fs_base import Filesystem
from .filesystems.fat12fs import FATFilesystem
from .utils.logging_config import get_logger

logger = get_logger(__name__)

FILESYSTEM_TYPES: List[Type[Filesystem]] = [
    FATFilesystem,
    # CBMFilesystem,
    # CPMFilesystem,
]

def create_filesystem(disk: Disk) -> Optional[Filesystem]:
    if not disk or not disk.physical_format:
        logger.warning("Cannot create filesystem: Disk or physical format not available.")
        return None

    logger.debug(f"Attempting to detect filesystem on disk...")

    for fs_class in FILESYSTEM_TYPES:
        try:
            logger.debug(f"Trying filesystem type: {fs_class.__name__}")
            fs_instance = fs_class(disk)

            if fs_instance.is_valid():
                logger.info(f"Detected valid {fs_class.__name__} filesystem.")
                return fs_instance
            else:
                logger.debug(f"{fs_class.__name__} check failed or structure invalid.")

        except Exception as e:
            logger.error(f"Error while checking {fs_class.__name__}: {e}", exc_info=False)

    logger.warning("No valid filesystem type detected.")
    return None
