# src/fatfloppy/gui/managers/file_manager.py
"""
Manages file operations including import, export, deletion, and editing.
"""

import logging
import os
import posixpath
import re
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMainWindow, QMessageBox


class FileManager(QObject):
    """Handles all file-related operations for the GUI application."""

    operation_complete = pyqtSignal(str)
    refresh_needed = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, parent: "QMainWindow") -> None:
        """
        Initialize the file manager.

        Args:
            parent: The main window that owns this manager.
        """
        super().__init__(parent)
        self.parent = parent
        self.logger: logging.Logger = parent.logger

    def add_file(self) -> None:
        """
        Opens a file dialog to select a local file and adds it to current directory.
        """
        if not self.parent.controller or not self.parent.current_node:
            QMessageBox.warning(
                self.parent,
                "Warning",
                "No disk image loaded or no current directory selected.",
            )
            return

        file_path, _ = QFileDialog.getOpenFileName(self.parent, "Select File to Add")
        if not file_path:
            self.logger.debug("Add File dialog cancelled.")
            return

        self.import_multiple_paths(
            [file_path], self.parent.current_path, auto_name=False
        )

    def create_directory(self) -> None:
        """Prompts the user for a directory name and creates a new directory."""
        if not self.parent.controller or not self.parent.current_node:
            QMessageBox.warning(
                self.parent,
                "Warning",
                "No disk image loaded or no current directory selected.",
            )
            return

        current_path = self.parent.current_path
        dir_name, ok = QInputDialog.getText(
            self.parent, "Create New Directory", "Enter directory name (8.3 format):"
        )
        if not ok or not dir_name:
            self.logger.debug("Create Directory dialog cancelled or no name entered.")
            return

        full_path = f"{current_path}/{dir_name}".replace("//", "/")
        self.logger.info(f"Attempting to create directory: {full_path}")

        is_physical = (
            self.parent.controller.driver
            and self.parent.controller.driver.driver_category == "physical"
        )

        if is_physical:

            def create_dir_op(progress_callback=None):
                if progress_callback:
                    progress_callback(0, 1, f"Creating {dir_name}...")

                success = self.parent.controller.create_directory(full_path)
                if not success:
                    raise Exception(f"Failed to create directory {dir_name}")

                if progress_callback:
                    progress_callback(1, 1, "Complete")
                return True

            def on_create_success(_):
                self.refresh_needed.emit(current_path)
                self.operation_complete.emit(
                    f"Created directory {dir_name} in {current_path}"
                )
                self.logger.info(f"Successfully created directory: {full_path}")

            def on_create_error(exception):
                QMessageBox.warning(self.parent, "Warning", str(exception))
                self.logger.warning(f"Failed to create directory {full_path}.")

            self.parent._run_threaded_operation(
                create_dir_op,
                f"Creating directory {dir_name}",
                on_success=on_create_success,
                on_error=on_create_error,
                cancelable=False,
            )
        else:
            try:
                success = self.parent.controller.create_directory(full_path)
                if success:
                    self.refresh_needed.emit(current_path)
                    self.operation_complete.emit(
                        f"Created directory {dir_name} in {current_path}"
                    )
                    self.logger.info(f"Successfully created directory: {full_path}")
                else:
                    QMessageBox.warning(
                        self.parent,
                        "Warning",
                        f"Failed to create directory {dir_name}. It might "
                        f"already exist or name is invalid.",
                    )
                    self.logger.warning(f"Failed to create directory {full_path}.")
            except Exception as e:
                self.error_occurred.emit(
                    "Error", f"Failed to create directory: {str(e)}"
                )
                self.logger.exception(f"Error creating directory {full_path}.")

    def delete_selected_items(self) -> None:
        """Deletes selected files or directories from the disk image."""
        selected_items = self.parent.file_list.selectedItems()
        if not selected_items or not self.parent.controller:
            QMessageBox.warning(
                self.parent,
                "No Selection",
                "No items selected or no disk loaded to delete.",
            )
            return

        paths_to_delete: list[str] = [
            self.parent._build_full_path(item.node.name) for item in selected_items
        ]
        self.logger.info(
            f"Attempting to delete {len(paths_to_delete)} item(s): {paths_to_delete}"
        )

        if len(paths_to_delete) == 1:
            msg = f"Are you sure you want to delete '{paths_to_delete[0]}'?"
        else:
            msg = f"Are you sure you want to delete {len(paths_to_delete)} items?"

        reply = QMessageBox.question(
            self.parent,
            "Confirm Deletion",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.logger.debug("Deletion cancelled by user.")
            return

        current_path = self.parent.current_path
        is_physical = (
            self.parent.controller.driver
            and self.parent.controller.driver.driver_category == "physical"
        )

        if is_physical:

            def delete_op(progress_callback=None):
                failed_paths: list[str] = []
                total = len(paths_to_delete)

                for idx, path in enumerate(paths_to_delete):
                    if progress_callback:
                        progress_callback(idx, total, f"Deleting {Path(path).name}...")

                    try:
                        success = self.parent.controller.delete_item_recursive(path)
                        if not success:
                            failed_paths.append(path)
                            self.logger.warning(f"Failed to delete item: {path}")
                    except Exception as e:
                        failed_paths.append(path)
                        self.logger.error(
                            f"Error deleting item {path}: {e}", exc_info=True
                        )

                if progress_callback:
                    progress_callback(total, total, "Complete")
                return failed_paths

            def on_delete_success(failed_paths):
                self.refresh_needed.emit(current_path)

                if not failed_paths:
                    self.operation_complete.emit(
                        f"Deleted {len(paths_to_delete)} item(s)"
                    )
                    self.logger.info(
                        f"Successfully deleted {len(paths_to_delete)} item(s)."
                    )
                else:
                    failed_msg = "Failed to delete:\n" + "\n".join(failed_paths)
                    QMessageBox.warning(self.parent, "Deletion Failed", failed_msg)
                    self.operation_complete.emit(
                        f"Deleted {len(paths_to_delete) - len(failed_paths)} "
                        f"item(s), {len(failed_paths)} failed."
                    )
                    self.logger.warning(
                        f"Deletion completed with {len(failed_paths)} failures."
                    )

            self.parent._run_threaded_operation(
                delete_op,
                f"Deleting {len(paths_to_delete)} item(s)",
                on_success=on_delete_success,
                cancelable=False,
            )
        else:
            failed_paths: list[str] = []
            for path in paths_to_delete:
                try:
                    success = self.parent.controller.delete_item_recursive(path)
                    if not success:
                        failed_paths.append(path)
                        self.logger.warning(f"Failed to delete item: {path}")
                except Exception as e:
                    failed_paths.append(path)
                    self.logger.error(f"Error deleting item {path}: {e}", exc_info=True)

            self.refresh_needed.emit(current_path)

            if not failed_paths:
                self.operation_complete.emit(f"Deleted {len(paths_to_delete)} item(s)")
                self.logger.info(
                    f"Successfully deleted {len(paths_to_delete)} item(s)."
                )
            else:
                failed_msg = "Failed to delete:\n" + "\n".join(failed_paths)
                QMessageBox.warning(self.parent, "Deletion Failed", failed_msg)
                self.operation_complete.emit(
                    f"Deleted {len(paths_to_delete) - len(failed_paths)} "
                    f"item(s), {len(failed_paths)} failed."
                )
                self.logger.warning(
                    f"Deletion completed with {len(failed_paths)} failures."
                )

    def extract_selected_items(self) -> None:
        """
        Extracts selected files or directories from disk image to local filesystem.
        """
        selected_items = self.parent.file_list.selectedItems()
        if not selected_items or not self.parent.controller:
            QMessageBox.warning(
                self.parent,
                "No Selection",
                "No items selected or no disk loaded to extract.",
            )
            return

        self.logger.debug(f"Extracting {len(selected_items)} selected item(s).")

        is_physical = (
            self.parent.controller.driver
            and self.parent.controller.driver.driver_category == "physical"
        )

        if len(selected_items) == 1:
            item = selected_items[0]
            node = item.node
            source_path = self.parent._build_full_path(node.name)

            if node.is_dir:
                base_dir = QFileDialog.getExistingDirectory(
                    self.parent, "Select Directory to Extract To"
                )
                if not base_dir:
                    return
                local_dir_path = str(Path(base_dir) / node.name)

                if is_physical:

                    def extract_op(progress_callback=None):
                        self._extract_directory(
                            source_path, local_dir_path, progress_callback
                        )
                        return True

                    self.parent._run_threaded_operation(
                        extract_op,
                        f"Extracting {node.name}",
                        on_success=lambda _: self.operation_complete.emit(
                            "Extraction complete."
                        ),
                        cancelable=False,
                    )
                else:
                    self._extract_directory(source_path, local_dir_path)
                    self.operation_complete.emit("Extraction complete.")
            else:
                save_path, _ = QFileDialog.getSaveFileName(
                    self.parent, "Save File", node.name
                )
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

                    self.parent._run_threaded_operation(
                        extract_op,
                        f"Extracting {node.name}",
                        on_success=lambda _: self.operation_complete.emit(
                            "Extraction complete."
                        ),
                        cancelable=False,
                    )
                else:
                    self._extract_file(source_path, save_path)
                    self.operation_complete.emit("Extraction complete.")
        else:
            base_dir = QFileDialog.getExistingDirectory(
                self.parent, "Select Directory to Extract To"
            )
            if not base_dir:
                return

            if is_physical:

                def extract_multiple_op(progress_callback=None):
                    total = len(selected_items)
                    for idx, item in enumerate(selected_items):
                        node = item.node
                        if progress_callback:
                            progress_callback(idx, total, f"Extracting {node.name}...")

                        source_path = self.parent._build_full_path(node.name)
                        if node.is_dir:
                            local_dir_path = str(Path(base_dir) / node.name)
                            self._extract_directory(source_path, local_dir_path)
                        else:
                            local_file_path = str(Path(base_dir) / node.name)
                            self._extract_file(source_path, local_file_path)

                    if progress_callback:
                        progress_callback(total, total, "Complete")
                    return True

                self.parent._run_threaded_operation(
                    extract_multiple_op,
                    f"Extracting {len(selected_items)} items",
                    on_success=lambda _: self.operation_complete.emit(
                        "Extraction complete."
                    ),
                    cancelable=False,
                )
            else:
                for item in selected_items:
                    node = item.node
                    source_path = self.parent._build_full_path(node.name)
                    if node.is_dir:
                        local_dir_path = str(Path(base_dir) / node.name)
                        self._extract_directory(source_path, local_dir_path)
                    else:
                        local_file_path = str(Path(base_dir) / node.name)
                        self._extract_file(source_path, local_file_path)
                self.operation_complete.emit("Extraction complete.")

    def import_multiple_paths(
        self, file_paths: list[str], target_path: str, auto_name: bool = True
    ) -> None:
        """
        Imports multiple files/directories at once with progress tracking.

        Args:
            file_paths: List of local file/directory paths to import.
            target_path: Target path on the disk image.
            auto_name: If True, generates unique names automatically. If False,
                       prompts for name.
        """
        if not file_paths:
            return

        if len(file_paths) == 1 and Path(file_paths[0]).is_file() and not auto_name:
            local_path = file_paths[0]
            base_name = Path(local_path).name
            base_name = self._format_83_filename(base_name)
            new_name, ok = QInputDialog.getText(
                self.parent,
                "File Name",
                f"Enter file name for {base_name} (8.3 format):",
                text=base_name,
            )
            if not ok or not new_name:
                self.logger.debug("File name input cancelled for import.")
                return

            existing_names = [
                item["name"].upper()
                for item in self.parent.controller.list_directory(target_path)
            ]
            if new_name.upper() in existing_names:
                QMessageBox.warning(
                    self.parent,
                    "Warning",
                    f"File '{new_name}' already exists in {target_path}",
                )
                self.logger.warning(
                    f"File '{new_name}' already exists, import aborted."
                )
                return

            file_paths_with_names = [(local_path, new_name)]
        else:
            file_paths_with_names = []
            for local_path in file_paths:
                local_path = os.path.normpath(local_path)
                if Path(local_path).is_file():
                    original_name = Path(local_path).name
                    new_name = self._generate_unique_83_name(
                        original_name, target_path, is_dir=False
                    )
                    file_paths_with_names.append((local_path, new_name))
                elif Path(local_path).is_dir():
                    original_name = Path(local_path).name
                    if not original_name:
                        continue
                    new_name = self._generate_unique_83_name(
                        original_name, target_path, is_dir=True
                    )
                    file_paths_with_names.append((local_path, new_name))

        self.logger.info(
            f"Importing {len(file_paths_with_names)} items to '{target_path}'."
        )

        is_physical = (
            self.parent.controller.driver
            and self.parent.controller.driver.driver_category == "physical"
        )

        if is_physical:

            def import_multiple_op(progress_callback=None):
                total = len(file_paths_with_names)

                for idx, (local_path, dest_name) in enumerate(file_paths_with_names):
                    if progress_callback:
                        progress_callback(idx, total, f"Importing {dest_name}...")

                    if Path(local_path).is_file():
                        self._add_file_to_disk(local_path, dest_name, target_path)
                    elif Path(local_path).is_dir():
                        new_dir_path = (
                            f"{target_path}/{dest_name}"
                            if target_path != "/"
                            else f"/{dest_name}"
                        )

                        success = self.parent.controller.create_directory(new_dir_path)
                        if not success:
                            self.logger.error(f"Failed to create directory {dest_name}")
                            continue

                        self._import_directory_recursive(
                            local_path, new_dir_path, progress_callback
                        )

                if progress_callback:
                    progress_callback(total, total, "Complete")
                return total

            def on_success(count):
                self.refresh_needed.emit(target_path)
                item_word = "item" if count == 1 else "items"
                self.operation_complete.emit(
                    f"Imported {count} {item_word} to '{target_path}'"
                )

            def on_error(exception):
                self.error_occurred.emit(
                    "Error", f"Failed to import items: {str(exception)}"
                )
                self.logger.exception("Error importing items.")

            item_word = "item" if len(file_paths_with_names) == 1 else "items"
            self.parent._run_threaded_operation(
                import_multiple_op,
                f"Importing {len(file_paths_with_names)} {item_word}",
                on_success=on_success,
                on_error=on_error,
                cancelable=False,
            )
        else:
            for local_path, dest_name in file_paths_with_names:
                if Path(local_path).is_file():
                    self._add_file_to_disk(local_path, dest_name, target_path)
                elif Path(local_path).is_dir():
                    new_dir_path = (
                        f"{target_path}/{dest_name}"
                        if target_path != "/"
                        else f"/{dest_name}"
                    )
                    success = self.parent.controller.create_directory(new_dir_path)
                    if not success:
                        self.logger.error(f"Failed to create directory {dest_name}")
                        continue

                    items = [p.name for p in Path(local_path).iterdir()]
                    sub_paths = [str(Path(local_path) / item) for item in items]
                    self.import_multiple_paths(sub_paths, new_dir_path, auto_name=True)

            self.refresh_needed.emit(target_path)
            item_word = "item" if len(file_paths_with_names) == 1 else "items"
            self.operation_complete.emit(
                f"Imported {len(file_paths_with_names)} {item_word} to '{target_path}'"
            )

    def read_file_threaded(self, path: str, on_success: Callable) -> None:
        """
        Reads a file from disk in a background thread.

        Args:
            path: Path to the file on the disk image.
            on_success: Callback to handle successful read (receives content bytes).
        """

        def operation():
            return self.parent.controller.read_file(path)

        def success_handler(content):
            if content is not None:
                on_success(content)
            else:
                QMessageBox.warning(
                    self.parent, "Warning", f"Could not read file: {path}"
                )

        self.parent._run_threaded_operation(
            operation,
            f"Reading {Path(path).name}",
            on_success=success_handler,
            cancelable=False,
        )

    def save_file(
        self,
        file_path: str,
        content_bytes: bytes,
        original_text: str,
        on_success: Callable,
    ) -> None:
        """
        Saves file content to the disk image.

        Args:
            file_path: Path to the file on the disk image.
            content_bytes: Content to write.
            original_text: Original text content (for editor state update).
            on_success: Callback for successful save.
        """
        is_physical = (
            self.parent.controller.driver
            and self.parent.controller.driver.driver_category == "physical"
        )

        if is_physical:

            def save_op():
                if not self.parent.controller.write_file(file_path, content_bytes):
                    raise Exception("Write operation failed")
                return original_text

            def on_save_success(saved_text):
                self.operation_complete.emit(f"Saved changes to {Path(file_path).name}")
                on_success(saved_text)

            def on_save_error(exception):
                QMessageBox.warning(
                    self.parent,
                    "Save Failed",
                    f"Could not write changes: {str(exception)}",
                )
                self.logger.warning(f"Failed to write file data for {file_path}")

            self.parent._run_threaded_operation(
                save_op,
                f"Saving {Path(file_path).name}",
                on_success=on_save_success,
                on_error=on_save_error,
                cancelable=False,
            )
        else:
            if self.parent.controller.write_file(file_path, content_bytes):
                self.operation_complete.emit(f"Saved changes to {Path(file_path).name}")
                on_success(original_text)
                self.logger.info(f"Successfully saved {file_path}")
            else:
                QMessageBox.warning(
                    self.parent,
                    "Save Failed",
                    f"Could not write changes to {file_path}",
                )
                self.logger.warning(f"Failed to write file data for {file_path}")

    def write_file_threaded(self, path: str, data: bytes, on_success: Callable) -> None:
        """
        Writes a file to disk in a background thread.

        Args:
            path: Path to the file on the disk image.
            data: Data to write.
            on_success: Callback to handle successful write.
        """

        def operation():
            success = self.parent.controller.write_file(path, data)
            if not success:
                raise Exception("Write operation returned False")
            return success

        self.parent._run_threaded_operation(
            operation,
            f"Writing {Path(path).name}",
            on_success=on_success,
            cancelable=False,
        )

    def _add_file_to_disk(self, file_path: str, dest_name: str, dest_path: str) -> None:
        """
        Reads a local file and writes it to the disk image.

        Args:
            file_path: Path to the local file.
            dest_name: Name of the file on the disk image (8.3 format).
            dest_path: Path to the directory on the disk image.
        """
        self.logger.info(
            f"Adding local file '{file_path}' as '{dest_name}' to '{dest_path}'."
        )
        file_data = Path(file_path).read_bytes()

        full_path = posixpath.normpath(f"{dest_path}/{dest_name}")
        success = self.parent.controller.write_file(full_path, file_data)
        if not success:
            raise Exception(f"Failed to write file {dest_name}")
        self.logger.info(f"Successfully added file {dest_name}.")

    def _extract_directory(
        self, source_dir_path: str, local_dir_path: str, progress_callback=None
    ) -> None:
        """
        Recursively extracts a directory and its contents from the disk image.

        Args:
            source_dir_path: The full path to the directory on the disk image.
            local_dir_path: The full path to create the directory locally.
            progress_callback: Optional callback for progress reporting.
        """
        try:
            self.logger.info(
                f"Extracting directory '{source_dir_path}' to '{local_dir_path}'."
            )
            Path(local_dir_path).mkdir(parents=True, exist_ok=True)
            items = self.parent.controller.list_directory(source_dir_path)

            total_items = len([i for i in items if i["name"] not in [".", ".."]])
            current_item = 0

            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue

                if progress_callback:
                    progress_callback(
                        current_item, total_items, f"Extracting {item_name}..."
                    )

                item_source_path = posixpath.normpath(f"{source_dir_path}/{item_name}")
                item_local_path = str(Path(local_dir_path) / item_name)
                if item["is_dir"]:
                    self._extract_directory(item_source_path, item_local_path)
                else:
                    self._extract_file(item_source_path, item_local_path)

                current_item += 1

            self.logger.info(f"Successfully extracted directory {source_dir_path}.")
        except Exception as e:
            self.error_occurred.emit(
                "Error", f"Failed to extract directory {source_dir_path}: {str(e)}"
            )
            self.logger.exception(f"Error extracting directory {source_dir_path}.")

    def _extract_file(self, source_path: str, local_path: str) -> None:
        """
        Extracts a single file from the disk image to the local filesystem.

        Args:
            source_path: The full path to the file on the disk image.
            local_path: The full path to save the file locally.
        """
        try:
            self.logger.info(f"Extracting file '{source_path}' to '{local_path}'.")
            file_data = self.parent.controller.read_file(source_path)
            if file_data is not None:
                Path(local_path).write_bytes(file_data)
                self.operation_complete.emit(
                    f"Extracted {Path(source_path).name} to {Path(local_path).name}"
                )
                self.logger.info(f"Successfully extracted file {source_path}.")
            else:
                QMessageBox.warning(
                    self.parent,
                    "Warning",
                    f"Failed to read file data for {source_path}",
                )
                self.logger.warning(
                    f"Failed to read file data for extraction of {source_path}."
                )
        except Exception as e:
            self.error_occurred.emit(
                "Error", f"Failed to extract file {source_path}: {str(e)}"
            )
            self.logger.exception(f"Error extracting file {source_path}.")

    def _format_83_filename(self, filename: str) -> str:
        """
        Formats a filename into an 8.3 FAT-compatible format.

        Args:
            filename: The original filename.

        Returns:
            The 8.3 formatted filename.
        """
        filename = re.sub(r'[<>:"/\\|?*]', "", filename).strip().upper()

        if "." in filename:
            parts = filename.split(".")
            base = parts[0][:8]
            ext = parts[-1][:3]
            return f"{base}.{ext}"
        else:
            return filename[:8]

    def _generate_unique_83_name(
        self, original_name: str, target_path: str, is_dir: bool
    ) -> str:
        """
        Generates a unique 8.3 filename (or directory name) for target filesystem.

        Args:
            original_name: The original name from the local filesystem.
            target_path: The directory on the disk image where the item will be
                         added.
            is_dir: True if the item is a directory, False for a file.

        Returns:
            A unique 8.3 format name.

        Raises:
            ValueError: If a unique name cannot be generated after many attempts.
        """

        def to_83_name_format(name: str, is_directory: bool) -> str:
            name = name.upper()
            name = re.sub(r'[\\/:*?"<>|\s+]', "_", name)
            if "." in name and not is_directory:
                base, ext = name.rsplit(".", 1)
                base = base[:8]
                ext = ext[:3]
                return f"{base}.{ext}"
            else:
                return name[:8]

        existing_names = {
            item["name"].upper()
            for item in self.parent.controller.list_directory(target_path)
        }
        base_name_83 = to_83_name_format(original_name, is_dir)

        if base_name_83 not in existing_names:
            return base_name_83

        if "." in base_name_83 and not is_dir:
            base, ext = base_name_83.rsplit(".", 1)
            base = base[:6]
            for counter in range(1, 1000):
                new_base = f"{base}~{counter:02d}"[:8]
                new_name = f"{new_base}.{ext}"
                if new_name not in existing_names:
                    return new_name
        else:
            base = base_name_83[:6]
            for counter in range(1, 1000):
                new_name = f"{base}~{counter:02d}"[:8]
                if new_name not in existing_names:
                    return new_name

        self.logger.error(
            f"Failed to generate unique 8.3 name for '{original_name}' in "
            f"'{target_path}'."
        )
        raise ValueError("Cannot generate unique name")

    def _import_directory_recursive(
        self, local_path: str, target_path: str, progress_callback=None
    ) -> None:
        """
        Recursively imports a directory from local filesystem to disk image.

        Args:
            local_path: Local directory path to import.
            target_path: Target path on disk image.
            progress_callback: Optional callback for progress reporting.
        """
        dir_name = Path(local_path).name
        new_name = self._generate_unique_83_name(dir_name, target_path, is_dir=True)
        new_path = f"{target_path}/{new_name}" if target_path != "/" else f"/{new_name}"

        success = self.parent.controller.create_directory(new_path)
        if not success:
            raise Exception(f"Failed to create directory {new_name}")

        items = [p.name for p in Path(local_path).iterdir()]
        total_items = len(items)

        for idx, item in enumerate(items):
            if progress_callback:
                progress_callback(idx, total_items, f"Importing {item}...")

            item_path = str(Path(local_path) / item)
            if Path(item_path).is_file():
                item_name = self._generate_unique_83_name(
                    Path(item_path).name, new_path, is_dir=False
                )
                self._add_file_to_disk(item_path, item_name, new_path)
            elif Path(item_path).is_dir():
                self._import_directory_recursive(item_path, new_path)
