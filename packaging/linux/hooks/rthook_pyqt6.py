import os
import sys
from pathlib import Path

# Set up Qt plugin paths for PyInstaller frozen app
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    bundle_dir = Path(sys._MEIPASS)

    # Set Qt plugin path
    os.environ["QT_PLUGIN_PATH"] = str(bundle_dir / "PyQt6" / "Qt6" / "plugins")

    # Set QML2 import path
    os.environ["QML2_IMPORT_PATH"] = str(bundle_dir / "PyQt6" / "Qt6" / "qml")

    # Add Qt lib directory to LD_LIBRARY_PATH for Linux
    qt_lib_path = str(bundle_dir / "PyQt6" / "Qt6" / "lib")
    current_ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    if current_ld_path:
        os.environ["LD_LIBRARY_PATH"] = f"{qt_lib_path}:{current_ld_path}"
    else:
        os.environ["LD_LIBRARY_PATH"] = qt_lib_path
