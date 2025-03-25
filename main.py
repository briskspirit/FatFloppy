#!/usr/bin/env python3
"""
FatFloppy - File browser for floppy drives connected to modern OSes with Greaseweazle board.
"""

import sys
import argparse
from PyQt6.QtWidgets import QApplication
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
        window = FileBrowserApp()
        window.show()
        sys.exit(app.exec())

if __name__ == "__main__":
    main()
