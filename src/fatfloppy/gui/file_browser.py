# src/fatfloppy/gui/file_browser.py
import os
import tempfile
from PyQt6.QtCore import Qt, QUrl, QMimeData, QTimer
from PyQt6.QtGui import QDrag
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
                    # callback((idx + 1) / total_files)  # Update progress
                except Exception as e:
                    QMessageBox.critical(self.parent, "Error", f"Failed to add file: {str(e)}")

        # Execute with progress dialog
        # self.parent.perform_with_progress(lambda cb: import_files(cb))
        import_files()

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return

        items = self.selectedItems()
        if not items:
            return

        files_to_drag = [item for item in items if not item.node.is_dir]
        if not files_to_drag:
            return

        total_files = len(files_to_drag)
        temp_dir = tempfile.mkdtemp()
        urls = []

        def prepare_files(callback):
            for idx, item in enumerate(files_to_drag):
                node = item.node
                try:
                    file_path = self.parent.build_full_path(node.name)
                    file_data = self.parent.controller.read_file(file_path)
                    temp_path = os.path.join(temp_dir, node.name)
                    with open(temp_path, 'wb') as f:
                        f.write(file_data)
                    urls.append(QUrl.fromLocalFile(temp_path))
                    callback((idx + 1) / total_files)  # Update progress
                except Exception as e:
                    QMessageBox.critical(self.parent, "Error", f"Failed to prepare file for dragging: {str(e)}")
            return urls

        # Execute with progress dialog
        result_urls = self.parent.perform_with_progress(lambda cb: prepare_files(cb), title="Preparing Files...")
        if result_urls:
            urls = result_urls

        if urls:
            drag = QDrag(self)
            mime_data = QMimeData()
            mime_data.setUrls(urls)
            drag.setMimeData(mime_data)
            drag.exec(Qt.DropAction.CopyAction)

            # Cleanup temporary files after a delay
            def cleanup_temp_files():
                try:
                    import shutil
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except:
                    pass
            QTimer.singleShot(10000, cleanup_temp_files)

        super().mouseMoveEvent(event)
