#!/usr/bin/env python3
"""
FatFloppy - File browser for floppy drives connected to modern OSes with Greaseweazle board.
"""

import sys
import os
import argparse
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from gui import FileBrowserApp
from cli import run_cui

def main():
    parser = argparse.ArgumentParser(description="FatFloppy - Floppy Disk Browser")
    parser.add_argument('--cli', action='store_true', help='Use command-line interface instead of GUI')
    args = parser.parse_args()

    if args.cli:
        # Run the command-line interface
        run_cui()
    else:
        # Run the GUI
        app = QApplication(sys.argv)

        # Set application name and organization
        app.setApplicationName("FatFloppy")
        app.setOrganizationName("FatFloppy")

        # Set application icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'icons', 'floppy_icon.png')
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))

        window = FileBrowserApp()
        window.show()
        sys.exit(app.exec())

if __name__ == "__main__":
    main()
