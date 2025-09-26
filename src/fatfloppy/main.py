#!/usr/bin/env python3
"""
Main entry point for the FatFloppy application.

This script initializes the application's logging configuration and launches the
graphical user interface (GUI). It is designed to be executed directly to
start the program.
"""

from logging import Logger

from src.fatfloppy.core.utils.logging_config import get_logger

logger: Logger = get_logger()


def main() -> None:
    """
    Initializes and starts the FatFloppy application.

    This function logs the application start and attempts to import and run the
    GUI. If the GUI components cannot be imported, it logs a critical error,
    as the application cannot proceed.
    """
    logger.info("Starting FatFloppy application")
    try:
        from src.fatfloppy.gui import run_gui
        logger.info("Starting GUI mode")
        run_gui()
    except ImportError as e:
        logger.critical(f"Failed to import and run the GUI module: {e}")
    except Exception as e:
        logger.critical(f"An unexpected error occurred during application startup: {e}", exc_info=True)


if __name__ == "__main__":
    main()
