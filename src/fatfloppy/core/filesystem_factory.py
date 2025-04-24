# src/fatfloppy/core/filesystem_factory.py
from typing import Optional
from .disk import Disk
from .filesystem import Filesystem, FATFilesystem
from .utils.logging_config import get_logger

logger = get_logger(__name__)

def create_filesystem(disk: Disk) -> Optional[Filesystem]:
    try:
        fs = FATFilesystem(disk)
        if fs.is_valid():
            return fs
        else:
            logger.info("FATFilesystem is not valid for this disk")
            return None
    except Exception as e:
        logger.error(f"Error creating FATFilesystem: {e}")
    return None
