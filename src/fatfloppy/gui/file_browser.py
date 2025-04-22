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
        total_files = len(file_paths)

        def import_files():
            for idx, file_path in enumerate(file_paths):
                if os.path.isdir(file_path):
                    QMessageBox.information(self.parent, "Info",
                                        f"Directory dropping is not yet supported: {os.path.basename(file_path)}")
                    continue

                base_name = os.path.basename(file_path)
                base_name = self.parent.format_83_filename(base_name)

                new_name, ok = QInputDialog.getText(self.parent, "File Name",
                                                f"Enter file name for {base_name} (8.3 format):",
                                                text=base_name)
                if not ok or not new_name:
                    continue

                try:
                    self.parent.add_file_to_disk(file_path, new_name, current_path)
                except Exception as e:
                    QMessageBox.critical(self.parent, "Error", f"Failed to add file: {str(e)}")
        import_files()
