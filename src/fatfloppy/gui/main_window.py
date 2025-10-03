# src/fatfloppy/gui/main_window.py
import copy
import datetime
import logging
import os
import re
from typing import Any, Dict, List, Optional, Callable, Tuple

from PyQt6.QtCore import QPointF, Qt, QSettings, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette, QAction
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QSizePolicy,
                             QDockWidget, QFileDialog, QGraphicsView, QGroupBox,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                             QMainWindow, QMessageBox, QPlainTextEdit,
                             QPushButton, QToolBar, QTreeWidget, QMenu,
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
    MAX_RECENT_FILES = 10

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
        self.selected_file_path: Optional[str] = None
        self.selected_file_units: List[int] = []

        # Text Editor / Hex Viewer State
        self.original_text_content: Optional[str] = None
        self.text_editor_modified: bool = False
        self.current_hex_file_path: Optional[str] = None

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

        self._open_disk_image_by_path(file_path)

    def _read_file_threaded(self, path: str, on_success: Callable) -> None:
        """Thread-safe file reading."""
        def operation():
            return self.controller.read_file(path)

        def success_handler(content):
            if content is not None:
                on_success(content)
            else:
                QMessageBox.warning(self, "Warning", f"Could not read file: {path}")

        self._run_threaded_operation(
            operation,
            f"Reading {os.path.basename(path)}",
            on_success=success_handler,
            cancelable=False
        )

    def _write_file_threaded(self, path: str, data: bytes, on_success: Callable) -> None:
        """Thread-safe file writing."""
        def operation():
            success = self.controller.write_file(path, data)
            if not success:
                raise Exception("Write operation returned False")
            return success

        self._run_threaded_operation(
            operation,
            f"Writing {os.path.basename(path)}",
            on_success=on_success,
            cancelable=False
        )

    def _list_directory_threaded(self, path: str, on_success: Callable) -> None:
        """Thread-safe directory listing."""
        def operation():
            return self.controller.list_directory(path)

        self._run_threaded_operation(
            operation,
            f"Reading directory {path}",
            on_success=on_success,
            cancelable=False
        )

    def _open_disk_image_by_path(self, file_path: str) -> None:
        """
        Opens a disk image file at the specified path.

        Args:
            file_path: The path to the disk image file to open.
        """
        if not file_path or not os.path.exists(file_path):
            self.logger.warning(f"File path does not exist: {file_path}")
            return

        _, ext = os.path.splitext(file_path)
        ext_lower = ext.lower()

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
                self._add_to_recent_files(file_path)
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
        """Opens a physical floppy drive using Greaseweazle."""
        self.logger.debug("Attempting to open a physical floppy.")

        try:
            dialog = DriveSelectionDialog(self)
            if not dialog.exec():
                self.logger.debug("Drive Selection dialog cancelled.")
                return

            drive_letter, drive_size, format_info = dialog.get_selection()
            self.logger.info(f"Opening physical floppy: Drive {drive_letter}, Size {drive_size}, Format {format_info}")

            # Define the operation
            def open_operation(progress_callback=None):
                if progress_callback:
                    progress_callback(0, 100, "Initializing device...")

                controller = DiskController()

                if progress_callback:
                    progress_callback(30, 100, "Opening drive...")

                success = controller.open_disk(
                    None, "physical",
                    drive_letter=drive_letter,
                    drive_size=drive_size,
                    format_info=format_info
                )

                if not success:
                    raise Exception("Failed to open physical floppy")

                if progress_callback:
                    progress_callback(100, 100, "Complete")

                return controller

            # Define success handler
            def on_success(controller):
                self.controller = controller
                self.current_file_path = None
                self.refresh_filesystem_ui()

                if self.controller.physical_format and self.controller.physical_format.heads > 1:
                    self.head_action.setEnabled(True)
                    self.head_action.setText(f"Switch Head (Current: {self.current_head})")
                else:
                    self.head_action.setEnabled(False)
                    self.head_action.setText("Single-sided disk")

                format_name, _ = self.controller.detect_format()
                format_text = f" using {format_name}" if format_name else ""
                if format_info and not format_info.get("profile_name"):
                    format_text += f" (Custom format: {format_info.get('cylinders')}x{format_info.get('heads')}x{format_info.get('sectors_per_track')})"
                self.statusBar().showMessage(
                    f"Loaded physical floppy{format_text} (Drive: {drive_letter}, Size: {drive_size}\")"
                )
                self.logger.info(f"Successfully opened physical floppy on drive {drive_letter}.")

            # Define error handler
            def on_error(exception):
                self.reset_ui()
                QMessageBox.critical(
                    self, "Error",
                    f"Failed to open physical floppy: {str(exception)}"
                )
                self.logger.exception("Error opening physical floppy.")

            # Run in thread
            self._run_threaded_operation(
                open_operation,
                "Opening Physical Floppy",
                on_success=on_success,
                on_error=on_error,
                cancelable=False
            )

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

        # Check if using physical drive
        is_physical = (self.controller.driver and
                    self.controller.driver.driver_category == "physical")

        if len(selected_items) == 1:
            item = selected_items[0]
            node: FileSystemNode = item.node
            source_path = self._build_full_path(node.name)

            if node.is_dir:
                base_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Extract To")
                if not base_dir:
                    return
                local_dir_path = os.path.join(base_dir, node.name)

                if is_physical:
                    def extract_op(progress_callback=None):
                        self._extract_directory(source_path, local_dir_path, progress_callback)
                        return True

                    self._run_threaded_operation(
                        extract_op,
                        f"Extracting {node.name}",
                        on_success=lambda _: self.statusBar().showMessage("Extraction complete."),
                        cancelable=False
                    )
                else:
                    self._extract_directory(source_path, local_dir_path)
                    self.statusBar().showMessage("Extraction complete.")
            else:
                save_path, _ = QFileDialog.getSaveFileName(self, "Save File", node.name)
                if not save_path:
                    return

                if is_physical:
                    def extract_op(progress_callback=None):
                        if progress_callback:
                            progress_callback(0, 1, f"Extracting {node.name}...")
                        self._extract_file(source_path, save_path)
                        if progress_callback:
                            progress_callback(1, 1, "Complete")
                        return True

                    self._run_threaded_operation(
                        extract_op,
                        f"Extracting {node.name}",
                        on_success=lambda _: self.statusBar().showMessage("Extraction complete."),
                        cancelable=False
                    )
                else:
                    self._extract_file(source_path, save_path)
                    self.statusBar().showMessage("Extraction complete.")
        else:
            base_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Extract To")
            if not base_dir:
                return

            if is_physical:
                def extract_multiple_op(progress_callback=None):
                    total = len(selected_items)
                    for idx, item in enumerate(selected_items):
                        node = item.node
                        if progress_callback:
                            progress_callback(idx, total, f"Extracting {node.name}...")

                        source_path = self._build_full_path(node.name)
                        if node.is_dir:
                            local_dir_path = os.path.join(base_dir, node.name)
                            self._extract_directory(source_path, local_dir_path)
                        else:
                            local_file_path = os.path.join(base_dir, node.name)
                            self._extract_file(source_path, local_file_path)

                    if progress_callback:
                        progress_callback(total, total, "Complete")
                    return True

                self._run_threaded_operation(
                    extract_multiple_op,
                    f"Extracting {len(selected_items)} items",
                    on_success=lambda _: self.statusBar().showMessage("Extraction complete."),
                    cancelable=False
                )
            else:
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

        current_path = self.current_path

        # Check if using physical drive
        is_physical = (self.controller.driver and
                    self.controller.driver.driver_category == "physical")

        if is_physical:
            def delete_op(progress_callback=None):
                failed_paths: List[str] = []
                total = len(paths_to_delete)

                for idx, path in enumerate(paths_to_delete):
                    if progress_callback:
                        progress_callback(idx, total, f"Deleting {os.path.basename(path)}...")

                    try:
                        success = self.controller.delete_item_recursive(path)
                        if not success:
                            failed_paths.append(path)
                            self.logger.warning(f"Failed to delete item: {path}")
                    except Exception as e:
                        failed_paths.append(path)
                        self.logger.error(f"Error deleting item {path}: {e}", exc_info=True)

                if progress_callback:
                    progress_callback(total, total, "Complete")

                return failed_paths

            def on_delete_success(failed_paths):
                self.refresh_filesystem_ui(current_path)

                if not failed_paths:
                    self.statusBar().showMessage(f"Deleted {len(paths_to_delete)} item(s)")
                    self.logger.info(f"Successfully deleted {len(paths_to_delete)} item(s).")
                else:
                    failed_msg = "Failed to delete:\n" + "\n".join(failed_paths)
                    QMessageBox.warning(self, "Deletion Failed", failed_msg)
                    self.statusBar().showMessage(f"Deleted {len(paths_to_delete) - len(failed_paths)} item(s), {len(failed_paths)} failed.")
                    self.logger.warning(f"Deletion completed with {len(failed_paths)} failures.")

            self._run_threaded_operation(
                delete_op,
                f"Deleting {len(paths_to_delete)} item(s)",
                on_success=on_delete_success,
                cancelable=False
            )
        else:
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

        full_path = f"{current_path}/{dir_name}".replace("//", "/")
        self.logger.info(f"Attempting to create directory: {full_path}")

        # Check if using physical drive
        is_physical = (self.controller.driver and
                    self.controller.driver.driver_category == "physical")

        if is_physical:
            def create_dir_op(progress_callback=None):
                if progress_callback:
                    progress_callback(0, 1, f"Creating {dir_name}...")

                success = self.controller.create_directory(full_path)
                if not success:
                    raise Exception(f"Failed to create directory {dir_name}")

                if progress_callback:
                    progress_callback(1, 1, "Complete")

                return True

            def on_create_success(_):
                self.refresh_filesystem_ui(current_path)
                self.statusBar().showMessage(f"Created directory {dir_name} in {current_path}")
                self.logger.info(f"Successfully created directory: {full_path}")

            def on_create_error(exception):
                QMessageBox.warning(self, "Warning", str(exception))
                self.logger.warning(f"Failed to create directory {full_path}.")

            self._run_threaded_operation(
                create_dir_op,
                f"Creating directory {dir_name}",
                on_success=on_create_success,
                on_error=on_create_error,
                cancelable=False
            )
        else:
            try:
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

        # Use import_multiple_paths with auto_name=False to prompt for name
        self.import_multiple_paths([file_path], self.current_path, auto_name=False)

    def import_multiple_paths(self, file_paths: List[str], target_path: str, auto_name: bool = True) -> None:
        """
        Imports multiple files/directories at once with progress tracking.
        Can also handle single files.

        Args:
            file_paths: List of local file/directory paths to import.
            target_path: Target path on the disk image.
            auto_name: If True, generates unique names automatically. If False, prompts for name (only works with single file).
        """
        if not file_paths:
            return

        # Handle single file without auto-naming (interactive mode)
        if len(file_paths) == 1 and os.path.isfile(file_paths[0]) and not auto_name:
            local_path = file_paths[0]
            base_name = os.path.basename(local_path)
            base_name = self._format_83_filename(base_name)
            new_name, ok = QInputDialog.getText(
                self, "File Name",
                f"Enter file name for {base_name} (8.3 format):",
                text=base_name
            )
            if not ok or not new_name:
                self.logger.debug("File name input cancelled for import.")
                return

            existing_names = [item['name'].upper() for item in self.controller.list_directory(target_path)]
            if new_name.upper() in existing_names:
                QMessageBox.warning(self, "Warning", f"File '{new_name}' already exists in {target_path}")
                self.logger.warning(f"File '{new_name}' already exists, import aborted.")
                return

            # Use the user-provided name instead of auto-generating
            file_paths_with_names = [(local_path, new_name)]
        else:
            # Auto-name mode: generate names for all files
            file_paths_with_names = []
            for local_path in file_paths:
                local_path = os.path.normpath(local_path)
                if os.path.isfile(local_path):
                    original_name = os.path.basename(local_path)
                    new_name = self._generate_unique_83_name(original_name, target_path, is_dir=False)
                    file_paths_with_names.append((local_path, new_name))
                elif os.path.isdir(local_path):
                    original_name = os.path.basename(local_path)
                    if not original_name:
                        continue
                    new_name = self._generate_unique_83_name(original_name, target_path, is_dir=True)
                    file_paths_with_names.append((local_path, new_name))

        self.logger.info(f"Importing {len(file_paths_with_names)} items to '{target_path}'.")

        # Check if using physical drive
        is_physical = (self.controller.driver and
                    self.controller.driver.driver_category == "physical")

        if is_physical:
            def import_multiple_op(progress_callback=None):
                total = len(file_paths_with_names)

                for idx, (local_path, dest_name) in enumerate(file_paths_with_names):
                    if progress_callback:
                        progress_callback(idx, total, f"Importing {dest_name}...")

                    if os.path.isfile(local_path):
                        self._add_file_to_disk(local_path, dest_name, target_path)

                    elif os.path.isdir(local_path):
                        new_dir_path = f"{target_path}/{dest_name}" if target_path != "/" else f"/{dest_name}"

                        success = self.controller.create_directory(new_dir_path)
                        if not success:
                            self.logger.error(f"Failed to create directory {dest_name}")
                            continue

                        # Import directory contents
                        self._import_directory_recursive(local_path, new_dir_path, progress_callback)

                if progress_callback:
                    progress_callback(total, total, "Complete")
                return total

            def on_success(count):
                self.refresh_filesystem_ui(target_path)
                item_word = "item" if count == 1 else "items"
                self.statusBar().showMessage(f"Imported {count} {item_word} to '{target_path}'")

            def on_error(exception):
                QMessageBox.critical(self, "Error", f"Failed to import items: {str(exception)}")
                self.logger.exception("Error importing items.")

            item_word = "item" if len(file_paths_with_names) == 1 else "items"
            self._run_threaded_operation(
                import_multiple_op,
                f"Importing {len(file_paths_with_names)} {item_word}",
                on_success=on_success,
                on_error=on_error,
                cancelable=False
            )
        else:
            # Non-physical drives: process synchronously
            for local_path, dest_name in file_paths_with_names:
                if os.path.isfile(local_path):
                    self._add_file_to_disk(local_path, dest_name, target_path)
                elif os.path.isdir(local_path):
                    new_dir_path = f"{target_path}/{dest_name}" if target_path != "/" else f"/{dest_name}"
                    success = self.controller.create_directory(new_dir_path)
                    if not success:
                        self.logger.error(f"Failed to create directory {dest_name}")
                        continue

                    # Import directory contents
                    items = os.listdir(local_path)
                    sub_paths = [os.path.join(local_path, item) for item in items]
                    self.import_multiple_paths(sub_paths, new_dir_path, auto_name=True)

            self.refresh_filesystem_ui(target_path)
            item_word = "item" if len(file_paths_with_names) == 1 else "items"
            self.statusBar().showMessage(f"Imported {len(file_paths_with_names)} {item_word} to '{target_path}'")

    def _import_directory_recursive(self, local_path: str, target_path: str, progress_callback=None) -> None:
        """
        Helper for recursive directory import from within a worker thread.
        Does NOT update GUI.

        Args:
            local_path: Local directory path to import.
            target_path: Target path on disk image.
            progress_callback: Optional callback for progress reporting.
        """
        dir_name = os.path.basename(local_path)
        new_name = self._generate_unique_83_name(dir_name, target_path, is_dir=True)
        new_path = f"{target_path}/{new_name}" if target_path != "/" else f"/{new_name}"

        success = self.controller.create_directory(new_path)
        if not success:
            raise Exception(f"Failed to create directory {new_name}")

        items = os.listdir(local_path)
        total_items = len(items)

        for idx, item in enumerate(items):
            if progress_callback:
                progress_callback(idx, total_items, f"Importing {item}...")

            item_path = os.path.join(local_path, item)
            if os.path.isfile(item_path):
                item_name = self._generate_unique_83_name(os.path.basename(item_path), new_path, is_dir=False)
                self._add_file_to_disk(item_path, item_name, new_path)
            elif os.path.isdir(item_path):
                self._import_directory_recursive(item_path, new_path)

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
                normalized_text = current_text.replace('\n', '\r\n')

                content_bytes = b''
                fs_type = self.controller.filesystem.get_display_info().get("Filesystem Type", "Unknown")

                if fs_type == "CP/M":
                    content_bytes = bytes([b & 0x7F for b in normalized_text.encode('ascii', errors='replace')])
                else:
                    content_bytes = normalized_text.encode('cp437', errors='replace')

                # Check if using physical drive
                is_physical = (self.controller.driver and
                            self.controller.driver.driver_category == "physical")

                if is_physical:
                    file_path = self.current_file_path
                    current_path = self.current_path

                    def save_op():
                        if not self.controller.write_file(file_path, content_bytes):
                            raise Exception("Write operation failed")
                        return current_text

                    def on_save_success(saved_text):
                        self.statusBar().showMessage(f"Saved changes to {os.path.basename(file_path)}")
                        self.original_text_content = saved_text
                        self.text_editor_modified = False
                        self.save_button.setEnabled(False)
                        self.discard_button.setEnabled(False)
                        self.refresh_filesystem_ui(preserve_path=current_path)
                        self.logger.info(f"Successfully saved {file_path}")

                    def on_save_error(exception):
                        QMessageBox.warning(self, "Save Failed", f"Could not write changes: {str(exception)}")
                        self.logger.warning(f"Failed to write file data for {file_path}")

                    self._run_threaded_operation(
                        save_op,
                        f"Saving {os.path.basename(file_path)}",
                        on_success=on_save_success,
                        on_error=on_save_error,
                        cancelable=False
                    )
                    # For threaded operation, return True immediately as the operation is queued
                    success = True
                else:
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
            self.disk_map._draw_no_disk_message(
                self.app_font,
                self.palette().color(QPalette.ColorRole.WindowText)
            )
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
            selected_file_units=self.selected_file_units,
            selected_file_path=self.selected_file_path
        )
        self.logger.debug(f"Disk map drawn for head {self.current_head}.")

    @pyqtSlot()
    def view_file_content(self) -> None:
        """Views the selected file - threaded for physical drives."""
        selected_items = self.file_list.selectedItems()
        if len(selected_items) != 1:
            QMessageBox.information(self, "Info", "Please select a single file to view.")
            return

        item = selected_items[0]
        if not hasattr(item, 'node'):
            QMessageBox.warning(self, "Warning", "Invalid item selected.")
            return

        node: FileSystemNode = item.node
        if node.is_dir:
            QMessageBox.information(self, "Info", "Cannot view directory contents.")
            return

        file_path = self._build_full_path(node.name)

        # Check if using physical drive
        is_physical = (self.controller.driver and
                    self.controller.driver.driver_category == "physical")

        if is_physical:
            # Thread the operation
            def on_read_success(content_bytes):
                if self._is_text_file(content_bytes):
                    self._clear_hex_viewer_state()
                    self._load_text_editor(file_path, node.name, content_bytes)
                else:
                    self._clear_text_viewer_state()
                    self._load_hex_viewer(file_path, node.name, content_bytes)

            self._read_file_threaded(file_path, on_read_success)
        else:
            # Direct read for image files (fast enough)
            try:
                content_bytes = self.controller.read_file(file_path)
                if content_bytes is None:
                    QMessageBox.warning(self, "Warning", f"Could not read file: {node.name}")
                    return

                if self._is_text_file(content_bytes):
                    self._clear_hex_viewer_state()
                    self._load_text_editor(file_path, node.name, content_bytes)
                else:
                    self._clear_text_viewer_state()
                    self._load_hex_viewer(file_path, node.name, content_bytes)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error reading file: {str(e)}")

    # ##################################################################
    # Private UI Setup Methods
    # ##################################################################

    def _init_ui(self) -> None:
        """Initializes and lays out the main UI components of the application."""
        settings = QSettings("FatFloppy", "FatFloppy")
        restore_dock_layout = settings.value("window/restore_dock_layout", False, type=bool)

        self._init_window_settings()
        self._create_menus()
        self._create_toolbars()
        self._create_docks()

        # Only setup default layout if NOT restoring custom layout
        if not restore_dock_layout:
            self._setup_dock_layout()

        self._load_window_state()
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

        # Recent Files submenu
        self.recent_files_menu = QMenu("Recent Files", self)
        file_menu.addMenu(self.recent_files_menu)
        self._update_recent_files_menu()  # Populate it initially

        file_menu.addSeparator()

        open_floppy_action = QAction("Open Physical Floppy", self)
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        if not self.greaseweazle_available:
            open_floppy_action.setEnabled(False)
            self.logger.info("Greaseweazle not available, 'Open Physical Floppy' disabled.")
        file_menu.addAction(open_floppy_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setMenuRole(QAction.MenuRole.QuitRole)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # View Menu
        view_menu = menu_bar.addMenu("&View")

        save_layout_action = QAction("Save Current Layout", self)
        save_layout_action.triggered.connect(self._save_current_layout)
        view_menu.addAction(save_layout_action)

        reset_layout_action = QAction("Reset to Default Layout", self)
        reset_layout_action.triggered.connect(self._reset_layout)
        view_menu.addAction(reset_layout_action)

        view_menu.addSeparator()

        # Add checkbox for auto-restore custom layout
        self.restore_layout_action = QAction("Restore Custom Layout on Startup", self)
        self.restore_layout_action.setCheckable(True)
        settings = QSettings("FatFloppy", "FatFloppy")
        self.restore_layout_action.setChecked(settings.value("window/restore_dock_layout", False, type=bool))
        self.restore_layout_action.triggered.connect(self._toggle_restore_layout)
        view_menu.addAction(self.restore_layout_action)

        self.logger.debug("Menus created.")

    def _save_current_layout(self) -> None:
        """Saves the current dock layout exactly as it is."""
        settings = QSettings("FatFloppy", "FatFloppy")
        current_state = self.saveState()
        settings.setValue("window/state", current_state)
        settings.setValue("window/restore_dock_layout", True)
        self.restore_layout_action.setChecked(True)

        # Save floating dock geometries
        floating_geometries = {}
        for dock_name in ["TreeDock", "DiskInfoDock", "FileListDock", "DiskMapDock", "TextEditorDock", "HexViewerDock"]:
            dock = self.findChild(QDockWidget, dock_name)
            if dock and dock.isFloating():
                floating_geometries[dock_name] = dock.saveGeometry()

        if floating_geometries:
            settings.setValue("window/floating_geometries", floating_geometries)
            self.logger.info(f"Saved custom dock layout with {len(floating_geometries)} floating dock(s)")
        else:
            settings.remove("window/floating_geometries")
            self.logger.info(f"Saved custom dock layout (state size: {len(current_state)} bytes)")

        QMessageBox.information(
            self, "Layout Saved",
            "Current layout saved and will be restored on next startup."
        )

    def _reset_layout(self) -> None:
        """Resets the dock layout to default configuration."""
        reply = QMessageBox.question(
            self, "Reset Layout",
            "Reset all dock positions to default layout?\n\n"
            "The application will restart to apply changes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # Clear custom layout settings
            settings = QSettings("FatFloppy", "FatFloppy")
            settings.setValue("window/restore_dock_layout", False)
            settings.remove("window/state")
            self.restore_layout_action.setChecked(False)

            self.logger.info("Layout reset to defaults, restart required")

            # Inform user and offer to restart
            restart_reply = QMessageBox.question(
                self, "Restart Required",
                "Layout has been reset to defaults.\n\n"
                "Restart now to apply changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )

            if restart_reply == QMessageBox.StandardButton.Yes:
                # Save any current work before restarting
                if self.text_editor_modified:
                    save_reply = QMessageBox.question(
                        self, "Unsaved Changes",
                        "Save changes in text editor before restarting?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Yes
                    )
                    if save_reply == QMessageBox.StandardButton.Yes:
                        if not self.save_file():
                            return
                    elif save_reply == QMessageBox.StandardButton.Cancel:
                        return

                # Restart the application
                QApplication.quit()
                import sys
                import subprocess
                subprocess.Popen([sys.executable] + sys.argv)

    def _toggle_restore_layout(self, checked: bool) -> None:
        """Toggles whether custom layout should be restored on startup."""
        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("window/restore_dock_layout", checked)

        if checked:
            # When enabling, tell user they need to use "Save Current Layout"
            QMessageBox.information(
                self, "Custom Layout Enabled",
                "Custom layout restoration is now enabled.\n\n"
                "Use 'View > Save Current Layout' to save your current arrangement.\n"
                "The saved layout will be restored on next startup."
            )
            self.logger.info("Custom layout restoration enabled")
        else:
            self.logger.info("Custom layout restoration disabled - will use defaults on next startup")

    def _load_recent_files(self) -> List[str]:
        """Loads the list of recent files from settings."""
        settings = QSettings("FatFloppy", "FatFloppy")
        recent = settings.value("recent_files", [])
        if recent is None:
            recent = []
        elif isinstance(recent, str):
            recent = [recent]
        self.logger.debug(f"Loaded {len(recent)} recent files from settings")
        return recent

    def _save_recent_files(self, recent_files: List[str]) -> None:
        """Saves the list of recent files to settings."""
        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("recent_files", recent_files)
        self.logger.debug(f"Saved {len(recent_files)} recent files to settings")

    def _add_to_recent_files(self, file_path: str) -> None:
        """
        Adds a file to the recent files list.

        Args:
            file_path: The absolute path to the file to add.
        """
        if not file_path or not os.path.exists(file_path):
            return

        recent_files = self._load_recent_files()

        # Remove if already in list
        if file_path in recent_files:
            recent_files.remove(file_path)

        # Add to front
        recent_files.insert(0, file_path)

        # Keep only MAX_RECENT_FILES
        recent_files = recent_files[:self.MAX_RECENT_FILES]

        self._save_recent_files(recent_files)
        self._update_recent_files_menu()
        self.logger.debug(f"Added '{file_path}' to recent files")

    def _update_recent_files_menu(self) -> None:
        """Updates the Recent Files submenu with current recent files."""
        self.recent_files_menu.clear()
        recent_files = self._load_recent_files()

        if not recent_files:
            no_recent_action = QAction("No recent files", self)
            no_recent_action.setEnabled(False)
            self.recent_files_menu.addAction(no_recent_action)
            return

        for file_path in recent_files:
            if os.path.exists(file_path):
                action = QAction(os.path.basename(file_path), self)
                action.setToolTip(file_path)
                action.triggered.connect(lambda checked, path=file_path: self._open_recent_file(path))
                self.recent_files_menu.addAction(action)

        self.recent_files_menu.addSeparator()
        clear_action = QAction("Clear Recent Files", self)
        clear_action.triggered.connect(self._clear_recent_files)
        self.recent_files_menu.addAction(clear_action)

    def _open_recent_file(self, file_path: str) -> None:
        """
        Opens a recent file.

        Args:
            file_path: The path to the file to open.
        """
        if not os.path.exists(file_path):
            QMessageBox.warning(
                self, "File Not Found",
                f"The file '{file_path}' no longer exists and will be removed from recent files."
            )
            recent_files = self._load_recent_files()
            if file_path in recent_files:
                recent_files.remove(file_path)
                self._save_recent_files(recent_files)
                self._update_recent_files_menu()
            return

        # Use the new helper method that takes a file path
        self._open_disk_image_by_path(file_path)

    def _clear_recent_files(self) -> None:
        """Clears the recent files list."""
        self._save_recent_files([])
        self._update_recent_files_menu()
        self.logger.info("Cleared recent files list")

    def _create_toolbars(self) -> None:
        """Creates the main toolbar and adds actions."""
        self.toolbar = QToolBar("Main Toolbar", self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        # LEFT SIDE - Disk Operations
        create_image_action = QAction("Create Image", self)
        create_image_action.setToolTip("Create a new blank disk image")
        create_image_action.triggered.connect(self.create_disk_image)
        self.toolbar.addAction(create_image_action)

        open_image_action = QAction("Open Image", self)
        open_image_action.setToolTip("Open a disk image file")
        open_image_action.triggered.connect(self.open_disk_image_file)
        self.toolbar.addAction(open_image_action)

        open_floppy_action = QAction("Open Floppy", self)
        open_floppy_action.setToolTip("Open a physical floppy drive")
        open_floppy_action.triggered.connect(self.open_physical_floppy)
        if not self.greaseweazle_available:
            open_floppy_action.setEnabled(False)
            self.logger.info("Greaseweazle not available, 'Open Floppy' disabled in toolbar.")
        self.toolbar.addAction(open_floppy_action)

        # Add spacer to push file operations to the right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.toolbar.addWidget(spacer)

        # RIGHT SIDE - File/Directory Operations
        view_action = QAction("View File", self)
        view_action.setToolTip("View selected file content")
        view_action.triggered.connect(self.view_file_content)
        self.toolbar.addAction(view_action)

        add_file_action = QAction("Add File", self)
        add_file_action.setToolTip("Add a file to current directory")
        add_file_action.triggered.connect(self.add_file)
        self.toolbar.addAction(add_file_action)

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

        self.logger.debug("Toolbars created.")

    def _create_docks(self) -> None:
        """Creates all dockable widgets for the application."""
        self._create_tree_dock()
        self._create_disk_info_dock()
        self._create_file_list_dock()
        self._create_disk_map_dock()
        self._create_text_editor_dock()
        self._create_hex_viewer_dock()
        self.logger.debug("Docks created.")

    def _create_tree_dock(self) -> None:
        """Creates and configures the directory tree dock."""
        self.tree_dock = QDockWidget("Directory Tree", self)
        self.tree_dock.setObjectName("TreeDock")  # CRITICAL for state restoration
        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderLabel("Directories")
        self.tree_widget.itemClicked.connect(self.select_directory)
        self.tree_dock.setWidget(self.tree_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.tree_dock)

    def _create_disk_info_dock(self) -> None:
        """Creates and configures the disk information dock."""
        self.disk_info_dock = QDockWidget("Disk Information", self)
        self.disk_info_dock.setObjectName("DiskInfoDock")  # CRITICAL for state restoration
        disk_info_widget = QWidget()
        disk_info_layout = QVBoxLayout(disk_info_widget)
        disk_info_layout.setContentsMargins(5, 5, 5, 5)
        disk_info_layout.setSpacing(6)

        self.physical_format_group = QGroupBox("Physical Geometry")
        self.physical_format_info = QLabel("No disk image loaded")
        self.physical_format_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.physical_format_info.setWordWrap(True)
        self.physical_format_info.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum
        )
        geometry_layout = QVBoxLayout(self.physical_format_group)
        geometry_layout.addWidget(self.physical_format_info)
        self.physical_format_group.setLayout(geometry_layout)

        self.filesystem_group = QGroupBox("Filesystem")
        self.filesystem_info = QLabel("No filesystem detected")
        self.filesystem_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.filesystem_info.setWordWrap(True)
        self.filesystem_info.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum
        )
        filesystem_layout = QVBoxLayout(self.filesystem_group)
        filesystem_layout.addWidget(self.filesystem_info)
        self.filesystem_group.setLayout(filesystem_layout)

        disk_info_layout.addWidget(self.physical_format_group)
        disk_info_layout.addWidget(self.filesystem_group)
        disk_info_layout.addStretch(1)
        disk_info_widget.setLayout(disk_info_layout)
        self.disk_info_dock.setWidget(disk_info_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.disk_info_dock)

    def _create_file_list_dock(self) -> None:
        """Creates and configures the file list dock."""
        self.file_list_dock = QDockWidget("Files", self)
        self.file_list_dock.setObjectName("FileListDock")  # CRITICAL for state restoration
        self.file_list = DragDropTreeWidget(self)
        self.file_list.itemDoubleClicked.connect(self._on_file_double_clicked)
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
        self.disk_map_dock.setObjectName("DiskMapDock")  # CRITICAL for state restoration
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
        self.text_viewer_dock.setObjectName("TextEditorDock")  # CRITICAL for state restoration
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
        self.save_button.setEnabled(False)

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

    def _create_hex_viewer_dock(self) -> None:
        """Creates and configures the hex viewer dock."""
        self.hex_viewer_dock = QDockWidget("Hex Viewer", self)
        self.hex_viewer_dock.setObjectName("HexViewerDock")  # CRITICAL for state restoration
        hex_viewer_widget = QWidget()
        hex_viewer_layout = QVBoxLayout(hex_viewer_widget)
        hex_viewer_layout.setContentsMargins(2, 2, 2, 2)
        hex_viewer_layout.setSpacing(4)

        self.hex_viewer = QPlainTextEdit()
        self.hex_viewer.setReadOnly(True)
        self.hex_viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        font_metrics = self.hex_viewer.fontMetrics()
        self.hex_viewer.setTabStopDistance(font_metrics.horizontalAdvance(' ') * 8)

        hex_viewer_layout.addWidget(self.hex_viewer)
        hex_viewer_widget.setLayout(hex_viewer_layout)
        self.hex_viewer_dock.setWidget(hex_viewer_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.hex_viewer_dock)


    @pyqtSlot(QTreeWidgetItem, int)
    def _on_file_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """
        Handles double-click on a file list item.
        Opens files in viewer, navigates into directories.

        Args:
            item: The item that was double-clicked.
            column: The column that was double-clicked.
        """
        if not hasattr(item, 'node'):
            return

        node: FileSystemNode = item.node

        if node.is_dir:
            # Double-click on directory: navigate into it
            new_path = self._build_full_path(node.name)
            self._navigate_to_path(new_path)
            self.logger.debug(f"Navigated to directory: {new_path}")
        else:
            # Double-click on file: view it
            self.view_file_content()

    def _setup_dock_layout(self) -> None:
        """Arranges and resizes the dockable widgets."""
        self.splitDockWidget(self.tree_dock, self.disk_info_dock, Qt.Orientation.Vertical)
        self.splitDockWidget(self.file_list_dock, self.disk_map_dock, Qt.Orientation.Horizontal)

        # Tab the viewers together: disk map, text editor, hex viewer
        self.tabifyDockWidget(self.disk_map_dock, self.text_viewer_dock)
        self.tabifyDockWidget(self.text_viewer_dock, self.hex_viewer_dock)
        self.disk_map_dock.raise_()  # Show disk map by default

        # Set initial sizes for docks
        self.resizeDocks([self.tree_dock, self.file_list_dock], [200, 1000], Qt.Orientation.Horizontal)
        self.resizeDocks([self.file_list_dock, self.disk_map_dock], [400, 600], Qt.Orientation.Horizontal)
        self.resizeDocks([self.tree_dock, self.disk_info_dock], [400, 400], Qt.Orientation.Vertical)

        # Set size policies
        self.tree_dock.setMinimumWidth(320)
        self.tree_dock.setMaximumWidth(400)
        self.disk_info_dock.setMinimumWidth(150)
        self.disk_info_dock.setMaximumWidth(400)
        self.file_list_dock.setMinimumWidth(400)
        self.disk_map_dock.setMinimumWidth(300)

        # Save this as the default state (only once)
        settings = QSettings("FatFloppy", "FatFloppy")
        if not settings.contains("window/default_state"):
            settings.setValue("window/default_state", self.saveState())
            self.logger.debug("Saved default dock layout state")

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
        Configures and sets fonts for various UI elements.
        Tries to load custom font from assets, then falls back to system monospace fonts.
        """
        custom_font_loaded = False

        # Find the assets directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = current_dir
        while not os.path.exists(os.path.join(root_dir, 'assets')) and root_dir != os.path.dirname(root_dir):
            root_dir = os.path.dirname(root_dir)

        font_path = os.path.join(root_dir, 'assets', 'fonts', 'JetBrainsMono-Regular.ttf')

        self.logger.debug(f"Looking for font at: {font_path}")
        self.logger.debug(f"Font file exists: {os.path.exists(font_path)}")

        if os.path.exists(font_path):
            # Try to load the font
            font_id = QFontDatabase.addApplicationFont(font_path)
            self.logger.debug(f"Font ID returned: {font_id}")

            if font_id != -1:
                font_families = QFontDatabase.applicationFontFamilies(font_id)
                self.logger.debug(f"Font families available: {font_families}")

                if font_families:
                    family_name = font_families[0]
                    self.app_font = QFont(family_name)
                    self.app_font.setPointSize(12)

                    # Apply to the application globally
                    QApplication.instance().setFont(self.app_font)

                    custom_font_loaded = True
                    self.logger.info(f"Successfully loaded custom font: {family_name}")
                else:
                    self.logger.warning(f"Font loaded but no families returned from {font_path}")
            else:
                self.logger.warning(f"Failed to load font (addApplicationFont returned -1) from {font_path}")
        else:
            self.logger.warning(f"Custom font file not found at {font_path}")

        # Fallback to system monospace fonts if custom font not loaded
        if not custom_font_loaded:
            monospace_fonts: List[str] = [
                "JetBrainsMono", "Courier New", "DejaVu Sans Mono", "Consolas",
                "Menlo", "Liberation Mono", "Monaco", "SF Mono"
            ]
            self.app_font = QFont()
            found_font = False

            for font_name in monospace_fonts:
                # Check using hasFamily instead of deprecated isFixedPitch
                if QFontDatabase.hasFamily(font_name):
                    self.app_font.setFamily(font_name)
                    self.app_font.setPointSize(12)
                    found_font = True
                    self.logger.info(f"Using fallback monospace font: {font_name}")
                    break

            if not found_font:
                default_monospace = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
                self.app_font = default_monospace
                self.app_font.setPointSize(12)
                self.logger.info(f"Using system default monospace font: {default_monospace.family()}")

            # Apply to application
            QApplication.instance().setFont(self.app_font)

        # Apply font globally to the application first
        QApplication.instance().setFont(self.app_font)

        # Then explicitly set it on the main window
        self.setFont(self.app_font)

        # Apply font to all specific widgets
        self.tree_widget.setFont(self.app_font)
        self.file_list.setFont(self.app_font)
        self.physical_format_info.setFont(self.app_font)
        self.filesystem_info.setFont(self.app_font)
        self.text_viewer.setFont(self.app_font)
        self.hex_viewer.setFont(self.app_font)

        # Apply to group boxes (for their titles)
        self.physical_format_group.setFont(self.app_font)
        self.filesystem_group.setFont(self.app_font)

        # Apply to dock widgets (for their titles)
        self.tree_dock.setFont(self.app_font)
        self.disk_info_dock.setFont(self.app_font)
        self.file_list_dock.setFont(self.app_font)
        self.disk_map_dock.setFont(self.app_font)
        self.text_viewer_dock.setFont(self.app_font)
        self.hex_viewer_dock.setFont(self.app_font)

        # Apply to toolbar
        self.toolbar.setFont(self.app_font)

        # Apply to menu bar
        self.menuBar().setFont(self.app_font)

        # Apply to status bar
        self.statusBar().setFont(self.app_font)

        # Update tab stops for hex viewer after font is set
        font_metrics = self.hex_viewer.fontMetrics()
        self.hex_viewer.setTabStopDistance(font_metrics.horizontalAdvance(' ') * 8)

        self.logger.info(f"Final font in use: {self.app_font.family()}, Size: {self.app_font.pointSize()}")

    def _load_window_state(self) -> None:
        """Loads and restores the window geometry and optionally dock positions from settings."""
        settings = QSettings("FatFloppy", "FatFloppy")

        # Always restore window geometry
        geometry = settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
            self.logger.debug("Restored window geometry from settings")
        else:
            self.resize(1400, 900)
            self.logger.debug("Using default window size")

        # Restore dock state if enabled
        restore_dock_layout = settings.value("window/restore_dock_layout", False, type=bool)

        if restore_dock_layout:
            state = settings.value("window/state")
            if state:
                success = self.restoreState(state)
                if success:
                    self.logger.info(f"Restored custom dock layout (state size: {len(state)} bytes)")

                    # Restore floating dock geometries
                    floating_geometries = settings.value("window/floating_geometries")
                    if floating_geometries:
                        for dock_name, dock_geometry in floating_geometries.items():
                            dock = self.findChild(QDockWidget, dock_name)
                            if dock and dock.isFloating():
                                dock.restoreGeometry(dock_geometry)
                                self.logger.debug(f"Restored floating geometry for {dock_name}")
                else:
                    self.logger.warning("Failed to restore custom dock layout - state may be corrupted")
            else:
                self.logger.warning("Custom dock layout enabled but no saved state found - using defaults")
        else:
            self.logger.info("Using default dock layout")

        self.logger.info("Window state loaded")

    def _save_window_state(self) -> None:
        """Saves the current window geometry and dock state to settings."""
        settings = QSettings("FatFloppy", "FatFloppy")
        settings.setValue("window/geometry", self.saveGeometry())

        # Save floating dock geometries separately
        floating_geometries = {}
        for dock_name in ["TreeDock", "DiskInfoDock", "FileListDock", "DiskMapDock", "TextEditorDock", "HexViewerDock"]:
            dock = self.findChild(QDockWidget, dock_name)
            if dock and dock.isFloating():
                floating_geometries[dock_name] = dock.saveGeometry()

        if floating_geometries:
            settings.setValue("window/floating_geometries", floating_geometries)
        else:
            settings.remove("window/floating_geometries")

        self.logger.debug("Saved window geometry (dock state NOT auto-saved)")

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
        self._clear_hex_viewer_state()
        self.hex_viewer.clear()
        self.current_hex_file_path = None
        self.hex_viewer_dock.setWindowTitle("Hex Viewer")
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

    def closeEvent(self, event) -> None:
        """
        Handles the window close event.
        Saves window state and prompts for unsaved changes.
        """
        if self.text_editor_modified:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                f"Do you want to save changes to {os.path.basename(self.current_file_path)}?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel
            )

            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    event.ignore()
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return

        # Save window state before closing
        self._save_window_state()
        event.accept()

    def _extract_directory(self, source_dir_path: str, local_dir_path: str, progress_callback=None) -> None:
        """
        Recursively extracts a directory and its contents from the disk image.

        Args:
            source_dir_path: The full path to the directory on the disk image.
            local_dir_path: The full path to create the directory locally.
            progress_callback: Optional callback for progress reporting.
        """
        try:
            self.logger.info(f"Extracting directory '{source_dir_path}' to '{local_dir_path}'.")
            os.makedirs(local_dir_path, exist_ok=True)
            items = self.controller.list_directory(source_dir_path)

            total_items = len([i for i in items if i["name"] not in [".", ".."]])
            current_item = 0

            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue

                if progress_callback:
                    progress_callback(current_item, total_items, f"Extracting {item_name}...")

                item_source_path = os.path.normpath(os.path.join(source_dir_path, item_name))
                item_local_path = os.path.join(local_dir_path, item_name)
                if item["is_dir"]:
                    self._extract_directory(item_source_path, item_local_path)
                else:
                    self._extract_file(item_source_path, item_local_path)

                current_item += 1

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
        Does NOT refresh UI - caller is responsible for that.

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
        if not success:
            raise Exception(f"Failed to write file {dest_name}")
        self.logger.info(f"Successfully added file {dest_name}.")

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
        Clears viewers and switches to disk map when selection changes.
        """
        if self.text_editor_modified:
            reply = QMessageBox.warning(
                self, "Unsaved Changes",
                f"Do you want to save the changes to {os.path.basename(self.current_file_path)}?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel
            )

            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    self.logger.warning("Save during selection change failed/cancelled, preventing new selection.")
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                self.logger.debug("Selection change cancelled due to unsaved changes.")
                return

        # Clear previous file selection tracking
        self.selected_file_path = None
        self.selected_file_units = []

        # Clear both text editor and hex viewer when selection changes
        self._clear_text_viewer_state()
        self._clear_hex_viewer_state()

        # Switch to disk map
        self.disk_map_dock.raise_()

        selected_items = self.file_list.selectedItems()

        if len(selected_items) == 1:
            item = selected_items[0]

            # Safety check: ensure item has the node attribute
            if not hasattr(item, 'node'):
                self.logger.warning("Selected item does not have 'node' attribute (possibly from drag-drop). Ignoring.")
                return

            node: FileSystemNode = item.node

            if not node.is_dir:
                file_path = self._build_full_path(node.name)
                self.selected_file_path = file_path

                # Get allocation units for this file
                if self.controller:
                    units = self.controller.get_file_allocation_units(file_path)
                    if units:
                        self.selected_file_units = units
                        self.logger.debug(f"File '{node.name}' uses {len(units)} units: {units[:10]}{'...' if len(units) > 10 else ''}")
                    else:
                        self.logger.debug(f"File '{node.name}' has no allocation units (empty or error)")

        # Redraw disk map with highlighted file
        self.draw_disk_map()

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

    def _clear_hex_viewer_state(self) -> None:
        """
        Clears the hex viewer content and resets its state.
        """
        self.hex_viewer.clear()
        self.current_hex_file_path = None
        self.hex_viewer_dock.setWindowTitle("Hex Viewer")
        self.logger.debug("Hex viewer state cleared.")

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

    def _run_threaded_operation(
        self,
        operation: Callable,
        operation_name: str,
        on_success: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        cancelable: bool = False,
        *args,
        **kwargs
    ) -> None:
        """
        Run a disk operation in a background thread with progress dialog.

        Args:
            operation: The function to run in background
            operation_name: Name for progress dialog
            on_success: Callback for successful completion (receives result)
            on_error: Callback for errors (receives exception)
            cancelable: Whether operation can be cancelled
            *args, **kwargs: Arguments for the operation
        """
        from .worker import DiskOperationWorker
        from .progress_dialog import ProgressDialog

        # Create progress dialog
        progress = ProgressDialog(
            title=operation_name,
            message=f"{operation_name}...",
            cancelable=cancelable,
            parent=self
        )

        # Create worker thread
        worker = DiskOperationWorker(operation, *args, **kwargs)

        # Connect signals
        worker.progress.connect(progress.update_progress)

        def on_finished(result):
            progress.accept()
            if on_success:
                on_success(result)

        def on_worker_error(exception):
            progress.reject()
            if on_error:
                on_error(exception)
            else:
                # Default error handling
                QMessageBox.critical(
                    self, "Error",
                    f"{operation_name} failed: {str(exception)}"
                )
                self.logger.exception(f"Threaded operation '{operation_name}' failed")

        worker.finished.connect(on_finished)
        worker.error.connect(on_worker_error)

        # Handle cancellation
        if cancelable:
            progress.rejected.connect(worker.cancel)

        # Start worker and show progress
        worker.start()
        progress.exec()

        # Wait for thread to finish (important!)
        worker.wait()

    def _load_text_editor(self, file_path: str, filename: str, content_bytes: bytes) -> None:
        """Loads file content into the text editor."""
        try:
            fs_type = self.controller.filesystem.get_display_info().get("Filesystem Type", "Unknown")
            if fs_type == "CP/M":
                cleaned_bytes = bytes([b & 0x7F for b in content_bytes])
                content_text = cleaned_bytes.decode('ascii', errors='replace')
            else:
                content_text = content_bytes.decode('cp437', errors='replace')

            content_text = content_text.replace('\r\n', '\n').replace('\r', '\n')

            self.text_viewer.blockSignals(True)
            self.text_viewer.setPlainText(content_text)
            self.text_viewer.blockSignals(False)
            self.original_text_content = content_text
            self.current_file_path = file_path
            self.text_editor_modified = False
            self.save_button.setEnabled(False)
            self.discard_button.setEnabled(False)
            self.text_viewer_dock.setWindowTitle(f"Text Editor - {filename}")
            self.text_viewer_dock.raise_()
            self.logger.info(f"Loaded '{filename}' into text editor.")
        except Exception as e:
            self.logger.error(f"Error loading text editor: {e}", exc_info=True)
            QMessageBox.warning(self, "Warning", f"Could not display as text: {str(e)}")

    def _load_hex_viewer(self, file_path: str, filename: str, content_bytes: bytes) -> None:
        """Loads file content into the hex viewer."""
        try:
            hex_lines = []
            bytes_per_line = 16

            for offset in range(0, len(content_bytes), bytes_per_line):
                chunk = content_bytes[offset:offset + bytes_per_line]

                # Offset column (8 chars)
                offset_str = f"{offset:08X}"

                # Hex bytes - always 16 positions, padding with spaces for incomplete lines
                hex_parts = []
                for i in range(bytes_per_line):
                    if i < len(chunk):
                        hex_parts.append(f"{chunk[i]:02X}")
                    else:
                        hex_parts.append("  ")  # Two spaces for missing bytes

                # Group hex bytes in pairs of 2 for readability
                hex_str = ' '.join(hex_parts[:8]) + '  ' + ' '.join(hex_parts[8:])

                # ASCII representation - pad with spaces for incomplete lines
                ascii_chars = []
                for i in range(bytes_per_line):
                    if i < len(chunk):
                        b = chunk[i]
                        ascii_chars.append(chr(b) if 32 <= b < 127 else '.')
                    else:
                        ascii_chars.append(' ')
                ascii_str = ''.join(ascii_chars)

                # Format: OFFSET  HEX(8) HEX(8)  ASCII
                hex_lines.append(f"{offset_str}  {hex_str}  {ascii_str}")

            hex_content = '\n'.join(hex_lines)

            self.hex_viewer.setPlainText(hex_content)
            self.current_hex_file_path = file_path
            self.hex_viewer_dock.setWindowTitle(f"Hex Viewer - {filename} ({len(content_bytes)} bytes)")
            self.hex_viewer_dock.raise_()
            self.logger.info(f"Loaded '{filename}' into hex viewer.")
        except Exception as e:
            self.logger.error(f"Error loading hex viewer: {e}", exc_info=True)
            QMessageBox.warning(self, "Warning", f"Could not display hex view: {str(e)}")


def run_gui() -> None:
    """
    Initializes and runs the FatFloppy GUI application.
    """
    import sys
    import os

    # macOS: Set app name before QApplication creation
    if sys.platform == 'darwin':
        os.environ['RESOURCE_NAME'] = 'FatFloppy'

    app = QApplication(sys.argv)

    # Set application metadata BEFORE creating any windows
    app.setApplicationName("FatFloppy")
    app.setApplicationDisplayName("FatFloppy")
    app.setOrganizationName("FatFloppy")
    app.setOrganizationDomain("fatfloppy.local")

    # Set the menuRole to make macOS treat it properly
    # This should be done in the menu creation, but let's also set desktop file name
    if sys.platform == 'darwin':
        app.setDesktopFileName("FatFloppy")

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
