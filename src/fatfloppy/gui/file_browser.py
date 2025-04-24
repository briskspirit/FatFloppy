import os
from PyQt6.QtCore import Qt, QUrl, QMimeData
from PyQt6.QtWidgets import QInputDialog, QMessageBox, QTreeWidget

class DragDropTreeWidget(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.parent = parent

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            links = []
            for url in event.mimeData().urls():
                links.append(str(url.toLocalFile()))
            self.process_dropped_files(links)
        else:
            super().dropEvent(event)

    def process_dropped_files(self, file_paths):
        if not hasattr(self.parent, 'controller') or not self.parent.current_node:
            QMessageBox.warning(self.parent, "Warning", "No disk image loaded")
            return

        current_path = self.parent.current_path

        if len(file_paths) == 1 and os.path.isfile(file_paths[0]):
            self.parent.import_path(file_paths[0], current_path, auto_name=False)
        else:
            for file_path in file_paths:
                self.parent.import_path(file_path, current_path, auto_name=True)
