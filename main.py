#!/usr/bin/env python3
"""
FatFloppy - File browser for floppy drives connected to modern OSes with Greaseweazle board.
"""

import sys
import os
import argparse
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import Qt
from gui import FileBrowserApp
from cli import run_cui

# Set app name for system processes
if hasattr(sys, 'setappname'):
    sys.setappname('FatFloppy')

def main():
    parser = argparse.ArgumentParser(description="FatFloppy - Floppy Disk Browser")
    parser.add_argument('--cli', action='store_true', help='Use command-line interface instead of GUI')
    args = parser.parse_args()

    if args.cli:
        # Run the command-line interface
        run_cui()
    else:
        # Run the GUI
        # Set process name if possible (Linux)
        try:
            import setproctitle
            setproctitle.setproctitle('fatfloppy')
        except ImportError:
            pass  # Optional dependency

        # Force argv[0] to be the application name
        sys.argv[0] = 'FatFloppy'

        app = QApplication(sys.argv)

        # Set application name and organization
        app.setApplicationName("FatFloppy")
        app.setOrganizationName("FatFloppy")
        app.setOrganizationDomain("fatfloppy.org")
        app.setApplicationDisplayName("FatFloppy")

        # Disable the native menu bar on macOS
        app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)

        # Set application icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'icons', 'floppy_icon.png')
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))

        window = FileBrowserApp()
        window.show()
        sys.exit(app.exec())

if __name__ == "__main__":
    main()
