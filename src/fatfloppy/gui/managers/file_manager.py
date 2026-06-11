# src/fatfloppy/gui/managers/file_manager.py
"""
Manages file operations including import, export, deletion, and editing.
"""

import logging
import os
import posixpath
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMainWindow, QMessageBox

from fatfloppy.core.filesystems.fs_base import (
    default_suggest_host_name,
    default_suggest_import_name,
)


def _active_filesystem(manager):
    """
    Returns the manager's open filesystem, or None when nothing is loaded.

    Tolerates a missing controller/filesystem (no disk open) and even an
    unbound call with manager=None (the test suite pins the no-filesystem
    fallback by calling naming methods unbound).

    Args:
        manager: A FileManager instance, or None.

    Returns:
        The active Filesystem, or None.
    """
    parent = getattr(manager, "parent", None)
    controller = getattr(parent, "controller", None)
    return getattr(controller, "filesystem", None)


def _unique_host_name(name: str, used: set[str]) -> str:
    """
    Uniquifies a host filename within a batch.

    Two distinct on-disk names can map to the same host-safe name (e.g.
    "COPY/ALL" and "COPY_ALL" both become "COPY_ALL"), so multi-item
    extraction appends " (2)", " (3)", ... when the name was already produced
    earlier in the same batch.  Pre-existing host files are overwritten
    (legacy refresh workflow); same-batch collisions get " (N)".

    Args:
        name: The host-safe candidate name.
        used: Names already produced in this batch (updated in place).

    Returns:
        A name unique within the current batch.
    """
    candidate = name
    counter = 2
    while candidate in used:
        candidate = f"{name} ({counter})"
        counter += 1
    used.add(candidate)
    return candidate


def _sanitize_local_name(name: str) -> str:
    """
    Reduces an untrusted on-disk filename to a single safe path component.

    Disk-image filenames are untrusted (CP/M and HDOS names are not validated on
    read, so they can contain '/', '\\' or '..'). Stripping the path and rejecting
    traversal segments prevents an extracted file from escaping the chosen
    directory (audit file_manager.py:698).

    Args:
        name: The raw filename from the disk image.

    Returns:
        A safe, non-empty single path component.
    """
    candidate = name.replace("\\", "/").split("/")[-1].strip()
    if not candidate or candidate in (".", ".."):
        return "_unnamed_"
    return candidate


