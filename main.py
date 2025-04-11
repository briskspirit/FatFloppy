# main.py

#!/usr/bin/env python3

import sys
import os
import argparse
from typing import Optional

from controller import DiskController

def main():
    parser = argparse.ArgumentParser(description="FatFloppy - Floppy Disk Browser and Utility")
    parser.add_argument('--cli', action='store_true', help='Use command-line interface (not yet implemented)')
    parser.add_argument('--image', type=str, help='Path to disk image file')
    parser.add_argument('--device', type=str, help='Physical device name (e.g., COM3)')
    args = parser.parse_args()

    if args.cli:
        run_cli(args)
    else:
        try:
            from gui import run_gui
            run_gui()
        except ImportError:
            print("GUI mode not available. Running in CLI mode.")
            run_cli(args)

def run_cli(args):
    controller = DiskController()

    # Open disk if specified
    disk_opened = False
    if args.image:
        print(f"Opening disk image: {args.image}")
        disk_opened = controller.open_disk(args.image, "image")
    elif args.device:
        print(f"Opening physical device: {args.device}")
        disk_opened = controller.open_disk(args.device, "physical")

    if not disk_opened:
        print("No disk opened. Exiting.")
        return

    print("Disk opened successfully.")

    # Show basic info
    format_name = controller.detect_format()
    if format_name:
        print(f"Detected format: {format_name}")
    else:
        print("Format not detected.")

    fs_type = controller.detect_filesystem()
    if fs_type:
        print(f"Detected filesystem: {fs_type}")

        # List root directory
        root_contents = controller.list_directory("/")
        if root_contents:
            print("\nRoot directory contents:")
            for item in root_contents:
                type_str = "<DIR>" if item["is_dir"] else "     "
                size_str = "" if item["is_dir"] else f"{item['size']} bytes"
                print(f"{type_str} {item['name']} {size_str} {item['datetime']} {item['attributes']}")
        else:
            print("Root directory is empty or could not be read.")
    else:
        print("No filesystem detected or supported.")

    # Clean up
    controller.close_disk()
    print("Disk closed.")

if __name__ == "__main__":
    main()
