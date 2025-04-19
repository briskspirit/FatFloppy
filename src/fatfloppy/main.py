#!/usr/bin/env python3
from src.fatfloppy.core.utils.logging_config import get_logger

logger = get_logger()

def main():
    logger.info("Starting FatFloppy application")

    try:
        from src.fatfloppy.gui import run_gui
        logger.info("Starting GUI mode")
        run_gui()
    except ImportError as e:
        logger.error(f"Failed to import GUI module: {e}")

if __name__ == "__main__":
    main()
