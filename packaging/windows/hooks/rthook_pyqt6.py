import os
import sys
from pathlib import Path

# Set up Qt plugin paths for PyInstaller frozen app on Windows
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    bundle_dir = Path(sys._MEIPASS)

    # Set Qt plugin path
    os.environ["QT_PLUGIN_PATH"] = str(bundle_dir / "PyQt6" / "Qt6" / "plugins")

    # Set QML2 import path (prevents warnings even though we don't use QML)
    os.environ["QML2_IMPORT_PATH"] = str(bundle_dir / "PyQt6" / "Qt6" / "qml")

    # Windows-specific: Ensure DLLs can be found
    # Add directories to the DLL search path (Python 3.8+ on Windows)
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(bundle_dir))
        os.add_dll_directory(str(bundle_dir / "PyQt6" / "Qt6" / "bin"))
