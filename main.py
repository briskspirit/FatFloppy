#!/usr/bin/env python3

import sys
import os
import argparse
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import Qt
from gui import FileBrowserApp
# TODO: Fix CLI
# from cli import run_cui

if hasattr(sys, 'setappname'):
    sys.setappname('FatFloppy')

def main():
    parser = argparse.ArgumentParser(description="FatFloppy - Floppy Disk Browser")
    parser.add_argument('--cli', action='store_true', help='Use command-line interface instead of GUI')
    args = parser.parse_args()

    if args.cli:
        # run_cui()
        print("CLI mode is not yet implemented.")
        sys.exit(1)
    else:
        sys.argv[0] = 'FatFloppy'
        app = QApplication(sys.argv)
        app.setApplicationName("FatFloppy")
        app.setOrganizationName("FatFloppy")
        app.setOrganizationDomain("fatfloppy.org")
        app.setApplicationDisplayName("FatFloppy")
        app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'icons', 'floppy_icon.png')
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))

        window = FileBrowserApp()
        window.show()
        sys.exit(app.exec())

if __name__ == "__main__":
    main()
