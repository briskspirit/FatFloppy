# src/fatfloppy/gui/managers/settings_manager.py
"""
Manages application settings, window state, and recent files.
"""

import logging
import os
import subprocess
import sys

from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox


class SettingsManager(QObject):
    """Handles application settings, window state persistence, and recent files."""

    recent_files_changed = pyqtSignal()
    open_file_requested = pyqtSignal(str)

    def __init__(self, parent: 'QMainWindow', max_recent_files: int = 10) -> None:
        """
        Initialize the settings manager.

        Args:
            parent: The main window that owns this manager.
            max_recent_files: Maximum number of recent files to track.
        """
        super().__init__(parent)
        self.parent = parent
        self.logger: logging.Logger = parent.logger
        self.max_recent_files = max_recent_files

    def add_to_recent_files(self, file_path: str) -> None:
        """
        Adds a file to the recent files list.

        Args:
            file_path: The absolute path to the file to add.
        """
        if not file_path or not os.path.exists(file_path):
            return

        recent_files = self.load_recent_files()

        if file_path in recent_files:
            recent_files.remove(file_path)

        recent_files.insert(0, file_path)
        recent_files = recent_files[:self.max_recent_files]

        self.save_recent_files(recent_files)
        self.recent_files_changed.emit()
        self.logger.debug(f"Added '{file_path}' to recent files")

    def clear_recent_files(self) -> None:
        """Clears the recent files list."""
        self.save_recent_files([])
        self.recent_files_changed.emit()
        self.logger.info("Cleared recent files list")

    def load_recent_files(self) -> list[str]:
        """
        Loads the list of recent files from settings.

        Returns:
            List of file paths.
        """
        settings = QSettings("FatFloppy", "FatFloppy")
        recent = settings.value("recent_files", [])
        if recent is None:
            recent = []
        elif isinstance(recent, str):
            recent = [recent]
        self.logger.debug(f"Loaded {len(recent)} recent files from settings")
        return recent

    def load_window_state(self) -> None:
        """
        Loads and restores window geometry and optionally dock positions from settings.
        """
        settings = QSettings("FatFloppy", "FatFloppy")

        geometry = settings.value("window/geometry")
        if geometry:
            self.parent.restoreGeometry(geometry)
            self.logger.debug("Restored window geometry from settings")
        else:
            self.parent.resize(1400, 900)
            self.logger.debug("Using default window size")

        restore_dock_layout = settings.value(
            "window/restore_dock_layout",
            False,
            type=bool
        )

        if restore_dock_layout:
            state = settings.value("window/state")
            if state:
                success = self.parent.restoreState(state)
                if success:
                    self.logger.info(
                        f"Restored custom dock layout (state size: "
                        f"{len(state)} bytes)"
                    )

                    floating_geometries = settings.value(
                        "window/floating_geometries"
                    )
                    if floating_geometries:
                        for dock_name, dock_geometry in floating_geometries.items():
                            dock = self.parent.findChild(
                                type(self.parent.tree_dock),
                                dock_name
                            )
                            if dock and dock.isFloating():
                                dock.restoreGeometry(dock_geometry)
                                self.logger.debug(
                                    f"Restored floating geometry for {dock_name}"
                                )
                else:
                    self.logger.warning(
                        "Failed to restore custom dock layout - state may be "
                        "corrupted"
                    )
            else:
                self.logger.warning(
                    "Custom dock layout enabled but no saved state found - "
                    "using defaults"
                )
        else:
            self.logger.info("Using default dock layout")

        self.logger.info("Window state loaded")

    def reset_layout(self) -> bool:
        """
        Resets the dock layout to default configuration.

        Returns:
            True if user confirmed and restart initiated, False otherwise.
        """
        reply = QMessageBox.question(
            self.parent,
            "Reset Layout",
            "Reset all dock positions to default layout?\n\n"
            "The application will restart to apply changes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return False

        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("window/restore_dock_layout", False)
        settings.remove("window/state")

        if hasattr(self.parent, 'restore_layout_action'):
            self.parent.restore_layout_action.setChecked(False)

        self.logger.info("Layout reset to defaults, restart required")

        restart_reply = QMessageBox.question(
            self.parent,
            "Restart Required",
            "Layout has been reset to defaults.\n\n"
            "Restart now to apply changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if restart_reply == QMessageBox.StandardButton.Yes:
            if hasattr(self.parent, 'editor_manager'):
                if self.parent.editor_manager.has_unsaved_changes():
                    save_reply = QMessageBox.question(
                        self.parent,
                        "Unsaved Changes",
                        "Save changes in text editor before restarting?",
                        QMessageBox.StandardButton.Yes |
                        QMessageBox.StandardButton.No |
                        QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Yes
                    )
                    if save_reply == QMessageBox.StandardButton.Yes:
                        if not self.parent.editor_manager.save_file():
                            return False
                    elif save_reply == QMessageBox.StandardButton.Cancel:
                        return False

            QApplication.quit()
            subprocess.Popen([sys.executable] + sys.argv)
            return True

        return False

    def save_current_layout(self) -> None:
        """Saves the current dock layout exactly as it is."""
        settings = QSettings("FatFloppy", "FatFloppy")
        current_state = self.parent.saveState()
        settings.setValue("window/state", current_state)
        settings.setValue("window/restore_dock_layout", True)

        if hasattr(self.parent, 'restore_layout_action'):
            self.parent.restore_layout_action.setChecked(True)

        floating_geometries = {}
        dock_names = [
            "TreeDock",
            "DiskInfoDock",
            "FileListDock",
            "DiskMapDock",
            "TextEditorDock",
            "HexViewerDock"
        ]
        for dock_name in dock_names:
            dock = self.parent.findChild(type(self.parent.tree_dock), dock_name)
            if dock and dock.isFloating():
                floating_geometries[dock_name] = dock.saveGeometry()

        if floating_geometries:
            settings.setValue("window/floating_geometries", floating_geometries)
            self.logger.info(
                f"Saved custom dock layout with {len(floating_geometries)} "
                f"floating dock(s)"
            )
        else:
            settings.remove("window/floating_geometries")
            self.logger.info(
                f"Saved custom dock layout (state size: {len(current_state)} "
                f"bytes)"
            )

        QMessageBox.information(
            self.parent,
            "Layout Saved",
            "Current layout saved and will be restored on next startup."
        )

    def save_recent_files(self, recent_files: list[str]) -> None:
        """
        Saves the list of recent files to settings.

        Args:
            recent_files: List of file paths to save.
        """
        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("recent_files", recent_files)
        self.logger.debug(f"Saved {len(recent_files)} recent files to settings")

    def save_window_state(self) -> None:
        """Saves the current window geometry and dock state to settings."""
        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("window/geometry", self.parent.saveGeometry())

        floating_geometries = {}
        dock_names = [
            "TreeDock",
            "DiskInfoDock",
            "FileListDock",
            "DiskMapDock",
            "TextEditorDock",
            "HexViewerDock"
        ]
        for dock_name in dock_names:
            dock = self.parent.findChild(type(self.parent.tree_dock), dock_name)
            if dock and dock.isFloating():
                floating_geometries[dock_name] = dock.saveGeometry()

        if floating_geometries:
            settings.setValue("window/floating_geometries", floating_geometries)
        else:
            settings.remove("window/floating_geometries")

        self.logger.debug("Saved window geometry (dock state NOT auto-saved)")

    def toggle_restore_layout(self, checked: bool) -> None:
        """
        Toggles whether custom layout should be restored on startup.

        Args:
            checked: True to enable custom layout restoration.
        """
        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("window/restore_dock_layout", checked)

        if checked:
            QMessageBox.information(
                self.parent,
                "Custom Layout Enabled",
                "Custom layout restoration is now enabled.\n\n"
                "Use 'View > Save Current Layout' to save your current "
                "arrangement.\n"
                "The saved layout will be restored on next startup."
            )
            self.logger.info("Custom layout restoration enabled")
        else:
            self.logger.info(
                "Custom layout restoration disabled - will use defaults on "
                "next startup"
            )

    def update_recent_files_menu(self, menu: 'QMenu') -> None:
        """
        Updates the Recent Files submenu with current recent files.

        Args:
            menu: The QMenu to populate with recent files.
        """
        menu.clear()
        recent_files = self.load_recent_files()

        if not recent_files:
            no_recent_action = QAction("No recent files", self.parent)
            no_recent_action.setEnabled(False)
            menu.addAction(no_recent_action)
            return

        for file_path in recent_files:
            if os.path.exists(file_path):
                action = QAction(os.path.basename(file_path), self.parent)
                action.setToolTip(file_path)
                action.triggered.connect(
                    lambda checked, path=file_path: self._open_recent_file(path)
                )
                menu.addAction(action)

        menu.addSeparator()
        clear_action = QAction("Clear Recent Files", self.parent)
        clear_action.triggered.connect(self.clear_recent_files)
        menu.addAction(clear_action)

    def _open_recent_file(self, file_path: str) -> None:
        """
        Opens a recent file.

        Args:
            file_path: The path to the file to open.
        """
        if not os.path.exists(file_path):
            QMessageBox.warning(
                self.parent,
                "File Not Found",
                f"The file '{file_path}' no longer exists and will be removed "
                f"from recent files."
            )
            recent_files = self.load_recent_files()
            if file_path in recent_files:
                recent_files.remove(file_path)
                self.save_recent_files(recent_files)
                self.recent_files_changed.emit()
            return

        self.open_file_requested.emit(file_path)
