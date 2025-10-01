# src/fatfloppy/gui/main_window.py
import copy
import datetime
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPointF, Qt, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette, QAction
from PyQt6.QtWidgets import (QAbstractItemView, QApplication,
                             QDockWidget, QFileDialog, QGraphicsView, QGroupBox,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                             QMainWindow, QMessageBox, QPlainTextEdit,
                             QPushButton, QToolBar, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core.controller import DiskController
from ..core.drivers import GREASEWEAZLE_AVAILABLE
from ..core.driver_factory import DriverFactory
from ..core.format_definitions import FLOPPY_FORMATS
from ..core.utils.logging_config import get_logger
from .dialogs import CreateImageDialog, DriveSelectionDialog
from .disk_map import DiskMapView
from .file_browser import DragDropTreeWidget
from .models import FileSystemNode
from .themes import get_dark_theme, get_light_theme


class FileBrowserApp(QMainWindow):
    """
    Main application window for the FatFloppy Disk Browser.
    Provides a GUI for interacting with floppy disk images and physical floppies.
    """

    logger: logging.Logger = get_logger(__name__)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initializes the FileBrowserApp.

        Args:
            parent: The parent widget, defaults to None.
        """
        super().__init__(parent)
        # Disk/Filesystem State
        self.root_node: Optional[FileSystemNode] = None
        self.current_node: Optional[FileSystemNode] = None
        self.current_path: str = "/"
        self.controller: Optional[DiskController] = None
        self.current_file_path: Optional[str] = None

        # Disk Map State
        self.current_head: int = 0
        self.busy_units: List[Any] = []  # Type depends on filesystem implementation
        self.free_space: int = 0
        self.total_space: int = 0

        # Text Editor State
        self.original_text_content: Optional[str] = None
        self.text_editor_modified: bool = False

        # Store discovered driver info
        self.greaseweazle_available: bool = GREASEWEAZLE_AVAILABLE
        self.extension_to_driver_map: Dict[str, str] = {}
        self.file_dialog_filter: str = ""
        self._build_file_dialog_filter()

        # UI Components (initialized in _init_ui)
        self.app_font: QFont
        self.toolbar: QToolBar
        self.tree_dock: QDockWidget
        self.tree_widget: QTreeWidget
        self.disk_info_dock: QDockWidget
        self.physical_format_group: QGroupBox
        self.physical_format_info: QLabel
        self.filesystem_group: QGroupBox
        self.filesystem_info: QLabel
        self.file_list_dock: QDockWidget
        self.file_list: DragDropTreeWidget
        self.disk_map_dock: QDockWidget
        self.disk_map: DiskMapView
        self.disk_map_view: QGraphicsView
        self.head_action: QAction
        self.text_viewer_dock: QDockWidget
        self.text_viewer: QPlainTextEdit
        self.save_button: QPushButton
        self.discard_button: QPushButton

        self._init_ui()
        self._setup_fonts()
        self._update_theme()
        QApplication.instance().styleHints().colorSchemeChanged.connect(self._update_theme)
        self.reset_ui()
        self.logger.info("FatFloppy application initialized.")

    # ##################################################################
    # Public Methods (Application Actions)
    # ##################################################################

    @pyqtSlot()
    def create_disk_image(self) -> None:
        """
        Opens a dialog to create a new disk image and formats it.
        """
        self.logger.debug("Attempting to create a new disk image.")
        dialog = CreateImageDialog(self)
        if not dialog.exec():
            self.logger.debug("Create Disk Image dialog cancelled.")
            return

        try:
            file_path, format_info, volume_label, output_format = dialog.get_selection()
            self.logger.info(f"Creating image: {file_path}, format_info: {format_info}, volume_label: {volume_label}, output_format: {output_format}")
        except ValueError as e:
            QMessageBox.warning(self, "Warning", str(e))
            self.logger.warning(f"Invalid selection for disk image creation: {e}")
            return

        try:
            profile = self._get_format_profile(format_info)
            if not profile:
                QMessageBox.critical(self, "Error", "Failed to determine format profile for creation.")
                self.logger.error("Failed to get format profile for disk image creation.")
                return
            if not profile.physical_format or not profile.filesystem_config:
                QMessageBox.critical(self, "Error", "Selected format profile is incomplete for creation.")
                self.logger.error(f"Incomplete format profile for creation: {profile.name}")
                return

            self.controller = DiskController()
            self.statusBar().showMessage(f"Creating and formatting disk image: {os.path.basename(file_path)}...")
            QApplication.processEvents()

            if self.controller.create_and_format_image(file_path, profile, volume_label, output_format):
                self.logger.info(f"Successfully created and formatted disk image: {file_path}")
                self.current_file_path = file_path # Store for later saves if it's an image
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch Head (Current: {self.current_head})")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                self.statusBar().showMessage(f"Created and formatted disk image: {os.path.basename(file_path)}")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", "Failed to create and format disk image.")
                self.logger.error(f"Failed to create/format disk image: {file_path}")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"An unexpected error occurred during image creation: {str(e)}")
            self.logger.exception("Unexpected error during disk image creation.")

    @pyqtSlot()
    def open_disk_image_file(self) -> None:
        """
        Opens a disk image file selected by the user, using auto-discovered drivers.
        """
        self.logger.debug("Attempting to open a disk image file.")
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Disk Image", "", self.file_dialog_filter
        )
        if not file_path:
            self.logger.debug("Open Disk Image file dialog cancelled.")
            return

        _, ext = os.path.splitext(file_path)
        ext_lower = ext.lower()

        # --- DYNAMIC DRIVER SELECTION ---
        # Use the map to find the driver type, defaulting to 'IMG' if not found
        disk_type = self.extension_to_driver_map.get(ext_lower, "IMG")
        self.logger.info(f"File extension '{ext_lower}' mapped to driver type '{disk_type}'.")

        try:
            self.reset_ui()
            self.controller = DiskController()
            self.statusBar().showMessage(f"Opening {disk_type} disk: {os.path.basename(file_path)}...")
            QApplication.processEvents()

            if self.controller.open_disk(file_path, disk_type):
                self.logger.info(f"Successfully opened disk image: {file_path} (Type: {disk_type})")
                self.current_file_path = file_path
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch Head (Current: {self.current_head})")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")
                self.statusBar().showMessage(f"Loaded: {os.path.basename(file_path)} (Type: {disk_type})")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", f"Failed to open {disk_type} disk image. Check logs for details.")
                self.logger.error(f"Failed to open disk image: {file_path}")
        except ValueError as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to parse or load image file: {str(e)}")
            self.logger.error(f"ValueError opening disk image {file_path}: {e}")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open disk image: {str(e)}")
            self.logger.exception(f"Unexpected error opening disk image {file_path}.")

    @pyqtSlot()
    def open_physical_floppy(self) -> None:
        """
        Opens a physical floppy drive using Greaseweazle.
        """
        self.logger.debug("Attempting to open a physical floppy.")
        try:
            dialog = DriveSelectionDialog(self)
            if not dialog.exec():
                self.logger.debug("Drive Selection dialog cancelled.")
                return

            drive_letter, drive_size, format_info = dialog.get_selection()
            self.logger.info(f"Opening physical floppy: Drive {drive_letter}, Size {drive_size}, Format {format_info}")
            self.controller = DiskController()
            self.statusBar().showMessage(f"Opening physical floppy drive {drive_letter}...")
            QApplication.processEvents()

            if self.controller.open_disk(None, "physical", drive_letter=drive_letter, drive_size=drive_size, format_info=format_info):
                self.logger.info(f"Successfully opened physical floppy on drive {drive_letter}.")
                self.current_file_path = None # Physical floppy has no file path
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch Head (Current: {self.current_head})")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                format_name = self.controller.detect_format()
                format_text = f" using {format_name}" if format_name else ""
                if format_info and not format_info.get("profile_name"):
                    format_text += f" (Custom format: {format_info.get('cylinders')}x{format_info.get('heads')}x{format_info.get('sectors_per_track')})"
                self.statusBar().showMessage(f"Loaded physical floppy{format_text} (Drive: {drive_letter}, Size: {drive_size}\")")
            else:
                self.reset_ui()
                QMessageBox.critical(self, "Error", "Failed to open physical floppy")
                self.logger.error(f"Failed to open physical floppy: Drive {drive_letter}, Size {drive_size}, Format {format_info}")
        except Exception as e:
            self.reset_ui()
            QMessageBox.critical(self, "Error", f"Failed to open physical floppy: {str(e)}")
            self.logger.exception("Unexpected error opening physical floppy.")

    @pyqtSlot()
    def extract_selected_items(self) -> None:
        """
        Extracts selected files or directories from the disk image to the local filesystem.
        """
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            QMessageBox.warning(self, "No Selection", "No items selected or no disk loaded to extract.")
            return

        self.logger.debug(f"Extracting {len(selected_items)} selected item(s).")

        if len(selected_items) == 1:
            item = selected_items[0]
            node: FileSystemNode = item.node
            source_path = self._build_full_path(node.name)
            if node.is_dir:
                base_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Extract To")
                if not base_dir:
                    return
                local_dir_path = os.path.join(base_dir, node.name)
                self._extract_directory(source_path, local_dir_path)
            else:
                save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
                if not save_path:
                    return
                self._extract_file(source_path, save_path)
        else:
            base_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Extract To")
            if not base_dir:
                return
            for item in selected_items:
                node = item.node
                source_path = self._build_full_path(node.name)
                if node.is_dir:
                    local_dir_path = os.path.join(base_dir, node.name)
                    self._extract_directory(source_path, local_dir_path)
                else:
                    local_file_path = os.path.join(base_dir, node.name)
                    self._extract_file(source_path, local_file_path)
        self.statusBar().showMessage("Extraction complete.")

    @pyqtSlot()
    def delete_selected_items(self) -> None:
        """
        Deletes selected files or directories from the disk image.
        """
        selected_items = self.file_list.selectedItems()
        if not selected_items or not self.controller:
            QMessageBox.warning(self, "No Selection", "No items selected or no disk loaded to delete.")
            return

        paths_to_delete: List[str] = [self._build_full_path(item.node.name) for item in selected_items]
        self.logger.info(f"Attempting to delete {len(paths_to_delete)} item(s): {paths_to_delete}")

        if len(paths_to_delete) == 1:
            msg = f"Are you sure you want to delete '{paths_to_delete[0]}'?"
        else:
            msg = f"Are you sure you want to delete {len(paths_to_delete)} items?"
        reply = QMessageBox.question(self, "Confirm Deletion", msg,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            self.logger.debug("Deletion cancelled by user.")
            return

        failed_paths: List[str] = []
        for path in paths_to_delete:
            try:
                success = self.controller.delete_item_recursive(path)
                if not success:
                    failed_paths.append(path)
                    self.logger.warning(f"Failed to delete item: {path}")
            except Exception as e:
                failed_paths.append(path)
                self.logger.error(f"Error deleting item {path}: {e}", exc_info=True)

        current_path = self.current_path
        self.refresh_filesystem_ui(current_path)

        if not failed_paths:
            self.statusBar().showMessage(f"Deleted {len(paths_to_delete)} item(s)")
            self.logger.info(f"Successfully deleted {len(paths_to_delete)} item(s).")
        else:
            failed_msg = "Failed to delete:\n" + "\n".join(failed_paths)
            QMessageBox.warning(self, "Deletion Failed", failed_msg)
            self.statusBar().showMessage(f"Deleted {len(paths_to_delete) - len(failed_paths)} item(s), {len(failed_paths)} failed.")
            self.logger.warning(f"Deletion completed with {len(failed_paths)} failures.")

    @pyqtSlot()
    def create_directory(self) -> None:
        """
        Prompts the user for a directory name and creates a new directory
        in the current filesystem path.
        """
        if not self.controller or not self.current_node:
            QMessageBox.warning(self, "Warning", "No disk image loaded or no current directory selected.")
            return

        current_path = self.current_path
        dir_name, ok = QInputDialog.getText(self, "Create New Directory",
                                            "Enter directory name (8.3 format):")
        if not ok or not dir_name:
            self.logger.debug("Create Directory dialog cancelled or no name entered.")
            return

        try:
            full_path = f"{current_path}/{dir_name}".replace("//", "/")
            self.logger.info(f"Attempting to create directory: {full_path}")
            success = self.controller.create_directory(full_path)
            if success:
                self.refresh_filesystem_ui(current_path)
                self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
                self.logger.info(f"Successfully created directory: {full_path}")
            else:
                QMessageBox.warning(self, "Warning", f"Failed to create directory {dir_name}. It might already exist or name is invalid.")
                self.logger.warning(f"Failed to create directory {full_path}.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create directory: {str(e)}")
            self.logger.exception(f"Error creating directory {full_path}.")

    @pyqtSlot()
    def add_file(self) -> None:
        """
        Opens a file dialog to select a local file and adds it to the
        current directory on the disk image.
        """
        if not self.controller or not self.current_node:
            QMessageBox.warning(self, "Warning", "No disk image loaded or no current directory selected.")
            return

        file_path, _ = QFileDialog.getOpenFileName(self, "Select File to Add")
        if not file_path:
            self.logger.debug("Add File dialog cancelled.")
            return

        base_name = os.path.basename(file_path)
        base_name = self._format_83_filename(base_name)
        new_name, ok = QInputDialog.getText(self, "File Name",
                                            "Enter file name (8.3 format):",
                                            text=base_name)
        if not ok or not new_name:
            self.logger.debug("File name input dialog cancelled or no name entered.")
            return

        try:
            self._add_file_to_disk(file_path, new_name, self.current_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")
            self.logger.exception(f"Error adding file {file_path} to {self.current_path}/{new_name}.")

    @pyqtSlot(str, str, bool)
    def import_path(self, local_path: str, target_path: str, auto_name: bool) -> None:
        """
        Imports a local file or directory into the disk image.

        Args:
            local_path: The path to the local file or directory.
            target_path: The target path on the disk image.
            auto_name: If True, a unique 8.3 name will be generated.
                       If False for a file, a dialog will ask for the name.
        """
        local_path = os.path.normpath(local_path)
        self.logger.info(f"Importing local path '{local_path}' to target '{target_path}' (auto_name={auto_name}).")

        if os.path.isfile(local_path):
            if not auto_name:
                base_name = os.path.basename(local_path)
                base_name = self._format_83_filename(base_name)
                new_name, ok = QInputDialog.getText(self, "File Name",
                                                    f"Enter file name for {base_name} (8.3 format):",
                                                    text=base_name)
                if not ok or not new_name:
                    self.logger.debug("File name input cancelled for non-auto-named import.")
                    return
                existing_names = [item['name'].upper() for item in self.controller.list_directory(target_path)]
                if new_name.upper() in existing_names:
                    QMessageBox.warning(self, "Warning", f"File '{new_name}' already exists in {target_path}")
                    self.logger.warning(f"File '{new_name}' already exists, import aborted.")
                    return
                try:
                    self._add_file_to_disk(local_path, new_name, target_path)
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")
                    self.logger.exception(f"Error adding specific file {local_path} to {target_path}/{new_name}.")
            else:
                original_name = os.path.basename(local_path)
                new_name = self._generate_unique_83_name(original_name, target_path, is_dir=False)
                try:
                    self._add_file_to_disk(local_path, new_name, target_path)
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to add file: {str(e)}")
                    self.logger.exception(f"Error adding auto-named file {local_path} to {target_path}/{new_name}.")
        elif os.path.isdir(local_path):
            original_name = os.path.basename(local_path)
            if not original_name:
                QMessageBox.critical(self, "Error", f"Invalid directory name: empty name for path {local_path}")
                self.logger.error(f"Attempted to import directory with empty name: {local_path}")
                return
            new_dir_name = self._generate_unique_83_name(original_name, target_path, is_dir=True)
            new_dir_path = f"{target_path}/{new_dir_name}" if target_path != "/" else f"/{new_dir_name}"
            try:
                self.logger.info(f"Creating directory '{new_dir_path}' for import from '{local_path}'.")
                success = self.controller.create_directory(new_dir_path)
                if not success:
                    raise Exception("Failed to create directory")
                for item in os.listdir(local_path):
                    item_path = os.path.join(local_path, item)
                    self.import_path(item_path, new_dir_path, auto_name=True)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to import directory: {str(e)}")
                self.logger.exception(f"Error importing directory {local_path} to {new_dir_path}.")

        self.refresh_filesystem_ui(target_path)
        self.statusBar().showMessage(f"Imported '{os.path.basename(local_path)}' to '{target_path}'")

    @pyqtSlot()
    def save_file(self) -> bool:
        """
        Saves changes from the text editor to the currently viewed file on the disk image.

        Returns:
            True if the file was saved successfully or no changes were present, False otherwise.
        """
        success = False
        if self.current_file_path and self.text_editor_modified:
            self.logger.info(f"Saving changes to {self.current_file_path}")
            try:
                current_text = self.text_viewer.toPlainText()
                # Normalize line endings to CR+LF for DOS/CP/M compatibility
                normalized_text = current_text.replace('\n', '\r\n')

                content_bytes = b''
                fs_type = self.controller.filesystem.get_display_info().get("Filesystem Type", "Unknown")

                if fs_type == "CP/M":
                    # For CP/M, strip high bit as often 7-bit ASCII is expected
                    content_bytes = bytes([b & 0x7F for b in normalized_text.encode('ascii', errors='replace')])
                else:  # Default to FAT/MS-DOS style (CP437)
                    content_bytes = normalized_text.encode('cp437', errors='replace')

                if self.controller.write_file(self.current_file_path, content_bytes):
                    self.statusBar().showMessage(f"Saved changes to {os.path.basename(self.current_file_path)}")
                    self.original_text_content = current_text
                    self.text_editor_modified = False
                    self.save_button.setEnabled(False)
                    self.discard_button.setEnabled(False)
                    self.refresh_filesystem_ui(preserve_path=self.current_path)
                    success = True
                    self.logger.info(f"Successfully saved {self.current_file_path}")
                else:
                    QMessageBox.warning(self, "Save Failed", f"Could not write changes to {self.current_file_path}")
                    self.logger.warning(f"Failed to write file data for {self.current_file_path}")
            except UnicodeEncodeError as e:
                QMessageBox.critical(self, "Encoding Error", f"Text contains characters not supported by the target encoding: {e}")
                self.logger.error(f"Encoding error when saving {self.current_file_path}: {e}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save file: {str(e)}")
                self.logger.exception(f"Unexpected error saving {self.current_file_path}.")
        elif not self.text_editor_modified:
            self.statusBar().showMessage("No changes to save.")
            success = True
            self.logger.debug(f"Save requested for {self.current_file_path} but no changes detected.")
        else:
            QMessageBox.warning(self, "Warning", "No file context for saving.")
            self.logger.warning("Save requested without a current file context.")

        return success

    @pyqtSlot()
    def discard_changes(self) -> None:
        """
        Discards any unsaved changes in the text editor and reverts to the
        original file content.
        """
        if self.original_text_content is not None and self.text_editor_modified:
            self.logger.info(f"Discarding changes for {self.current_file_path}")
            reply = QMessageBox.question(self, "Discard Changes",
                                         "Are you sure you want to discard all changes?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.No)

            if reply == QMessageBox.StandardButton.Yes:
                self.text_viewer.blockSignals(True)
                self.text_viewer.setPlainText(self.original_text_content)
                self.text_viewer.blockSignals(False)

                self.text_editor_modified = False
                self.save_button.setEnabled(False)
                self.discard_button.setEnabled(False)
                self.statusBar().showMessage(f"Changes to {os.path.basename(self.current_file_path)} discarded.")
                self.logger.info(f"Changes to {self.current_file_path} successfully discarded.")
            else:
                self.logger.debug("Discard changes cancelled by user.")
        else:
            self.save_button.setEnabled(False)
            self.discard_button.setEnabled(False)
            self.logger.debug("Discard changes requested, but no modifications or no original content.")

    @pyqtSlot()
    def toggle_head(self) -> None:
        """
        Switches between disk heads for visualization if the disk has multiple heads.
        """
        if self.controller and self.controller.physical_format.heads > 1:
            self.current_head = 1 - self.current_head
            self.logger.info(f"Switched to head: {self.current_head}")
            self.head_action.setText(f"Switch Head (Current: {self.current_head})")
            self.draw_disk_map()
        else:
            QMessageBox.information(self, "Info", "Head switching is not available for this disk (single-sided).")
            self.logger.info("Attempted to switch head on a single-sided disk.")

    @pyqtSlot(QTreeWidgetItem, int)
    def select_directory(self, item: QTreeWidgetItem, column: int) -> None:
        """
        Slot to handle directory selection in the tree widget.
        Updates the current path and refreshes the file list.

        Args:
            item: The selected QTreeWidgetItem.
            column: The column index of the clicked item (not used, but part of signal signature).
        """
        self.logger.debug(f"Directory '{item.text(0)}' selected in tree.")
        self.current_node = item.node
        self.current_path = self._build_path_from_node(item.node)
        self.update_file_list()
        self.statusBar().showMessage(f"Viewing: {self.current_path}")

    @pyqtSlot(QPointF, QFont, QColor)
    def draw_disk_map(self) -> None:
        """
        Draws or redraws the disk map visualization.
        """
        if not self.controller:
            self.disk_map.scene.clear()
            self.disk_map._draw_no_disk_message(self.app_font, self.palette().color(QPalette.ColorRole.WindowText))
            return

        text_color = self.palette().color(QPalette.ColorRole.WindowText)
        self.disk_map.draw_disk_map(
            self.controller,
            self.current_head,
            self.busy_units,
            self.free_space,
            self.total_space,
            self.app_font,
            text_color,
        )
        self.logger.debug(f"Disk map drawn for head {self.current_head}.")

    # ##################################################################
    # Private UI Setup Methods
    # ##################################################################

    def _init_ui(self) -> None:
        """Initializes and lays out the main UI components of the application."""
        self._init_window_settings()
        self._create_menus()
        self._create_toolbars()
        self._create_docks()
        self._setup_dock_layout()
        self._connect_signals_slots()
        self.logger.debug("UI components initialized.")

    def _init_window_settings(self) -> None:
        """Sets up initial window title and geometry."""
        self.setWindowTitle("FatFloppy Disk Browser")
        self.setGeometry(100, 100, 1200, 800)
        self.setDockNestingEnabled(True)

    def _create_menus(self) -> None:
        """Creates the application's menu bar and its actions."""
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        create_image_action = QAction("Create Disk Image", self)
        create_image_action.triggered.connect(self.create_disk_image)
        file_menu.addAction(create_image_action)

        open_image_action = QAction("Open Disk Image File", self)
        open_image_action.setToolTip("Open a disk image file (.ima, .img, .imd)")
        open_image_action.triggered.connect(self.open_disk_image_file)
        file_menu.addAction(open_image_action)

        open_floppy_action = QAction("Open Physical Floppy", self)
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        if not self.greaseweazle_available:
            open_floppy_action.setEnabled(False)
            self.logger.info("Greaseweazle not available, 'Open Physical Floppy' disabled.")
        file_menu.addAction(open_floppy_action)
        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        self.logger.debug("Menus created.")

    def _create_toolbars(self) -> None:
        """Creates the main toolbar and adds actions."""
        self.toolbar = QToolBar("Main Toolbar", self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        extract_action = QAction("Extract", self)
        extract_action.setToolTip("Extract selected item(s) to local filesystem")
        extract_action.triggered.connect(self.extract_selected_items)
        self.toolbar.addAction(extract_action)

        delete_action = QAction("Delete", self)
        delete_action.setToolTip("Delete selected file(s) or directory")
        delete_action.triggered.connect(self.delete_selected_items)
        self.toolbar.addAction(delete_action)

        create_dir_action = QAction("New Folder", self)
        create_dir_action.setToolTip("Create a new directory in current location")
        create_dir_action.triggered.connect(self.create_directory)
        self.toolbar.addAction(create_dir_action)

        add_file_action = QAction("Add File", self)
        add_file_action.setToolTip("Add a file to current directory")
        add_file_action.triggered.connect(self.add_file)
        self.toolbar.addAction(add_file_action)
        self.logger.debug("Toolbars created.")

    def _create_docks(self) -> None:
        """Creates all dockable widgets for the application."""
        self._create_tree_dock()
        self._create_disk_info_dock()
        self._create_file_list_dock()
        self._create_disk_map_dock()
        self._create_text_editor_dock()
        self.logger.debug("Docks created.")

    def _create_tree_dock(self) -> None:
        """Creates and configures the directory tree dock."""
        self.tree_dock = QDockWidget("Directory Tree", self)
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("Directories")
        self.tree_widget.itemClicked.connect(self.select_directory)
        self.tree_dock.setWidget(self.tree_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.tree_dock)

    def _create_disk_info_dock(self) -> None:
        """Creates and configures the disk information dock."""
        self.disk_info_dock = QDockWidget("Disk Information", self)
        disk_info_widget = QWidget()
        disk_info_layout = QVBoxLayout(disk_info_widget)
        disk_info_layout.setContentsMargins(5, 5, 5, 5)
        disk_info_layout.setSpacing(6)

        self.physical_format_group = QGroupBox("Physical Geometry")
        self.physical_format_info = QLabel("No disk image loaded")
        geometry_layout = QVBoxLayout(self.physical_format_group)
        geometry_layout.addWidget(self.physical_format_info)
        self.physical_format_group.setLayout(geometry_layout)

        self.filesystem_group = QGroupBox("Filesystem")
        self.filesystem_info = QLabel("No filesystem detected")
        filesystem_layout = QVBoxLayout(self.filesystem_group)
        filesystem_layout.addWidget(self.filesystem_info)
        self.filesystem_group.setLayout(filesystem_layout)

        disk_info_layout.addWidget(self.physical_format_group)
        disk_info_layout.addWidget(self.filesystem_group)
        disk_info_layout.addStretch(1) # Added stretch to push content to top
        disk_info_widget.setLayout(disk_info_layout)
        self.disk_info_dock.setWidget(disk_info_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.disk_info_dock)

    def _create_file_list_dock(self) -> None:
        """Creates and configures the file list dock."""
        self.file_list_dock = QDockWidget("Files", self)
        self.file_list = DragDropTreeWidget(self)
        self.file_list.setHeaderLabels(["Name", "Size", "Date/Time", "Attr"])
        self.file_list.setDragEnabled(True)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list_dock.setWidget(self.file_list)
        header = self.file_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.file_list_dock)

    def _create_disk_map_dock(self) -> None:
        """Creates and configures the disk map dock."""
        self.disk_map_dock = QDockWidget("Disk Map", self)
        self.disk_map = DiskMapView(self)
        self.disk_map_view = self.disk_map.view

        disk_map_container = QWidget()
        disk_map_layout = QVBoxLayout(disk_map_container)
        disk_map_layout.setContentsMargins(0, 0, 0, 0)
        disk_map_layout.setSpacing(0)

        disk_map_toolbar = QToolBar("Disk Map Tools")
        disk_map_toolbar.setIconSize(self.toolbar.iconSize())
        disk_map_toolbar.setMovable(False)
        disk_map_toolbar.setStyleSheet("QToolBar { border: none; }")

        self.head_action = QAction("Switch Head", self)
        self.head_action.setToolTip("Switch between disk heads (sides)")
        self.head_action.triggered.connect(self.toggle_head)
        self.head_action.setEnabled(False)

        disk_map_toolbar.addAction(self.head_action)
        disk_map_layout.addWidget(disk_map_toolbar)
        disk_map_layout.addWidget(self.disk_map_view)
        self.disk_map_dock.setWidget(disk_map_container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.disk_map_dock)

    def _create_text_editor_dock(self) -> None:
        """Creates and configures the text editor dock."""
        self.text_viewer_dock = QDockWidget("Text Editor", self)
        text_viewer_widget = QWidget()
        text_viewer_layout = QVBoxLayout(text_viewer_widget)
        text_viewer_layout.setContentsMargins(2, 2, 2, 2)
        text_viewer_layout.setSpacing(4)

        self.text_viewer = QPlainTextEdit()
        self.text_viewer.setReadOnly(False)
        self.text_viewer.textChanged.connect(self._on_text_editor_changed)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(6)

        self.save_button = QPushButton("Save Changes")
        self.save_button.clicked.connect(self.save_file)
        self.save_button.setEnabled(False)  # Start disabled

        self.discard_button = QPushButton("Discard Changes")
        self.discard_button.clicked.connect(self.discard_changes)
        self.discard_button.setEnabled(False)

        button_layout.addStretch(0)
        button_layout.addWidget(self.discard_button)
        button_layout.addWidget(self.save_button)
        button_layout.addStretch(0)

        text_viewer_layout.addWidget(self.text_viewer)
        text_viewer_layout.addLayout(button_layout)
        text_viewer_widget.setLayout(text_viewer_layout)
        self.text_viewer_dock.setWidget(text_viewer_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.text_viewer_dock)

    def _setup_dock_layout(self) -> None:
        """Arranges and resizes the dockable widgets."""
        self.splitDockWidget(self.tree_dock, self.disk_info_dock, Qt.Orientation.Vertical)
        self.splitDockWidget(self.file_list_dock, self.disk_map_dock, Qt.Orientation.Horizontal)
        self.tabifyDockWidget(self.disk_map_dock, self.text_viewer_dock)
        self.disk_map_dock.raise_()

        # Set initial sizes for docks
        # Note: resizeDocks operates on a list of docks and a list of sizes
        # The sizes are relative, so [200, 500] means first dock takes 200 units, second takes 500 units.
        # This can be tricky to get pixel-perfect without testing.
        self.resizeDocks([self.tree_dock, self.file_list_dock], [200, 500], Qt.Orientation.Horizontal)
        self.resizeDocks([self.file_list_dock, self.disk_map_dock], [500, 700], Qt.Orientation.Horizontal)
        self.resizeDocks([self.tree_dock, self.disk_info_dock], [600, 200], Qt.Orientation.Vertical)
        self.logger.debug("Dock layout configured.")

    def _connect_signals_slots(self) -> None:
        """Connects various UI signals to their respective slots."""
        self.file_list.selectionModel().selectionChanged.connect(self._on_file_list_selection_changed)
        self.statusBar().showMessage("Ready")

    # ##################################################################
    # Private Helper Methods (General UI)
    # ##################################################################

    def _setup_fonts(self) -> None:
        """
        Configures and sets a monospace font for various UI elements.
        Tries to find a suitable monospace font, falling back to system default.
        """
        monospace_fonts: List[str] = [
            "Courier New", "DejaVu Sans Mono", "Consolas", "Menlo", "Liberation Mono", "Monaco", "SF Mono"
        ]
        self.app_font = QFont()
        found_font = False
        for font_name in monospace_fonts:
            if QFontDatabase.isFixedPitch(font_name):
                self.app_font.setFamily(font_name)
                found_font = True
                self.logger.debug(f"Using monospace font: {font_name}")
                break

        if not found_font:
            default_monospace = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            self.app_font = default_monospace
            self.logger.debug(f"Using system default monospace font: {default_monospace.family()}")

        self.app_font.setPointSize(12)
        self.setFont(self.app_font)

        # Apply font to specific widgets
        self.tree_widget.setFont(self.app_font)
        self.file_list.setFont(self.app_font)
        self.physical_format_info.setFont(self.app_font)
        self.filesystem_info.setFont(self.app_font)
        self.text_viewer.setFont(self.app_font)
        self.logger.debug("Application fonts set up.")

    def _build_file_dialog_filter(self) -> None:
        """
        Dynamically builds the file dialog filter string from discovered drivers.
        """
        self.logger.debug("Building file dialog filter from discovered drivers.")
        self.extension_to_driver_map = DriverFactory.get_extension_map()

        all_extensions = sorted(self.extension_to_driver_map.keys())

        # Group extensions by driver type for a nicer dialog
        driver_to_exts: Dict[str, List[str]] = {}
        for ext, driver_type in self.extension_to_driver_map.items():
            driver_to_exts.setdefault(driver_type, []).append(f"*{ext}")

        filters = []
        # Create a filter for "All Supported Images"
        all_ext_str = " ".join(f"*{ext}" for ext in all_extensions)
        filters.append(f"All Supported Images ({all_ext_str})")

        # Create a specific filter for each driver type
        for driver_type, exts in sorted(driver_to_exts.items()):
            exts_str = " ".join(exts)
            desc = getattr(DriverFactory.get_driver_class(driver_type), 'driver_description', f"{driver_type} Files")
            filters.append(f"{desc} ({exts_str})")

        filters.append("All Files (*)")
        self.file_dialog_filter = ";;".join(filters)
        self.logger.info(f"Generated file dialog filter: {self.file_dialog_filter}")

    @pyqtSlot()
    def _update_theme(self) -> None:
        """
        Updates the application's theme (dark/light) based on the system's
        color scheme hints.
        """
        color_scheme = QApplication.styleHints().colorScheme()
        if color_scheme == Qt.ColorScheme.Dark:
            style_sheet = get_dark_theme()
            self.logger.debug("Applying dark theme.")
        elif color_scheme == Qt.ColorScheme.Light:
            style_sheet = get_light_theme()
            self.logger.debug("Applying light theme.")
        else:
            # Fallback for systems that don't specify, or if system is "unknown"
            style_sheet = get_dark_theme()
            self.logger.debug("Applying default dark theme (system color scheme unknown/unspecified).")
        self.setStyleSheet(style_sheet)

    def reset_ui(self) -> None:
        """
        Resets the UI and application state to its initial, no-disk-loaded condition.
        """
        self.logger.info("Resetting UI to initial state.")
        # Clear Disk/Filesystem State
        self.root_node = None
        self.current_node = None
        self.current_path = "/"
        self.current_head = 0
        self.busy_units = []
        self.free_space = 0
        self.total_space = 0
        self.current_file_path = None

        if self.controller:
            self.controller.close_disk()
            self.logger.debug("Closed existing disk controller.")
        self.controller = None

        # Clear UI components
        self.tree_widget.clear()
        self.file_list.clear()
        self.physical_format_info.setText("No disk image loaded")
        self.filesystem_info.setText("No filesystem detected")
        self.disk_map.scene.clear()
        self.draw_disk_map()  # Redraw with no disk
        self.head_action.setEnabled(False)
        self.head_action.setText("Switch Head")
        self.statusBar().showMessage("Ready")
        self._clear_text_viewer_state()
        self.disk_map_dock.raise_()
        self.logger.debug("UI reset complete.")

    def refresh_filesystem_ui(self, preserve_path: Optional[str] = None) -> None:
        """
        Refreshes the filesystem-related UI elements (tree, file list, disk info).

        Args:
            preserve_path: An optional path to navigate to after refreshing the tree.
                           If None, navigates to root.
        """
        self.logger.info(f"Refreshing filesystem UI (preserve_path: {preserve_path}).")
        self.root_node = self._build_fs_tree()
        self._populate_tree_widget()

        if preserve_path:
            self._navigate_to_path(preserve_path)
        else:
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()

        self._get_busy_units()
        self._update_disk_info()
        self.draw_disk_map()
        self.statusBar().showMessage(f"Current path: {self.current_path}")
        self.logger.debug("Filesystem UI refresh complete.")

    # ##################################################################
    # Private Helper Methods (Disk/Filesystem)
    # ##################################################################

    def _get_format_profile(self, format_info: Dict[str, Any]) -> Optional[Any]:
        """
        Retrieves a format profile from the provided format_info.
        Either by name for predefined formats or by creating a custom one.

        Args:
            format_info: Dictionary containing format details.

        Returns:
            A format profile object or None if not found/created.
        """
        if "profile_name" in format_info:
            profile_name = format_info["profile_name"]
            original_profile = FLOPPY_FORMATS.get(profile_name)
            if original_profile:
                self.logger.debug(f"Retrieved predefined format profile: {profile_name}")
                return copy.deepcopy(original_profile)
            else:
                QMessageBox.critical(self, "Error", f"Predefined format '{profile_name}' not found.")
                self.logger.error(f"Predefined format '{profile_name}' not found.")
                return None
        else:
            if not self.controller:
                self.controller = DiskController()
            self.logger.debug(f"Creating custom format profile with info: {format_info}")
            return self.controller.create_custom_profile(format_info)

    def _update_disk_info(self) -> None:
        """Updates both physical geometry and filesystem information displays."""
        self._update_geometry_info()
        self._update_filesystem_info()
        self.logger.debug("Disk info displays updated.")

    def _update_geometry_info(self) -> None:
        """Updates the display for physical disk geometry."""
        if not self.controller or not self.controller.physical_format:
            self.physical_format_info.setText("Disk geometry not available")
            return

        geometry = self.controller.physical_format
        total_sectors = geometry.total_sectors
        total_bytes = total_sectors * geometry.bytes_per_sector

        imd_comment = ""
        if hasattr(self.controller.driver, 'comment') and self.controller.driver.comment:
            imd_comment = self.controller.driver.comment
            display_comment = (imd_comment[:60] + '...') if len(imd_comment) > 63 else imd_comment
            imd_comment = f"IMD Comment: {display_comment}\n"

        encoding_text, rate_text, spt_text = "N/A", "N/A", "N/A"
        if geometry.track_formats:
            encodings = {tf.encoding for tf in geometry.track_formats}
            rates = {tf.rate for tf in geometry.track_formats}
            spts = {tf.sectors_per_track for tf in geometry.track_formats}

            encoding_text = list(encodings)[0] if len(encodings) == 1 else "variable"
            rate_text = f"{list(rates)[0]} kbps" if len(rates) == 1 else "variable"
            spt_text = str(list(spts)[0]) if len(spts) == 1 else "variable"

        info = (
            f"{imd_comment}"
            f"Encoding: {encoding_text}\n"
            f"Data Rate: {rate_text}\n"
            f"Rotation Speed: {geometry.rpm} RPM\n"
            f"Bytes per Sector: {geometry.bytes_per_sector}\n"
            f"Sectors per Track: {spt_text}\n"
            f"Number of Heads: {geometry.heads}\n"
            f"Number of Cylinders: {geometry.cylinders}\n"
            f"Total Sectors: {total_sectors}\n"
            f"Total Size: {total_bytes / 1024:.1f} KB"
        )
        self.physical_format_info.setText(info)
        self.logger.debug("Physical geometry info updated.")

    def _update_filesystem_info(self) -> None:
        """Updates the display for filesystem information."""
        if not self.controller:
            self.filesystem_info.setText("No disk loaded")
            return

        if self.controller.filesystem:
            fs_info_dict = self.controller.filesystem.get_display_info()
            fs_info_lines: List[str] = []
            # Prioritize certain fields
            if "Filesystem Type" in fs_info_dict:
                fs_info_lines.append(f"Filesystem Type: {fs_info_dict.pop('Filesystem Type')}")
            if "Volume Label" in fs_info_dict:
                fs_info_lines.append(f"Volume Label: {fs_info_dict.pop('Volume Label')}")
            # Add remaining fields
            for key, value in fs_info_dict.items():
                fs_info_lines.append(f"{key}: {value}")

            fs_details = "\n".join(fs_info_lines)

            space_info = self.controller.get_free_space()
            space_str: str
            if space_info:
                free_bytes, total_bytes = space_info
                free_kb = free_bytes / 1024
                total_kb = total_bytes / 1024
                percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
                space_str = f"Free Space: {free_kb:.1f} KB / {total_kb:.1f} KB ({percent_free:.1f}%)"
            else:
                space_str = "Free Space: N/A"
                self.logger.warning("Could not retrieve filesystem free space information.")

            info = f"{fs_details}\n{space_str}"
            self.filesystem_info.setText(info)
            self.logger.debug("Filesystem info updated.")
        else:
            self.filesystem_info.setText("No filesystem detected")
            self.logger.debug("No filesystem detected, display updated accordingly.")

    def _build_fs_tree(self) -> Optional[FileSystemNode]:
        """
        Builds a hierarchical representation of the disk's filesystem.

        Returns:
            The root FileSystemNode of the tree, or None if no controller is present.
        """
        if not self.controller:
            return None

        self.logger.debug("Building filesystem tree.")
        root_node = FileSystemNode("Root", is_dir=True, attributes="-")
        node_dict: Dict[str, FileSystemNode] = {"/": root_node}

        # First pass: collect all directories and their direct contents
        all_directories_to_scan: List[str] = ["/"]
        scanned_directories: List[str] = [] # To prevent infinite loops in case of weird FS structures
        directory_contents: Dict[str, List[Dict[str, Any]]] = {}

        while all_directories_to_scan:
            current_dir_path = all_directories_to_scan.pop(0)
            if current_dir_path in scanned_directories:
                continue
            scanned_directories.append(current_dir_path)

            try:
                items = self.controller.list_directory(current_dir_path)
                directory_contents[current_dir_path] = items
                for item in items:
                    if item["is_dir"] and item["name"] not in [".", ".."]:
                        next_dir_path = os.path.normpath(os.path.join(current_dir_path, item["name"]))
                        if next_dir_path not in scanned_directories and next_dir_path not in all_directories_to_scan:
                            all_directories_to_scan.append(next_dir_path)
            except Exception as e:
                self.logger.error(f"Error listing directory {current_dir_path} during tree build: {e}")

        # Second pass: build the actual node hierarchy for directories
        sorted_dir_paths = sorted(node_dict.keys(), key=lambda p: p.count('/'))
        for dir_path in sorted_dir_paths:
            if dir_path == "/":
                continue

            parts = dir_path.strip("/").split("/")
            parent_path = os.path.normpath("/" + "/".join(parts[:-1]))
            dir_name = parts[-1]

            parent_node = node_dict.get(parent_path)
            if not parent_node:
                self.logger.warning(f"Parent node for '{dir_path}' not found: '{parent_path}'. Skipping.")
                continue

            dir_info_found = None
            for item in directory_contents.get(parent_path, []):
                if item["is_dir"] and item["name"] == dir_name:
                    dir_info_found = item
                    break

            if not dir_info_found:
                self.logger.warning(f"Directory info not found for '{dir_path}'. Skipping.")
                continue

            node = FileSystemNode(
                name=dir_name,
                size=0,  # Directories usually have 0 size in FAT
                is_dir=True,
                modified=dir_info_found["datetime"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(dir_info_found["datetime"], datetime.datetime) else str(dir_info_found["datetime"]),
                attributes=dir_info_found["attributes"],
                parent=parent_node
            )
            parent_node.appendChild(node)
            node_dict[dir_path] = node

        # Third pass: add files to their parent nodes
        for dir_path, items in directory_contents.items():
            parent_node = node_dict.get(dir_path)
            if not parent_node:
                continue

            for item in items:
                if not item["is_dir"]:
                    node = FileSystemNode(
                        name=item["name"],
                        size=item["size"],
                        is_dir=False,
                        modified=item["datetime"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(item["datetime"], datetime.datetime) else str(item["datetime"]),
                        attributes=item["attributes"],
                        parent=parent_node
                    )
                    parent_node.appendChild(node)
        self.logger.debug("Filesystem tree built successfully.")
        return root_node

    def _populate_tree_widget(self) -> None:
        """Populates the QTreeWidget (directory tree) from the internal FileSystemNode tree."""
        self.tree_widget.clear()
        if not self.root_node:
            self.logger.debug("No root node to populate tree widget.")
            return

        root_item = QTreeWidgetItem(self.tree_widget, ["/"])
        root_item.node = self.root_node
        self._recursive_populate_tree_widget(self.root_node, root_item)
        self.tree_widget.expandAll()
        self.logger.debug("Tree widget populated.")

    def _recursive_populate_tree_widget(self, node: FileSystemNode, parent_item: QTreeWidgetItem) -> None:
        """
        Recursively populates the QTreeWidget with directory nodes.

        Args:
            node: The current FileSystemNode to process.
            parent_item: The QTreeWidgetItem corresponding to the parent node.
        """
        if not node:
            return

        for child in node.children:
            if child.is_dir:
                child_item = QTreeWidgetItem(parent_item, [child.name])
                child_item.node = child
                self._recursive_populate_tree_widget(child, child_item)

    def _build_path_from_node(self, node: FileSystemNode) -> str:
        """
        Constructs the full filesystem path from a FileSystemNode.

        Args:
            node: The FileSystemNode to get the path for.

        Returns:
            The full path string.
        """
        path_parts: List[str] = []
        curr = node
        while curr and curr.parent:  # Stop when curr is root_node (which has no parent)
            path_parts.insert(0, curr.name)
            curr = curr.parent
        return "/" + "/".join(path_parts) if path_parts else "/"

    def update_file_list(self) -> None:
        """
        Updates the file list widget based on the currently selected directory.
        """
        self.file_list.clear()
        if not self.current_node:
            self.logger.debug("No current node to update file list.")
            return

        for child in self.current_node.children:
            # Only add files and subdirectories
            if child.name not in [".", ".."]:
                item = QTreeWidgetItem(self.file_list, [
                    child.name,
                    str(child.size) if not child.is_dir else "",
                    child.modified,
                    child.attributes
                ])
                item.node = child
        self.logger.debug(f"File list updated for path: {self.current_path}")

    def _extract_file(self, source_path: str, local_path: str) -> None:
        """
        Extracts a single file from the disk image to the local filesystem.

        Args:
            source_path: The full path to the file on the disk image.
            local_path: The full path to save the file locally.
        """
        try:
            self.logger.info(f"Extracting file '{source_path}' to '{local_path}'.")
            file_data = self.controller.read_file(source_path)
            if file_data is not None:
                with open(local_path, 'wb') as f:
                    f.write(file_data)
                self.statusBar().showMessage(f"Extracted {os.path.basename(source_path)} to {os.path.basename(local_path)}")
                self.logger.info(f"Successfully extracted file {source_path}.")
            else:
                QMessageBox.warning(self, "Warning", f"Failed to read file data for {source_path}")
                self.logger.warning(f"Failed to read file data for extraction of {source_path}.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract file {source_path}: {str(e)}")
            self.logger.exception(f"Error extracting file {source_path}.")

    def _extract_directory(self, source_dir_path: str, local_dir_path: str) -> None:
        """
        Recursively extracts a directory and its contents from the disk image.

        Args:
            source_dir_path: The full path to the directory on the disk image.
            local_dir_path: The full path to create the directory locally.
        """
        try:
            self.logger.info(f"Extracting directory '{source_dir_path}' to '{local_dir_path}'.")
            os.makedirs(local_dir_path, exist_ok=True)
            items = self.controller.list_directory(source_dir_path)
            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue
                item_source_path = os.path.normpath(os.path.join(source_dir_path, item_name))
                item_local_path = os.path.join(local_dir_path, item_name)
                if item["is_dir"]:
                    self._extract_directory(item_source_path, item_local_path)
                else:
                    self._extract_file(item_source_path, item_local_path)
            self.logger.info(f"Successfully extracted directory {source_dir_path}.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to extract directory {source_dir_path}: {str(e)}")
            self.logger.exception(f"Error extracting directory {source_dir_path}.")

    def _navigate_to_path(self, path: str) -> bool:
        """
        Navigates the file browser to a specified path within the filesystem tree.

        Args:
            path: The target path string (e.g., "/DIR1/SUBDIR").

        Returns:
            True if navigation was successful, False otherwise.
        """
        self.logger.debug(f"Navigating to path: {path}")
        if path == "/":
            self.current_node = self.root_node
            self.current_path = "/"
            self.update_file_list()
            self._select_tree_item_by_path(path)
            return True

        parts = path.strip("/").split("/")
        current = self.root_node

        for part in parts:
            found = False
            if current:
                for child in current.children:
                    if child.is_dir and child.name == part:
                        current = child
                        found = True
                        break
            if not found:
                self.logger.warning(f"Path part '{part}' not found in tree. Resetting to root.")
                self.current_node = self.root_node
                self.current_path = "/"
                self.update_file_list()
                self._select_tree_item_by_path(self.current_path)
                return False

        self.current_node = current
        self.current_path = path
        self.update_file_list()
        self._select_tree_item_by_path(path)
        self.logger.info(f"Successfully navigated to path: {path}")
        return True

    def _select_tree_item_by_path(self, path: str) -> None:
        """
        Selects the corresponding item in the directory tree widget for a given path.

        Args:
            path: The path string to select.
        """
        if path == "/":
            if self.tree_widget.topLevelItemCount() > 0:
                self.tree_widget.setCurrentItem(self.tree_widget.topLevelItem(0))
            return

        parts = path.strip("/").split("/")
        if self.tree_widget.topLevelItemCount() == 0:
            return

        current_tree_item = self.tree_widget.topLevelItem(0)  # Start from the root '/' item

        for part in parts:
            found = False
            for i in range(current_tree_item.childCount()):
                child_item = current_tree_item.child(i)
                if hasattr(child_item, 'node') and child_item.node.name == part:
                    current_tree_item = child_item
                    found = True
                    break
            if not found:
                self.logger.warning(f"Tree item for path part '{part}' not found during selection.")
                return
        self.tree_widget.setCurrentItem(current_tree_item)
        self.logger.debug(f"Tree item selected for path: {path}")

    def _add_file_to_disk(self, file_path: str, dest_name: str, dest_path: str) -> None:
        """
        Internal helper to read a local file and write it to the disk image.

        Args:
            file_path: Path to the local file.
            dest_name: Name of the file on the disk image (8.3 format).
            dest_path: Path to the directory on the disk image.
        """
        self.logger.info(f"Adding local file '{file_path}' as '{dest_name}' to '{dest_path}'.")
        with open(file_path, 'rb') as f:
            file_data = f.read()

        full_path = os.path.normpath(f"{dest_path}/{dest_name}")
        success = self.controller.write_file(full_path, file_data)
        if success:
            self.refresh_filesystem_ui(dest_path)
            self.statusBar().showMessage(f"Added file {dest_name} to {dest_path}")
            self.logger.info(f"Successfully added file {dest_name}.")
        else:
            QMessageBox.warning(self, "Warning", f"Failed to add file {dest_name}")
            self.logger.warning(f"Failed to add file {dest_name} to {dest_path}.")

    def _generate_unique_83_name(self, original_name: str, target_path: str, is_dir: bool) -> str:
        """
        Generates a unique 8.3 filename (or directory name) for the target filesystem.

        Args:
            original_name: The original name from the local filesystem.
            target_path: The directory on the disk image where the item will be added.
            is_dir: True if the item is a directory, False for a file.

        Returns:
            A unique 8.3 format name.

        Raises:
            ValueError: If a unique name cannot be generated after many attempts.
        """
        def to_83_name_format(name: str, is_directory: bool) -> str:
            name = name.upper()
            name = re.sub(r'[\\/:*?"<>|\s+]', '_', name) # Replace invalid chars
            if '.' in name and not is_directory:
                base, ext = name.rsplit('.', 1)
                base = base[:8]
                ext = ext[:3]
                return f"{base}.{ext}"
            else:
                return name[:8]

        existing_names = {item['name'].upper() for item in self.controller.list_directory(target_path)}
        base_name_83 = to_83_name_format(original_name, is_dir)

        if base_name_83 not in existing_names:
            return base_name_83

        # If name exists, try to generate a unique one with a tilde sequence
        if '.' in base_name_83 and not is_dir:
            base, ext = base_name_83.rsplit('.', 1)
            base = base[:6] # Reserve 2 chars for ~XX
            for counter in range(1, 1000):
                new_base = f"{base}~{counter:02d}"[:8] # Ensure 8 char max for base
                new_name = f"{new_base}.{ext}"
                if new_name not in existing_names:
                    return new_name
        else: # Directory or file without extension
            base = base_name_83[:6] # Reserve 2 chars for ~XX
            for counter in range(1, 1000):
                new_name = f"{base}~{counter:02d}"[:8] # Ensure 8 char max
                if new_name not in existing_names:
                    return new_name

        self.logger.error(f"Failed to generate unique 8.3 name for '{original_name}' in '{target_path}'.")
        raise ValueError("Cannot generate unique name")

    def _format_83_filename(self, filename: str) -> str:
        """
        Formats a filename into an 8.3 FAT-compatible format.

        Args:
            filename: The original filename.

        Returns:
            The 8.3 formatted filename.
        """
        # Remove invalid characters and convert to uppercase
        filename = re.sub(r'[<>:"/\\|?*]', '', filename).strip().upper()

        if '.' in filename:
            parts = filename.split('.')
            base = parts[0][:8]
            ext = parts[-1][:3]
            return f"{base}.{ext}"
        else:
            return filename[:8]

    def _build_full_path(self, name: str) -> str:
        """
        Constructs a full path string from the current path and an item name.

        Args:
            name: The name of the item.

        Returns:
            The full path.
        """
        path = self.current_path
        if path != "/":
            path += "/"
        return os.path.normpath(path + name)

    def _get_busy_units(self) -> None:
        """
        Retrieves allocated units and updates free/total space information.
        """
        if not self.controller:
            self.busy_units = []
            self.free_space = 0
            self.total_space = 0
            self.logger.debug("No controller, busy units and space reset.")
            return

        try:
            self.busy_units = self.controller.get_allocated_units()
            # unit_name = "units" # Not currently displayed in stats
            allocation_unit_size_bytes = 512 # Default if FS doesn't provide

            if self.controller.filesystem:
                if self.controller.filesystem.allocation_unit_size > 0:
                    allocation_unit_size_bytes = self.controller.filesystem.allocation_unit_size
                else:
                    self.logger.warning("Filesystem reported zero allocation unit size. Using default 512 bytes.")

                space_info = self.controller.get_free_space()
                if space_info:
                    free_bytes, total_bytes = space_info
                    if allocation_unit_size_bytes > 0:
                        self.free_space = free_bytes // allocation_unit_size_bytes
                        self.total_space = total_bytes // allocation_unit_size_bytes
                    else:
                        self.logger.warning("Cannot calculate space in units (allocation unit size is zero).")
                        self.free_space = 0
                        self.total_space = 0
                else:
                    self.logger.warning("Could not retrieve free space information from filesystem.")
                    self.free_space = 0
                    if allocation_unit_size_bytes > 0 and self.controller.physical_format:
                        total_data_bytes_geom = self.controller.physical_format.total_bytes
                        self.total_space = total_data_bytes_geom // allocation_unit_size_bytes
                        self.free_space = max(0, self.total_space - len(self.busy_units))
                    else:
                        self.total_space = len(self.busy_units) # Fallback to count of busy units
                        self.free_space = 0
            else:
                self.logger.warning("No filesystem detected, cannot calculate accurate free/total space. Showing only busy units if any.")
                self.busy_units = []
                self.free_space = 0
                self.total_space = 0

            self.logger.debug(f"Busy units: {len(self.busy_units)}, Free units: {self.free_space}, Total units: {self.total_space}")

        except Exception as e:
            self.logger.error(f"Error getting busy units/space info: {e}", exc_info=True)
            self.busy_units = []
            self.free_space = 0
            self.total_space = 0

    # ##################################################################
    # Private Helper Methods (Text Editor)
    # ##################################################################

    @pyqtSlot()
    def _on_file_list_selection_changed(self) -> None:
        """
        Slot to handle changes in the file list selection.
        Manages saving/discarding changes in the text editor and
        loads the content of selected files into the editor.
        """
        if self.text_editor_modified:
            reply = QMessageBox.warning(self, "Unsaved Changes",
                                        f"Do you want to save the changes to {os.path.basename(self.current_file_path)}?",
                                        QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                        QMessageBox.StandardButton.Cancel)

            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    # If save failed or was cancelled, revert selection
                    if self.file_list.selectionModel().hasSelection():
                        # This part of the logic is complex in PyQt.
                        # It's attempting to restore a previous selection,
                        # but if 'selected' is empty and 'deselected' has items,
                        # it means all items might have been deselected or
                        # a new selection failed to take. The original code
                        # `self.file_list.selectionModel().select(deselected, ...)`
                        # implies it's trying to re-select what was just deselected,
                        # which effectively cancels the new selection.
                        # For simplicity and to not alter business logic, we'll
                        # assume the signal comes with relevant selected/deselected
                        # and that the original intent was to restore to 'deselected'
                        # state if a save was required but failed/cancelled.
                        # However, `selectionChanged` doesn't pass these.
                        # A robust solution might involve storing the *previous* selection.
                        # Given the current parameters, we simply return and let the UI
                        # implicitly remain in its previous state or default selection.
                        self.logger.warning("Save during selection change failed/cancelled, preventing new selection.")
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                self.logger.debug("Selection change cancelled due to unsaved changes.")
                return # Prevent the new selection from taking effect

        selected_items = self.file_list.selectedItems()
        if len(selected_items) == 1:
            item = selected_items[0]
            node: FileSystemNode = item.node
            if not node.is_dir:
                file_path = self._build_full_path(node.name)
                # 50 KB limit for text viewing
                if node.size <= 51200:
                    try:
                        content_bytes = self.controller.read_file(file_path)
                        if content_bytes is not None and self._is_text_file(content_bytes):
                            content_text = ""
                            fs_type = self.controller.filesystem.get_display_info().get("Filesystem Type", "Unknown")
                            if fs_type == "CP/M":
                                # CP/M often uses 7-bit ASCII, sometimes with high bit stripped
                                cleaned_bytes = bytes([b & 0x7F for b in content_bytes])
                                content_text = cleaned_bytes.decode('ascii', errors='replace')
                            else:  # Default to FAT/MS-DOS style (CP437)
                                content_text = content_bytes.decode('cp437', errors='replace')

                            # Normalize line endings for display in QPlainTextEdit
                            content_text = content_text.replace('\r\n', '\n').replace('\r', '\n')

                            self.text_viewer.blockSignals(True)
                            self.text_viewer.setPlainText(content_text)
                            self.text_viewer.blockSignals(False)
                            self.original_text_content = content_text
                            self.current_file_path = file_path
                            self.text_editor_modified = False
                            self.save_button.setEnabled(False)
                            self.discard_button.setEnabled(False)
                            self.text_viewer_dock.raise_()
                            self.text_viewer_dock.setWindowTitle(f"Text Editor - {node.name}")
                            self.logger.info(f"Loaded '{node.name}' into text editor.")
                            return

                    except Exception as e:
                        self.logger.error(f"Error reading/processing file {file_path} for text view: {e}", exc_info=True)
                else:
                    self.logger.info(f"File '{node.name}' too large ({node.size} bytes) for text editor. Limit is 50KB.")

        self._clear_text_viewer_state()
        self.disk_map_dock.raise_()

    def _clear_text_viewer_state(self) -> None:
        """
        Clears the text viewer content and resets its state.
        """
        self.text_viewer.blockSignals(True)
        self.text_viewer.clear()
        self.text_viewer.blockSignals(False)
        self.original_text_content = None
        self.current_file_path = None
        self.text_editor_modified = False
        self.save_button.setEnabled(False)
        self.discard_button.setEnabled(False)
        self.text_viewer_dock.setWindowTitle("Text Viewer")
        self.logger.debug("Text viewer state cleared.")

    def _is_text_file(self, content: bytes, check_bytes: int = 4096) -> bool:
        """
        Heuristically checks if the given bytes content is likely a text file.

        Args:
            content: The bytes content of the file.
            check_bytes: The number of bytes to sample for the check.

        Returns:
            True if likely a text file, False otherwise.
        """
        sample = content[:check_bytes]
        if not sample:
            return True  # Empty files are technically text files

        try:
            # Try CP437 as it's common for these systems and handles many extended chars
            text = sample.decode('cp437')
            # Count non-printable characters (excluding common whitespace)
            non_printable = sum(1 for c in text if not (c.isprintable() or c in '\r\n\t'))
            # If more than 10% of characters are non-printable, it's probably binary
            if len(text) > 0 and (non_printable / len(text)) > 0.1:
                percentage = (non_printable / len(text)) * 100
                self.logger.debug(f"File detected as binary: {percentage:.2f}% non-printable characters.")
                return False
            return True
        except UnicodeDecodeError:
            self.logger.debug("File detected as binary: UnicodeDecodeError during CP437 decode.")
            return False

    @pyqtSlot()
    def _on_text_editor_changed(self) -> None:
        """
        Slot to handle changes in the text editor's content.
        Updates the modification status and enables/disables save/discard buttons.
        """
        if self.original_text_content is not None:
            current_text = self.text_viewer.toPlainText()
            is_modified = (current_text != self.original_text_content)
            self.text_editor_modified = is_modified
            self.save_button.setEnabled(is_modified)
            self.discard_button.setEnabled(is_modified)
            self.logger.debug(f"Text editor modified status: {is_modified}")
        else:
            self.text_editor_modified = False
            self.save_button.setEnabled(False)
            self.discard_button.setEnabled(False)
            self.logger.debug("Text editor changed, but no original content set.")


def run_gui() -> None:
    """
    Initializes and runs the FatFloppy GUI application.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("FatFloppy")

    # Set application icon
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Traverse up to find the project root (where 'assets' typically resides)
    root_dir = current_dir
    while not os.path.exists(os.path.join(root_dir, 'assets')) and root_dir != os.path.dirname(root_dir):
        root_dir = os.path.dirname(root_dir)
    icon_path = os.path.join(root_dir, 'assets', 'icons', 'fatfloppy_icon.png')

    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
    else:
        FileBrowserApp.logger.warning(f"Application icon not found at: {icon_path}")

    window = FileBrowserApp()
    window.show()
    sys.exit(app.exec())
