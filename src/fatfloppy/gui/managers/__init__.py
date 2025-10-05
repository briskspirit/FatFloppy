# src/fatfloppy/gui/managers/__init__.py
"""
Manager classes for separating concerns in the GUI.
"""

from .disk_manager import DiskManager
from .editor_manager import EditorManager
from .file_manager import FileManager
from .settings_manager import SettingsManager

__all__ = [
    "DiskManager",
    "EditorManager",
    "FileManager",
    "SettingsManager",
]
