# src/fatfloppy/gui/file_browser.py
"""
Custom QTreeWidget with drag-and-drop functionality for importing files.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QMimeData, QPoint, Qt, QUrl
from PyQt6.QtGui import (
    QAction,
    QDrag,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
)
from PyQt6.QtWidgets import QMenu, QMessageBox, QTreeWidget, QWidget

DRAG_TEMP_DIR_PREFIX = "fatfloppy_drag_"
DRAG_HIGHLIGHT_STYLE = (
    # Pin a dark text colour so rows stay readable against the light highlight
    # background in dark themes (audit file_browser.py:24).
    "QTreeWidget { background-color: #E8F4F8; color: #101010; "
    "border: 2px dashed #2196F3; }"
)


class DragDropTreeWidget(QTreeWidget):
    """
    A QTreeWidget subclass that accepts dropped files from the local system.

    Supports:
    - Importing files via drag-and-drop from local filesystem
    - Exporting files via drag-out to local filesystem
    - Context menu for file operations
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initializes the DragDropTreeWidget.

        Args:
            parent: The parent widget, which is expected to handle the file
                    import logic.
        """
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.DragDrop)
        self.parent_widget = parent
        self._temp_extraction_dir = None
        atexit.register(self._cleanup_temp_dir)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _cleanup_temp_dir(self) -> None:
        """Remove the temporary extraction directory if it exists."""
        if self._temp_extraction_dir and Path(self._temp_extraction_dir).exists():
            shutil.rmtree(self._temp_extraction_dir, ignore_errors=True)
            self._temp_extraction_dir = None

    def process_dropped_files(self, file_paths: list[str]) -> None:
        """
        Processes a list of dropped file paths by calling parent's import method.

        Args:
            file_paths: List of local file paths to import.
        """
        if (
            not hasattr(self.parent_widget, "controller")
            or not self.parent_widget.current_node
        ):
            QMessageBox.warning(
                self.parent_widget,
                "Warning",
                "No disk image loaded or no destination selected.",
            )
            return

        current_path = self.parent_widget.current_path

        auto_name = not (len(file_paths) == 1 and Path(file_paths[0]).is_file())
        self.parent_widget.import_multiple_paths(
            file_paths, current_path, auto_name=auto_name
        )

    def startDrag(self, _supportedActions) -> None:  # noqa: N802, N803
        """
        Handles the start of a drag operation to extract files.

        Extracts selected files to a temp directory and provides them for dragging.

        Args:
            _supportedActions: Supported drag actions (unused, required by Qt).
        """
        selected_items = self.selectedItems()
        if not selected_items:
            return

        valid_items = []
        for item in selected_items:
            if hasattr(item, "node"):
                valid_items.append(item)

        if not valid_items:
            return

        if self._temp_extraction_dir is None:
            self._temp_extraction_dir = tempfile.mkdtemp(prefix=DRAG_TEMP_DIR_PREFIX)

        temp_file_paths = []
        for item in valid_items:
            node = item.node
            source_path = self._build_full_path_for_node(node)

            if node.is_dir:
                local_dir_path = str(Path(self._temp_extraction_dir) / node.name)
                if self._extract_directory_for_drag(source_path, local_dir_path):
                    temp_file_paths.append(local_dir_path)
            else:
                local_file_path = str(Path(self._temp_extraction_dir) / node.name)
                if self._extract_file_for_drag(source_path, local_file_path):
                    temp_file_paths.append(local_file_path)

        if not temp_file_paths:
            return

        mime_data = QMimeData()
        urls = [QUrl.fromLocalFile(path) for path in temp_file_paths]
        mime_data.setUrls(urls)

        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """
        Accepts drag events only if they contain file URLs from external sources.

        Args:
            event: The drag enter event.
        """
        if event.source() == self:
            event.ignore()
            return

        if event.mimeData().hasUrls():
            event.accept()
            self.setStyleSheet(DRAG_HIGHLIGHT_STYLE)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        """
        Resets the visual feedback when drag leaves the widget.

        Args:
            event: The drag leave event.
        """
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        """
        Handles the movement of a drag event over the widget.

        Args:
            event: The drag move event.
        """
        if event.source() == self:
            event.ignore()
            return

        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """
        Handles the drop event.

        Args:
            event: The drop event.
        """
        self.setStyleSheet("")

        if event.source() == self:
            event.ignore()
            return

        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            file_paths = [url.toLocalFile() for url in event.mimeData().urls()]
            self.process_dropped_files(file_paths)
        else:
            event.ignore()

    def _build_full_path_for_node(self, node) -> str:
        """
        Builds the full disk image path for a node.

        Args:
            node: The file/directory node.

        Returns:
            The full path on the disk image.
        """
        if hasattr(self.parent_widget, "_build_full_path"):
            return self.parent_widget._build_full_path(node.name)
        current_path = getattr(self.parent_widget, "current_path", "/")
        if current_path != "/":
            return f"{current_path}/{node.name}"
        return f"/{node.name}"

    def _context_delete(self) -> None:
        """Context menu handler for Delete action."""
        if hasattr(self.parent_widget, "delete_selected_items"):
            self.parent_widget.delete_selected_items()

    def _context_extract(self) -> None:
        """Context menu handler for Extract action."""
        if hasattr(self.parent_widget, "extract_selected_items"):
            self.parent_widget.extract_selected_items()

    def _context_view_file(self) -> None:
        """Context menu handler for View action."""
        if hasattr(self.parent_widget, "view_file_content"):
            self.parent_widget.view_file_content()

    def _extract_directory_for_drag(
        self, source_dir_path: str, local_dir_path: str
    ) -> bool:
        """
        Recursively extracts a directory from disk image to local filesystem.

        Args:
            source_dir_path: Path to the directory on the disk image.
            local_dir_path: Local filesystem path to extract to.

        Returns:
            True if extraction was successful, False otherwise.
        """
        try:
            if (
                not hasattr(self.parent_widget, "controller")
                or not self.parent_widget.controller
            ):
                return False

            Path(local_dir_path).mkdir(parents=True, exist_ok=True)
            items = self.parent_widget.controller.list_directory(source_dir_path)

            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue

                item_source_path = os.path.normpath(
                    str(Path(source_dir_path) / item_name)
                )
                item_local_path = str(Path(local_dir_path) / item_name)

                if item["is_dir"]:
                    self._extract_directory_for_drag(item_source_path, item_local_path)
                else:
                    self._extract_file_for_drag(item_source_path, item_local_path)

            return True
        except Exception as e:
            if hasattr(self.parent_widget, "logger"):
                self.parent_widget.logger.error(
                    f"Error extracting directory {source_dir_path}: {e}"
                )
        return False

    def _extract_file_for_drag(self, source_path: str, local_path: str) -> bool:
        """
        Extracts a file from disk image to local filesystem for dragging.

        Args:
            source_path: Path to the file on the disk image.
            local_path: Local filesystem path to extract to.

        Returns:
            True if extraction was successful, False otherwise.
        """
        try:
            if (
                not hasattr(self.parent_widget, "controller")
                or not self.parent_widget.controller
            ):
                return False

            file_data = self.parent_widget.controller.read_file(source_path)
            if file_data is not None:
                Path(local_path).write_bytes(file_data)
                return True
        except Exception as e:
            if hasattr(self.parent_widget, "logger"):
                self.parent_widget.logger.error(
                    f"Error extracting file {source_path}: {e}"
                )
        return False

    def _show_context_menu(self, position: QPoint) -> None:
        """
        Shows a context menu for file operations.

        Args:
            position: The position where the context menu was requested.
        """
        item = self.itemAt(position)
        if not item or not hasattr(item, "node"):
            return

        node = item.node

        menu = QMenu(self)

        if not node.is_dir:
            view_action = QAction("View", self)
            view_action.triggered.connect(lambda: self._context_view_file())
            menu.addAction(view_action)
            menu.addSeparator()

        extract_action = QAction("Extract", self)
        extract_action.triggered.connect(lambda: self._context_extract())
        menu.addAction(extract_action)

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._context_delete())
        menu.addAction(delete_action)

        menu.exec(self.viewport().mapToGlobal(position))
