#!/usr/bin/env python3
"""
Main entry point for the FatFloppy application.
"""
import os
import sys

if sys.platform == 'darwin':
    os.environ['RESOURCE_NAME'] = 'FatFloppy'

from logging import Logger

from fatfloppy.core.utils.logging_config import get_logger

logger: Logger = get_logger()


def main() -> None:
    """Initializes and starts the FatFloppy application."""
    logger.info("Starting FatFloppy application")
    try:
        from fatfloppy.gui import run_gui

        logger.info("Starting GUI mode")
        run_gui()
    except ImportError as e:
        logger.critical(f"Failed to import and run the GUI module: {e}")
    except Exception as e:
        logger.critical(
            f"An unexpected error occurred during application startup: {e}",
            exc_info=True
        )


if __name__ == "__main__":
    main()
