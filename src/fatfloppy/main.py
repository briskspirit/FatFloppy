#!/usr/bin/env python3

import argparse

from src.fatfloppy.utils.logging_config import get_logger
from src.fatfloppy.core.controller import DiskController

logger = get_logger()

def main():
    logger.info("Starting FatFloppy application")
    parser = argparse.ArgumentParser(description="FatFloppy - Floppy Disk Browser and Utility")
    parser.add_argument('--cli', action='store_true', help='Use command-line interface (not yet implemented)')
    parser.add_argument('--image', type=str, help='Path to disk image file')
    parser.add_argument('--device', type=str, help='Physical device name (e.g., COM3)')
    args = parser.parse_args()

    logger.debug(f"Command line arguments: {args}")

    if args.cli:
        run_cli(args)
    else:
        try:
            from src.fatfloppy.gui import run_gui
            logger.info("Starting GUI mode")
            run_gui()
        except ImportError as e:
            logger.error(f"Failed to import GUI module: {e}")
            logger.info("Falling back to CLI mode")
            run_cli(args)

def run_cli(args):
    logger.info("Running in CLI mode")
    controller = DiskController()

    # Open disk if specified
    disk_opened = False
    if args.image:
        logger.info(f"Opening disk image: {args.image}")
        disk_opened = controller.open_disk(args.image, "image")
    elif args.device:
        logger.info(f"Opening physical device: {args.device}")
        disk_opened = controller.open_disk(args.device, "physical")

    if not disk_opened:
        logger.warning("No disk opened. Exiting.")
        return

    logger.info("Disk opened successfully.")

    # Show basic info
    format_name = controller.detect_format()
    if format_name:
        logger.info(f"Detected format: {format_name}")
    else:
        logger.warning("Format not detected.")

    fs_type = controller.detect_filesystem()
    if fs_type:
        logger.info(f"Detected filesystem: {fs_type}")

        # List root directory
        root_contents = controller.list_directory("/")
        if root_contents:
            logger.info("Root directory contents:")
            for item in root_contents:
                type_str = "<DIR>" if item["is_dir"] else "     "
                size_str = "" if item["is_dir"] else f"{item['size']} bytes"
                logger.info(f"{type_str} {item['name']} {size_str} {item['datetime']} {item['attributes']}")
        else:
            logger.warning("Root directory is empty or could not be read.")
    else:
        logger.warning("No filesystem detected or supported.")

    # Clean up
    controller.close_disk()
    logger.info("Disk closed.")

if __name__ == "__main__":
    main()