def _safe_local_join(base_dir: str, name: str) -> str:
    """
    Joins an untrusted on-disk name onto a base directory, safely.

    Args:
        base_dir: The trusted local destination directory.
        name: The untrusted on-disk name.

    Returns:
        The local path string for the sanitized name.

    Raises:
        ValueError: If the resolved path would escape base_dir.
    """
    safe = _sanitize_local_name(name)
    base = Path(base_dir).resolve()
    target = (base / safe).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"Unsafe extraction path for on-disk name '{name}'")
    return str(Path(base_dir) / safe)


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
        hint = self._name_hint()
        dir_name, ok = QInputDialog.getText(
            self.parent, "Create New Directory", f"Enter directory name ({hint}):"
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
                local_dir_path = _safe_local_join(base_dir, self._host_name(node.name))

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
                    self.parent, "Save File", self._host_name(node.name)
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
                    used_names: set[str] = set()
                    for idx, item in enumerate(selected_items):
                        node = item.node
                        if progress_callback:
                            progress_callback(idx, total, f"Extracting {node.name}...")

                        source_path = self.parent._build_full_path(node.name)
                        local_name = _unique_host_name(
                            self._host_name(node.name), used_names
                        )
                        local_path = _safe_local_join(base_dir, local_name)
                        if node.is_dir:
                            self._extract_directory(source_path, local_path)
                        else:
                            self._extract_file(source_path, local_path)

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
                used_names: set[str] = set()
                for item in selected_items:
                    node = item.node
                    source_path = self.parent._build_full_path(node.name)
                    local_name = _unique_host_name(
                        self._host_name(node.name), used_names
                    )
                    local_path = _safe_local_join(base_dir, local_name)
                    if node.is_dir:
                        self._extract_directory(source_path, local_path)
                    else:
                        self._extract_file(source_path, local_path)
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
            hint = self._name_hint()
            base_name = self._format_83_filename(Path(local_path).name)
            prompt = f"Enter file name for {base_name} ({hint}):"
            suggested = base_name
            is_physical = (
                self.parent.controller.driver
                and self.parent.controller.driver.driver_category == "physical"
            )
            while True:
                new_name, ok = QInputDialog.getText(
                    self.parent, "File Name", prompt, text=suggested
                )
                if not ok or not new_name:
                    self.logger.debug("File name input cancelled for import.")
                    return
                suggested = new_name

                existing_names = [
                    item["name"].upper()
                    for item in self.parent.controller.list_directory(target_path)
                ]
                if new_name.upper() in existing_names:
                    prompt = (
                        f"File '{new_name}' already exists in {target_path}.\n"
                        f"Enter file name ({hint}):"
                    )
                    continue

                if is_physical:
                    # Physical writes run threaded; a rejected name surfaces
                    # through the threaded operation's error box instead.
                    break

                try:
                    self._add_file_to_disk(local_path, new_name, target_path)
                except ValueError as e:
                    # The filesystem rejected the name: show the reason and
                    # re-prompt instead of aborting.
                    self.logger.warning(f"Name '{new_name}' rejected: {e}")
                    prompt = f"{e}\nEnter file name ({hint}):"
                    continue
                except OSError as e:
                    # Disk-level error (e.g. "Disk full"): the name is not
                    # the problem, so show the error and stop without re-prompting.
                    self.logger.error(f"Write error for '{new_name}': {e}")
                    QMessageBox.warning(self.parent, "Write Error", str(e))
                    break
                self.logger.info(f"Imported 1 item to '{target_path}'.")
                self.refresh_needed.emit(target_path)
                self.operation_complete.emit(f"Imported 1 item to '{target_path}'")
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
            failed_imports: list[str] = []
            for local_path, dest_name in file_paths_with_names:
                try:
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
                            failed_imports.append(dest_name)
                            continue

                        items = [p.name for p in Path(local_path).iterdir()]
                        sub_paths = [str(Path(local_path) / item) for item in items]
                        self.import_multiple_paths(
                            sub_paths, new_dir_path, auto_name=True
                        )
                except (OSError, ValueError, NotImplementedError) as e:
                    # Surface the failure (e.g. a read-only filesystem, or a
                    # filesystem that doesn't support directories) in a dialog:
                    # an exception escaping this Qt slot would be fatal to the
                    # application.
                    self.logger.error(f"Error importing {dest_name}: {e}")
                    failed_imports.append(f"{dest_name}: {e}")

            self.refresh_needed.emit(target_path)
            if failed_imports:
                QMessageBox.warning(
                    self.parent,
                    "Import Failed",
                    "Failed to import:\n" + "\n".join(failed_imports),
                )
            imported = len(file_paths_with_names) - len(failed_imports)
            item_word = "item" if imported == 1 else "items"
            self.operation_complete.emit(
                f"Imported {imported} {item_word} to '{target_path}'"
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

        # posixpath.join, not normpath(f"{dest_path}/{dest_name}"): normpath
        # preserves a POSIX-special double leading slash, so a root target
        # produced "//NAME" and CBM (which allows '/' in names) stored the
        # file as "/NAME".
        full_path = posixpath.join(dest_path, dest_name)
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
            used_names: set[str] = set()

            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue

                if progress_callback:
                    progress_callback(
                        current_item, total_items, f"Extracting {item_name}..."
                    )

                item_source_path = posixpath.normpath(f"{source_dir_path}/{item_name}")
                local_name = _unique_host_name(self._host_name(item_name), used_names)
                item_local_path = _safe_local_join(local_dir_path, local_name)
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
                # _extract_file runs on a worker thread for physical disks; touch
                # the GUI only via a signal, never a direct QMessageBox (audit
                # file_manager.py:731).
                self.error_occurred.emit(
                    "Warning", f"Failed to read file data for {source_path}"
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
        Suggests a valid on-disk name for a host filename.

        Kept under its legacy 8.3 name for existing callers; it now delegates
        to the active filesystem's name policy, falling back to the classic
        8.3 default when no filesystem is loaded.

        Args:
            filename: The original host filename.

        Returns:
            A valid on-disk name suggestion.
        """
        fs = _active_filesystem(self)
        if fs is None:
            return default_suggest_import_name(filename, set())
        return fs.suggest_import_name(filename, set())

    def _generate_unique_83_name(
        self, original_name: str, target_path: str, is_dir: bool
    ) -> str:
        """
        Generates a valid on-disk name unique within target_path.

        Kept under its legacy 8.3 name for existing callers; it now delegates
        to the active filesystem's name policy, falling back to the classic
        8.3 default when no filesystem is loaded.

        Args:
            original_name: The original name from the local filesystem.
            target_path: The directory on the disk image where the item will be
                         added.
            is_dir: True if the item is a directory, False for a file.

        Returns:
            A valid, unique on-disk name.

        Raises:
            ValueError: If a unique name cannot be generated after many attempts.
        """
        existing_names = {
            item["name"] for item in self.parent.controller.list_directory(target_path)
        }
        fs = _active_filesystem(self)
        if fs is None:
            return default_suggest_import_name(
                original_name, existing_names, is_dir=is_dir
            )
        return fs.suggest_import_name(original_name, existing_names, is_dir=is_dir)

    def _host_name(self, name: str) -> str:
        """
        Maps an on-disk name to a host-safe filename for extraction.

        Args:
            name: The raw on-disk filename.

        Returns:
            A host-safe filename, per the active filesystem's policy.
        """
        fs = _active_filesystem(self)
        if fs is None:
            return default_suggest_host_name(name)
        return fs.suggest_host_name(name)

    def _name_hint(self) -> str:
        """Returns the active filesystem's naming-rules hint for dialog labels."""
        fs = _active_filesystem(self)
        return fs.name_hint() if fs else "8.3 format"

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
