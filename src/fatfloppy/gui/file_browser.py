# src/fatfloppy/gui/file_browser.py
"""
Custom QTreeWidget with drag-and-drop functionality for importing files.
"""

import os
import tempfile
from typing import List, Optional

from PyQt6.QtCore import Qt, QUrl, QMimeData, QPoint
from PyQt6.QtGui import QDrag, QDragEnterEvent, QDragMoveEvent, QDropEvent, QAction, QContextMenuEvent
from PyQt6.QtWidgets import QMessageBox, QTreeWidget, QWidget, QMenu


class DragDropTreeWidget(QTreeWidget):
    """
    A QTreeWidget subclass that accepts dropped files from the local system
    and initiates an import process. Also allows dragging files out for extraction.
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

        # Enable context menu
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ##################################################################
    # Context Menu
    # ##################################################################

    def _show_context_menu(self, position: QPoint) -> None:
        """
        Shows a context menu for file operations.

        Args:
            position: The position where the context menu was requested.
        """
        # Get the item at the click position
        item = self.itemAt(position)
        if not item or not hasattr(item, 'node'):
            return

        node = item.node

        # Create context menu
        menu = QMenu(self)

        # View action (only for files)
        if not node.is_dir:
            view_action = QAction("View", self)
            view_action.triggered.connect(lambda: self._context_view_file())
            menu.addAction(view_action)
            menu.addSeparator()

        # Extract action
        extract_action = QAction("Extract", self)
        extract_action.triggered.connect(lambda: self._context_extract())
        menu.addAction(extract_action)

        # Delete action
        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._context_delete())
        menu.addAction(delete_action)

        # Show the menu at the cursor position
        menu.exec(self.viewport().mapToGlobal(position))

    def _context_view_file(self) -> None:
        """Context menu handler for View action."""
        if hasattr(self.parent_widget, 'view_file_content'):
            self.parent_widget.view_file_content()

    def _context_extract(self) -> None:
        """Context menu handler for Extract action."""
        if hasattr(self.parent_widget, 'extract_selected_items'):
            self.parent_widget.extract_selected_items()

    def _context_delete(self) -> None:
        """Context menu handler for Delete action."""
        if hasattr(self.parent_widget, 'delete_selected_items'):
            self.parent_widget.delete_selected_items()

    # ##################################################################
    # Drag-out (Extract) Handlers
    # ##################################################################

    def startDrag(self, supportedActions) -> None:
        """
        Handles the start of a drag operation to extract files.
        Extracts selected files to a temp directory and provides them for dragging.
        """
        selected_items = self.selectedItems()
        if not selected_items:
            return

        # Validate all items have the node attribute
        valid_items = []
        for item in selected_items:
            if hasattr(item, 'node'):
                valid_items.append(item)

        if not valid_items:
            return

        # Create temp directory for extraction
        if self._temp_extraction_dir is None:
            self._temp_extraction_dir = tempfile.mkdtemp(prefix="fatfloppy_drag_")

        # Extract files to temp directory
        temp_file_paths = []
        for item in valid_items:
            node = item.node
            source_path = self._build_full_path_for_node(node)

            if node.is_dir:
                # Extract directory
                local_dir_path = os.path.join(self._temp_extraction_dir, node.name)
                if self._extract_directory_for_drag(source_path, local_dir_path):
                    temp_file_paths.append(local_dir_path)
            else:
                # Extract file
                local_file_path = os.path.join(self._temp_extraction_dir, node.name)
                if self._extract_file_for_drag(source_path, local_file_path):
                    temp_file_paths.append(local_file_path)

        if not temp_file_paths:
            return

        # Create MIME data with file URLs
        mime_data = QMimeData()
        urls = [QUrl.fromLocalFile(path) for path in temp_file_paths]
        mime_data.setUrls(urls)

        # Start the drag operation
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)

    def _build_full_path_for_node(self, node) -> str:
        """Builds the full disk image path for a node."""
        if hasattr(self.parent_widget, '_build_full_path'):
            return self.parent_widget._build_full_path(node.name)
        # Fallback: build path manually
        current_path = getattr(self.parent_widget, 'current_path', '/')
        if current_path != "/":
            return f"{current_path}/{node.name}"
        return f"/{node.name}"

    def _extract_file_for_drag(self, source_path: str, local_path: str) -> bool:
        """Extracts a file from disk image to local filesystem for dragging."""
        try:
            if not hasattr(self.parent_widget, 'controller') or not self.parent_widget.controller:
                return False

            file_data = self.parent_widget.controller.read_file(source_path)
            if file_data is not None:
                with open(local_path, 'wb') as f:
                    f.write(file_data)
                return True
        except Exception as e:
            print(f"Error extracting file {source_path}: {e}")
        return False

    def _extract_directory_for_drag(self, source_dir_path: str, local_dir_path: str) -> bool:
        """Recursively extracts a directory from disk image to local filesystem."""
        try:
            if not hasattr(self.parent_widget, 'controller') or not self.parent_widget.controller:
                return False

            os.makedirs(local_dir_path, exist_ok=True)
            items = self.parent_widget.controller.list_directory(source_dir_path)

            for item in items:
                item_name = item["name"]
                if item_name in [".", ".."]:
                    continue

                item_source_path = os.path.normpath(os.path.join(source_dir_path, item_name))
                item_local_path = os.path.join(local_dir_path, item_name)

                if item["is_dir"]:
                    self._extract_directory_for_drag(item_source_path, item_local_path)
                else:
                    self._extract_file_for_drag(item_source_path, item_local_path)

            return True
        except Exception as e:
            print(f"Error extracting directory {source_dir_path}: {e}")
        return False

    # ##################################################################
    # Drop-in (Import) Handlers
    # ##################################################################

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """
        Accepts drag events only if they contain file URLs from external sources.
        Rejects internal drags (items from this same widget).
        """
        # Reject internal drag-and-drop (moving items within the list)
        if event.source() == self:
            event.ignore()
            return

        # Accept external file drops
        if event.mimeData().hasUrls():
            event.accept()
            self.setStyleSheet("QTreeWidget { background-color: #E8F4F8; border: 2px dashed #2196F3; }")
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        """Resets the visual feedback when drag leaves the widget."""
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        """
        Handles the movement of a drag event over the widget.
        Rejects internal moves.
        """
        # Reject internal drag-and-drop
        if event.source() == self:
            event.ignore()
            return

        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """
        Handles the drop event.
        Rejects internal drops.
        """
        # Reset visual feedback
        self.setStyleSheet("")

        # Reject internal drag-and-drop (prevent dropping items back into the list)
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

    # ##################################################################
    # Public Methods
    # ##################################################################

    def process_dropped_files(self, file_paths: List[str]) -> None:
        """
        Processes a list of dropped file paths by calling the parent's
        import method.
        """
        if not hasattr(self.parent_widget, 'controller') or not self.parent_widget.current_node:
            QMessageBox.warning(self.parent_widget, "Warning", "No disk image loaded or no destination selected.")
            return

        current_path = self.parent_widget.current_path

        # If a single file is dropped, import it directly without auto-naming.
        if len(file_paths) == 1 and os.path.isfile(file_paths[0]):
            self.parent_widget.import_path(file_paths[0], current_path, auto_name=False)
        else:
            # For multiple files/folders, use auto-naming for each item.
            for file_path in file_paths:
                self.parent_widget.import_path(file_path, current_path, auto_name=True)
