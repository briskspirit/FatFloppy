# src/fatfloppy/gui/file_browser.py
"""
Custom QTreeWidget with drag-and-drop functionality for importing files.
"""

import os
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtWidgets import QMessageBox, QTreeWidget, QWidget


class DragDropTreeWidget(QTreeWidget):
    """
    A QTreeWidget subclass that accepts dropped files from the local system
    and initiates an import process.
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
        self.parent_widget = parent

    # ##################################################################
    # Event Handlers
    # ##################################################################

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """
        Accepts drag events if they contain file URLs.

        Args:
            event: The QDragEnterEvent containing mime data.
        """
        if event.mimeData().hasUrls():
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        """
        Handles the movement of a drag event over the widget.

        Sets the drop action to CopyAction to indicate that files will be
        copied, not moved.

        Args:
            event: The QDragMoveEvent.
        """
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        """
        Handles the drop event.

        Extracts the local file paths from the event's mime data and
        passes them to the processing method.

        Args:
            event: The QDropEvent.
        """
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            file_paths = [url.toLocalFile() for url in event.mimeData().urls()]
            self.process_dropped_files(file_paths)
        else:
            super().dropEvent(event)

    # ##################################################################
    # Public Methods
    # ##################################################################

    def process_dropped_files(self, file_paths: List[str]) -> None:
        """
        Processes a list of dropped file paths by calling the parent's
        import method.

        This method validates that a disk image is loaded before attempting
        the import. It also distinguishes between single-file and multi-file
        drops to adjust import behavior.

        Args:
            file_paths: A list of absolute paths to the dropped files/folders.
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
