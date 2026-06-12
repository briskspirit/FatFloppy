# src/fatfloppy/gui/managers/editor_manager.py
"""
Manages text and hex editor operations for viewing and editing files.
"""

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMainWindow, QMessageBox


class EditorManager(QObject):
    """Handles text and hex viewer/editor operations for the GUI application."""

    content_loaded = pyqtSignal(str, str, str)
    modified_changed = pyqtSignal(bool)
    save_complete = pyqtSignal(str)
    refresh_needed = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, parent: "QMainWindow") -> None:
        """
        Initialize the editor manager.

        Args:
            parent: The main window that owns this manager.
        """
        super().__init__(parent)
        self.parent = parent
        self.logger: logging.Logger = parent.logger

        self.current_file_path: Optional[str] = None
        self.text_editor_modified: bool = False
        self.original_text_content: Optional[str] = None
        self.current_hex_file_path: Optional[str] = None

    def clear_hex_viewer_state(self) -> None:
        """Clears the hex viewer content and resets its state."""
        self.parent.hex_viewer.clear()
        self.current_hex_file_path = None
        self.parent.hex_viewer_dock.setWindowTitle("Hex Viewer")
        self.logger.debug("Hex viewer state cleared.")

    def clear_text_viewer_state(self) -> None:
        """Clears the text viewer content and resets its state."""
        self.parent.text_viewer.blockSignals(True)
        self.parent.text_viewer.clear()
        self.parent.text_viewer.blockSignals(False)
        # Restore the default editable state (a binary preview may have
        # switched the widget to read-only).
        self.parent.text_viewer.setReadOnly(False)
        self.original_text_content = None
        self.current_file_path = None
        self.text_editor_modified = False
        self.parent.save_button.setEnabled(False)
        self.parent.discard_button.setEnabled(False)
        self.parent.text_viewer_dock.setWindowTitle("Text Viewer")
        self.logger.debug("Text viewer state cleared.")

    def discard_changes(self) -> None:
        """
        Discards unsaved changes in the text editor and reverts to original content.
        """
        if self.original_text_content is not None and self.text_editor_modified:
            self.logger.info(f"Discarding changes for {self.current_file_path}")
            reply = QMessageBox.question(
                self.parent,
                "Discard Changes",
                "Are you sure you want to discard all changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if reply == QMessageBox.StandardButton.Yes:
                self.parent.text_viewer.blockSignals(True)
                self.parent.text_viewer.setPlainText(self.original_text_content)
                self.parent.text_viewer.blockSignals(False)

                self.text_editor_modified = False
                self.parent.save_button.setEnabled(False)
                self.parent.discard_button.setEnabled(False)
                self.modified_changed.emit(False)
                self.parent.statusBar().showMessage(
                    f"Changes to {Path(self.current_file_path).name} discarded."
                )
                self.logger.info(
                    f"Changes to {self.current_file_path} successfully discarded."
                )
            else:
                self.logger.debug("Discard changes cancelled by user.")
        else:
            self.parent.save_button.setEnabled(False)
            self.parent.discard_button.setEnabled(False)
            self.logger.debug(
                "Discard changes requested, but no modifications or no original "
                "content."
            )

    def has_unsaved_changes(self) -> bool:
        """
        Checks if there are unsaved changes in the editor.

        Returns:
            True if there are unsaved changes.
        """
        return self.text_editor_modified

    def on_text_editor_changed(self) -> None:
        """Handles changes in the text editor's content."""
        if self.original_text_content is not None:
            current_text = self.parent.text_viewer.toPlainText()
            is_modified = current_text != self.original_text_content
            self.text_editor_modified = is_modified
            self.parent.save_button.setEnabled(is_modified)
            self.parent.discard_button.setEnabled(is_modified)
            self.modified_changed.emit(is_modified)
            self.logger.debug(f"Text editor modified status: {is_modified}")
        else:
            self.text_editor_modified = False
            self.parent.save_button.setEnabled(False)
            self.parent.discard_button.setEnabled(False)
            self.logger.debug("Text editor changed, but no original content set.")

    def save_file(self) -> bool:
        """
        Saves changes from the text editor to the currently viewed file.

        Returns:
            True if the file was saved successfully or no changes were present,
            False otherwise.
        """
        if not self.current_file_path or not self.text_editor_modified:
            if not self.text_editor_modified:
                self.parent.statusBar().showMessage("No changes to save.")
                self.logger.debug(
                    f"Save requested for {self.current_file_path} but no changes "
                    f"detected."
                )
                return True
            else:
                QMessageBox.warning(
                    self.parent, "Warning", "No file context for saving."
                )
                self.logger.warning("Save requested without a current file context.")
                return False

        self.logger.info(f"Saving changes to {self.current_file_path}")

        if not self.parent.controller or not self.parent.controller.filesystem:
            self.error_occurred.emit("Save Error", "No disk is open. Cannot save file.")
            self.logger.error("Save requested but no filesystem is active.")
            return False

        try:
            current_text = self.parent.text_viewer.toPlainText()
            # Restore the file's original line-ending style rather than always
            # forcing CRLF (audit editor_manager.py:155).
            if getattr(self, "_original_uses_crlf", True):
                normalized_text = current_text.replace("\n", "\r\n")
            else:
                normalized_text = current_text

            fs_type = self.parent.controller.filesystem.get_display_info().get(
                "Filesystem Type", "Unknown"
            )

            if fs_type == "CP/M":
                try:
                    encoded = normalized_text.encode("ascii", errors="strict")
                except UnicodeEncodeError as e:
                    self.error_occurred.emit(
                        "Encoding Warning",
                        f"Some characters cannot be represented in ASCII and "
                        f"will be replaced with '?': {e}",
                    )
                    encoded = normalized_text.encode("ascii", errors="replace")
                content_bytes = bytes([b & 0x7F for b in encoded])
            else:
                try:
                    content_bytes = normalized_text.encode("cp437", errors="strict")
                except UnicodeEncodeError as e:
                    self.error_occurred.emit(
                        "Encoding Warning",
                        f"Some characters cannot be represented in CP437 and "
                        f"will be replaced with '?': {e}",
                    )
                    content_bytes = normalized_text.encode("cp437", errors="replace")

            file_path = self.current_file_path
            current_path = self.parent.current_path

            def on_save_success(saved_text):
                self.original_text_content = saved_text
                self.text_editor_modified = False
                self.parent.save_button.setEnabled(False)
                self.parent.discard_button.setEnabled(False)
                self.modified_changed.emit(False)
                self.refresh_needed.emit(current_path)
                self.save_complete.emit(saved_text)
                self.logger.info(f"Successfully saved {file_path}")

            self.parent.file_manager.save_file(
                file_path, content_bytes, current_text, on_save_success
            )
            return True

        except UnicodeEncodeError as e:
            self.error_occurred.emit(
                "Encoding Error",
                f"Text contains characters not supported by the target encoding: {e}",
            )
            self.logger.error(
                f"Encoding error when saving {self.current_file_path}: {e}"
            )
            return False
        except Exception as e:
            self.error_occurred.emit("Error", f"Failed to save file: {str(e)}")
            self.logger.exception(f"Unexpected error saving {self.current_file_path}.")
            return False

    def view_file_content(self) -> None:
        """Views the selected file in appropriate viewer (text or hex)."""
        selected_items = self.parent.file_list.selectedItems()
        if len(selected_items) != 1:
            QMessageBox.information(
                self.parent, "Info", "Please select a single file to view."
            )
            return

        item = selected_items[0]
        if not hasattr(item, "node"):
            QMessageBox.warning(self.parent, "Warning", "Invalid item selected.")
            return

        node = item.node
        if node.is_dir:
            QMessageBox.information(
                self.parent, "Info", "Cannot view directory contents."
            )
            return

        file_path = self.parent._build_full_path(node.name)

        is_physical = (
            self.parent.controller.driver
            and self.parent.controller.driver.driver_category == "physical"
        )

        if is_physical:

            def on_read_success(content_bytes):
                self._display_in_viewers(file_path, node.name, content_bytes)

            self.parent.file_manager.read_file_threaded(file_path, on_read_success)
        else:
            try:
                content_bytes = self.parent.controller.read_file(file_path)
                if content_bytes is None:
                    QMessageBox.warning(
                        self.parent, "Warning", f"Could not read file: {node.name}"
                    )
                    return

                self._display_in_viewers(file_path, node.name, content_bytes)
            except Exception as e:
                self.error_occurred.emit("Error", f"Error reading file: {str(e)}")

    def _display_in_viewers(
        self, file_path: str, filename: str, content_bytes: bytes
    ) -> None:
        """
        Loads the file into BOTH the text and hex viewers.

        The text/binary guess from _is_text_file() only decides which dock is
        raised; the other viewer is populated all the same (exactly as if it
        had been opened directly, just not popped in front), so the user can
        simply switch tabs when the guess is wrong.

        Save-path safety: for a binary (hex-guessed) file the text view is a
        lossy decoded-with-replacement rendering, so it is loaded READ-ONLY
        with the save path disabled -- writing that text back would corrupt
        the file. For a text-guessed file the editor behaves exactly as
        before, and the hex viewer is read-only by construction.

        Args:
            file_path: Path to the file on the disk image.
            filename: Display name of the file.
            content_bytes: File content as bytes.
        """
        is_text = self._is_text_file(content_bytes)
        self._load_hex_viewer(
            file_path, filename, content_bytes, raise_dock=not is_text
        )
        self._load_text_editor(
            file_path,
            filename,
            content_bytes,
            read_only=not is_text,
            raise_dock=is_text,
        )

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
            return True

        try:
            text = sample.decode("cp437")
            non_printable = sum(
                1 for c in text if not (c.isprintable() or c in "\r\n\t")
            )
            if len(text) > 0 and (non_printable / len(text)) > 0.1:
                percentage = (non_printable / len(text)) * 100
                self.logger.debug(
                    f"File detected as binary: {percentage:.2f}% non-printable "
                    f"characters."
                )
                return False
            return True
        except UnicodeDecodeError:
            self.logger.debug(
                "File detected as binary: UnicodeDecodeError during CP437 decode."
            )
            return False

    def _load_hex_viewer(
        self,
        file_path: str,
        filename: str,
        content_bytes: bytes,
        raise_dock: bool = True,
    ) -> None:
        """
        Loads file content into the hex viewer.

        Args:
            file_path: Path to the file on disk image.
            filename: Display name of the file.
            content_bytes: File content as bytes.
            raise_dock: Whether to raise the hex viewer dock after loading.
        """
        try:
            hex_lines = []
            bytes_per_line = 16

            for offset in range(0, len(content_bytes), bytes_per_line):
                chunk = content_bytes[offset : offset + bytes_per_line]

                offset_str = f"{offset:08X}"

                hex_parts = []
                for i in range(bytes_per_line):
                    if i < len(chunk):
                        hex_parts.append(f"{chunk[i]:02X}")
                    else:
                        hex_parts.append("  ")

                hex_str = " ".join(hex_parts[:8]) + "  " + " ".join(hex_parts[8:])

                ascii_chars = []
                for i in range(bytes_per_line):
                    if i < len(chunk):
                        b = chunk[i]
                        ascii_chars.append(chr(b) if 32 <= b < 127 else ".")
                    else:
                        ascii_chars.append(" ")
                ascii_str = "".join(ascii_chars)

                hex_lines.append(f"{offset_str}  {hex_str}  {ascii_str}")

            hex_content = "\n".join(hex_lines)

            self.parent.hex_viewer.setPlainText(hex_content)
            self.current_hex_file_path = file_path
            self.parent.hex_viewer_dock.setWindowTitle(
                f"Hex Viewer - {filename} ({len(content_bytes)} bytes)"
            )
            if raise_dock:
                self.parent.hex_viewer_dock.raise_()
            self.logger.info(f"Loaded '{filename}' into hex viewer.")
        except Exception as e:
            self.logger.error(f"Error loading hex viewer: {e}", exc_info=True)
            QMessageBox.warning(
                self.parent, "Warning", f"Could not display hex view: {str(e)}"
            )

    def _load_text_editor(
        self,
        file_path: str,
        filename: str,
        content_bytes: bytes,
        read_only: bool = False,
        raise_dock: bool = True,
    ) -> None:
        """
        Loads file content into the text editor.

        Args:
            file_path: Path to the file on disk image.
            filename: Display name of the file.
            content_bytes: File content as bytes.
            read_only: Load as a read-only preview with the save path
                disabled. Used for binary (hex-guessed) files whose text
                rendering is lossy and must never be written back.
            raise_dock: Whether to raise the text editor dock after loading.
        """
        try:
            fs_type = "Unknown"
            if self.parent.controller and self.parent.controller.filesystem:
                fs_type = self.parent.controller.filesystem.get_display_info().get(
                    "Filesystem Type", "Unknown"
                )
            if fs_type == "CP/M":
                cleaned_bytes = bytes([b & 0x7F for b in content_bytes])
                content_text = cleaned_bytes.decode("ascii", errors="replace")
            else:
                content_text = content_bytes.decode("cp437", errors="replace")

            # Remember the file's dominant line-ending style so save restores it
            # instead of forcing CRLF onto a file that used bare LF (audit
            # editor_manager.py:155).
            crlf_count = content_text.count("\r\n")
            lone_lf_count = content_text.count("\n") - crlf_count
            self._original_uses_crlf = crlf_count >= lone_lf_count
            content_text = content_text.replace("\r\n", "\n").replace("\r", "\n")

            self.parent.text_viewer.blockSignals(True)
            self.parent.text_viewer.setPlainText(content_text)
            self.parent.text_viewer.blockSignals(False)
            self.parent.text_viewer.setReadOnly(read_only)
            if read_only:
                # Binary preview: keep the save path inert. With no original
                # content recorded, on_text_editor_changed() can never mark
                # the editor modified, and save_file() refuses to write.
                self.original_text_content = None
                self.parent.text_viewer_dock.setWindowTitle(
                    f"Text Viewer - {filename} (read-only)"
                )
            else:
                self.original_text_content = content_text
                self.parent.text_viewer_dock.setWindowTitle(f"Text Editor - {filename}")
            self.current_file_path = file_path
            self.text_editor_modified = False
            self.parent.save_button.setEnabled(False)
            self.parent.discard_button.setEnabled(False)
            if raise_dock:
                self.parent.text_viewer_dock.raise_()
            self.logger.info(
                f"Loaded '{filename}' into text editor"
                f"{' (read-only preview)' if read_only else ''}."
            )
        except Exception as e:
            self.logger.error(f"Error loading text editor: {e}", exc_info=True)
            QMessageBox.warning(
                self.parent, "Warning", f"Could not display as text: {str(e)}"
            )
