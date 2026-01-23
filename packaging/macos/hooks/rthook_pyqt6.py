import os
import sys
from pathlib import Path

# Disable problematic Qt plugins before Qt loads
os.environ["QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"] = "0"
os.environ["QT_MAC_WANTS_LAYER"] = "1"

# Set up Qt plugin paths for PyInstaller frozen app
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    bundle_dir = Path(sys._MEIPASS)

    # Set Qt plugin path
    os.environ["QT_PLUGIN_PATH"] = str(bundle_dir / "PyQt6" / "Qt6" / "plugins")

    # Set QML2 import path
    os.environ["QML2_IMPORT_PATH"] = str(bundle_dir / "PyQt6" / "Qt6" / "qml")

    # Set app bundle path for Qt
    os.environ["QT_MAC_FRAMEWORK_SEARCH_PATH"] = str(
        bundle_dir / "PyQt6" / "Qt6" / "lib"
    )
